"""Service supervisor: keep bots, workers and servers running.

Free hosting rarely offers background workers. rconsole is a machine you can
run anything on, but a plain `python bot.py &` dies on every restart or crash.
A **service** is a named command that rconsole keeps alive:

* it **starts automatically** when rconsole boots (and a restored instance
  starts them once your VMs are rebuilt — start attempts simply retry until the
  VM is ready);
* if it **crashes** it is restarted with exponential backoff (2 s, 4 s, ... up to
  1 min; a run that lasted a minute resets the backoff);
* it runs on the **host** (admins) or inside one of **your VMs**;
* its output is available with ``service logs <name>``;
* optionally it is **exposed** at ``/<name>/`` (``--expose PORT:NAME``).

Definitions are stored in ``<data>/services.json`` (so they are part of every
backup); the running state lives in memory. The supervisor is a single thread
that wakes every 2 seconds.
"""
import copy
import json
import os
import re
import secrets
import shlex
import threading
import time

import auth
import config
import exposure
import jobs
import sandbox
import state

FILE = config.DATA_DIR / "services.json"
NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?\Z")
MAX_PER_USER = 10
BACKOFF_MIN = 2
BACKOFF_MAX = 60
STABLE_AFTER = 60               # seconds of uptime that count as "it works"
TICK = 2

_lock = threading.RLock()
_cache = {"sig": None, "data": {}}
_rt = {}                        # name -> runtime state (in memory only)
_thread = None
_stop = threading.Event()
_paused = False          # set during shutdown so nothing is (re)started while going down


# ------------------------------------------------------------------ storage
def _sig():
    try:
        st = os.stat(FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _read():
    sig = _sig()
    if sig is None:
        return {}
    if sig == _cache["sig"]:
        return _cache["data"]
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        return {}
    _cache["sig"], _cache["data"] = sig, data
    return data


def _save(data):
    config.ensure_private_dir(config.DATA_DIR)
    tmp = FILE.with_name(f".services.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _cache["sig"], _cache["data"] = _sig(), data


# ------------------------------------------------------------------ queries
def get(name):
    with _lock:
        s = _read().get((name or "").lower())
        return dict(s, name=name.lower()) if s else None


def list_all():
    with _lock:
        return [dict(v, name=k) for k, v in sorted(_read().items())]


def list_for(owner):
    return [s for s in list_all() if s["owner"] == owner]


def validate_name(name):
    if not NAME_RE.match((name or "").lower()):
        return "name must be 1-32 characters: letters, digits, '-' and '_' (not at the ends)"
    return None


# ---------------------------------------------------------------- mutations
def add(name, owner, cmd, target, cwd="", env=None, expose=None, enabled=True):
    """Create (or replace, if you own it) a service. Returns ``(ok, message)``."""
    name = (name or "").lower()
    err = validate_name(name)
    if err:
        return False, err
    if not (cmd or "").strip():
        return False, "no command given"
    if expose:
        port, ename = expose
        err = exposure.validate_name(ename) or exposure.validate_port(port)
        if err:
            return False, f"--expose: {err}"
    with _lock:
        data = copy.deepcopy(_read())
        old = data.get(name)
        if old and old["owner"] != owner:
            return False, f"'{name}' is used by {old['owner']}"
        if not old and sum(1 for v in data.values() if v["owner"] == owner) >= MAX_PER_USER:
            return False, f"you already have {MAX_PER_USER} services; remove one first"
        if expose:
            ok, msg = exposure.add(expose[1], expose[0], owner)
            if not ok:
                return False, f"--expose: {msg}"
        data[name] = {"owner": owner, "cmd": cmd.strip(), "target": target, "cwd": cwd or "",
                      "env": dict(env or {}), "expose": list(expose) if expose else None,
                      "enabled": bool(enabled),
                      "created": old["created"] if old else int(time.time())}
        _save(data)
        _rt.pop(name, None)
        return True, "updated" if old else "created"


def remove(name, requester, is_admin=False):
    name = (name or "").lower()
    with _lock:
        data = copy.deepcopy(_read())
        s = data.get(name)
        if not s:
            return False, f"no service called '{name}'"
        if s["owner"] != requester and not is_admin:
            return False, f"'{name}' belongs to {s['owner']}"
        _kill(name, s["owner"])
        if s.get("expose"):
            exposure.remove(s["expose"][1], s["owner"], is_admin=True)
        del data[name]
        _save(data)
        _rt.pop(name, None)
        return True, "removed"


def set_enabled(name, requester, is_admin, on):
    name = (name or "").lower()
    with _lock:
        data = copy.deepcopy(_read())
        s = data.get(name)
        if not s:
            return False, f"no service called '{name}'"
        if s["owner"] != requester and not is_admin:
            return False, f"'{name}' belongs to {s['owner']}"
        s["enabled"] = bool(on)
        _save(data)
        if on:
            _rt.setdefault(name, _new_rt())["next"] = 0        # start on the next tick
        else:
            _kill(name, s["owner"])
        return True, "started" if on else "stopped"


def remove_owner(owner):
    """Drop every service of a user (account deleted); stops their processes."""
    with _lock:
        data = copy.deepcopy(_read())
        mine = [k for k, v in data.items() if v["owner"] == owner]
        for k in mine:
            _kill(k, owner)
            if data[k].get("expose"):
                exposure.remove(data[k]["expose"][1], owner, is_admin=True)
            del data[k]
            _rt.pop(k, None)
        if mine:
            _save(data)
        return len(mine)


def rename_owner(old, new):
    with _lock:
        data = copy.deepcopy(_read())
        changed = False
        for k, v in data.items():
            if v["owner"] == old:
                v["owner"] = new
                changed = True
        if changed:
            _save(data)
            _rt.clear()                  # jobs were keyed by the old name


def clear():
    with _lock:
        for k, v in _read().items():
            _kill(k, v["owner"])
        _save({})
        _rt.clear()


# ------------------------------------------------------------------ runtime
def _new_rt():
    return {"jid": None, "started": 0.0, "restarts": 0, "next": 0.0, "backoff": BACKOFF_MIN,
            "last_exit": None, "last_error": "", "tail": "", "running": False}


def _kill(name, owner):
    rt = _rt.get(name)
    if rt and rt.get("jid") is not None:
        job = jobs.get_job(owner, rt["jid"])
        if job and job.alive:
            job.kill()
        rt["running"] = False
        rt["jid"] = None


def _command_line(svc):
    target = "host" if svc["target"] == "host" else svc["target"]          # host | vm:<id>
    prefix = "".join(f"export {k}={shlex.quote(str(v))}; " for k, v in (svc.get("env") or {}).items())
    return f"@{target} {prefix}{svc['cmd']}"


def _default_cwd(svc):
    return state.default_cwd() if svc["target"] == "host" else sandbox.VM_HOME


def _launch(name, svc, rt, now):
    cmdline = _command_line(svc)
    try:
        jid, _job = jobs.start_job(svc["owner"], cmdline, svc.get("cwd") or _default_cwd(svc))
    except sandbox.NeedVmChoice:
        rt["last_error"] = "no system installed yet (it will retry when your VM is ready)"
        return False
    except Exception as e:
        rt["last_error"] = f"{type(e).__name__}: {e}"
        return False
    rt.update(jid=jid, started=now, running=True, last_error="")
    return True


def _reap(name, svc, rt, job, now):
    """A supervised process just ended: record why and schedule the restart."""
    rt["running"] = False
    uptime = now - rt["started"] if rt["started"] else 0
    code = getattr(getattr(job, "proc", None), "returncode", None) if job else None
    rt["last_exit"] = code
    try:
        rt["tail"] = job.tail(20) if job else ""
    except Exception:
        rt["tail"] = ""
    if uptime >= STABLE_AFTER:
        rt["backoff"] = BACKOFF_MIN
    rt["next"] = now + rt["backoff"]
    rt["backoff"] = min(rt["backoff"] * 2, BACKOFF_MAX)
    rt["restarts"] += 1


def tick(now=None):
    """One supervisor pass: start what should run, restart what died."""
    now = time.time() if now is None else now
    if _paused:
        return
    with _lock:
        services = _read()
        for name, svc in list(services.items()):
            if not svc.get("enabled", True):
                continue
            owner = svc["owner"]
            if auth.get_user(owner) is None:
                continue
            if svc["target"] == "host" and not sandbox.host_allowed(owner):
                _rt.setdefault(name, _new_rt())["last_error"] = "host access is admin-only"
                continue
            rt = _rt.setdefault(name, _new_rt())
            if rt["running"]:
                job = jobs.get_job(owner, rt["jid"])
                if job is not None and job.alive:
                    continue
                _reap(name, svc, rt, job, now)
            if now >= rt["next"]:
                if not _launch(name, svc, rt, now):
                    rt["next"] = now + rt["backoff"]
                    rt["backoff"] = min(rt["backoff"] * 2, BACKOFF_MAX)


def info(name):
    """Runtime description of one service for the UI."""
    s = get(name)
    if not s:
        return None
    rt = _rt.get(s["name"]) or _new_rt()
    now = time.time()
    if not s.get("enabled", True):
        st = "stopped"
    elif rt["running"]:
        job = jobs.get_job(s["owner"], rt["jid"])
        st = "running" if job is not None and job.alive else "restarting"
    elif rt["restarts"] or rt["last_error"]:
        st = "backoff"
    else:
        st = "starting"
    return {**s, "state": st, "uptime": int(now - rt["started"]) if rt["running"] else 0,
            "restarts": rt["restarts"], "last_exit": rt["last_exit"],
            "last_error": rt["last_error"], "retry_in": max(0, int(rt["next"] - now)),
            "jid": rt["jid"]}


def logs(name, lines=40):
    """Recent output (live job buffer, or what the last run printed before dying)."""
    s = get(name)
    rt = _rt.get(name) or {}
    if not s:
        return ""
    job = jobs.get_job(s["owner"], rt.get("jid")) if rt.get("jid") is not None else None
    if job is not None:
        return job.tail(lines)
    return rt.get("tail") or ""


# ---------------------------------------------------------------- the thread
def _loop():
    if _stop.wait(5):                      # let a restore finish rebuilding VMs first
        return
    while not _stop.is_set():
        try:
            tick()
        except Exception as e:             # the supervisor itself must never die
            print(f"[rconsole] supervisor error: {type(e).__name__}: {e}", flush=True)
        if _stop.wait(TICK):
            return


def start_background():
    global _thread
    with _lock:
        if not config.SUPERVISOR or (_thread and _thread.is_alive()):
            return bool(_thread and _thread.is_alive())
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="rconsole-supervisor", daemon=True)
        _thread.start()
        return True


def pause():
    """Stop starting/restarting anything (shutdown in progress)."""
    global _paused
    _paused = True


def resume():
    global _paused
    _paused = False


def stop_all_processes():
    """Kill every supervised process (used before a reboot)."""
    with _lock:
        for name, s in _read().items():
            _kill(name, s["owner"])
