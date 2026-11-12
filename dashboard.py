"""Admin dashboard backend: resource sampler, live views, audit trail and admin actions.

Everything here is **admin only**; ``app.py`` checks that before any of it is reachable. The module reads
what Rconsole already tracks (users, jobs, services, terminals, exposures, VMs, backups ...) plus the Linux
``/proc`` and cgroup files for resource numbers, so it adds no dependency. Where a number is not available
(for example on Windows) the value is ``None`` and the page shows ``n/a``.

* :func:`start_background` runs a tiny sampler thread that keeps one hour of CPU / memory / disk / network history.
* :func:`view` returns one section of the dashboard (``overview``, ``processes`` ...), :func:`history` the graphs.
* :func:`perform` runs an admin action (create / promote / demote / delete users, stop jobs and services ...).
* :func:`audit` / :func:`recent_audit` keep a small trail of who did what (also written to ``logs/audit.jsonl``).
"""
import collections
import json
import os
import platform
import shutil
import socket
import sys
import threading
import time

import auth
import config
import exposure
import jobs
import pidns
import sandbox
import services
import state
import termsess
from version import __version__

HISTORY_MAX = 720            # samples kept (one hour at the default 5 s)
ONLINE_WINDOW = 300          # a user is "online" if seen within this many seconds
AUDIT_MAX = 500
AUDIT_FILE_MAX = 1024 * 1024
STARTED = time.time()

_lock = threading.Lock()
_hist = collections.deque(maxlen=HISTORY_MAX)
_prev = {}
_stop = threading.Event()
_thread = None

_seen = {}                   # username -> (timestamp, ip)
_audit = collections.deque(maxlen=AUDIT_MAX)
_audit_loaded = False
_audit_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _int(text):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _proc_ids():
    try:
        return [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return []


# ------------------------------------------------------------------ activity (who is online)
def touch(username, ip=""):
    """Remember that ``username`` just made a request (for the Users tab)."""
    _seen[username] = (time.time(), ip or "")


def last_seen(username):
    return _seen.get(username)


# ------------------------------------------------------------------ audit trail
def _audit_path():
    return config.LOG_DIR / "audit.jsonl"


def _load_audit():
    global _audit_loaded
    with _audit_lock:
        if _audit_loaded:
            return
        _audit_loaded = True
        text = _read(_audit_path())
        for line in (text or "").splitlines()[-AUDIT_MAX:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                _audit.append(rec)


def audit(actor, action, target="", detail="", ip="", ok=True):
    """Record one event. Never raises: auditing must not break the thing it records."""
    try:
        _load_audit()
        rec = {"t": int(time.time()), "actor": str(actor or "")[:64], "action": str(action)[:48],
               "target": str(target or "")[:64], "detail": str(detail or "")[:200],
               "ip": str(ip or "")[:64], "ok": bool(ok)}
        with _audit_lock:
            _audit.append(rec)
            path = _audit_path()
            try:
                config.LOG_DIR.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size > AUDIT_FILE_MAX:
                    os.replace(path, path.with_suffix(".jsonl.1"))
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError:
                pass
    except Exception:
        pass


def recent_audit(limit=100):
    _load_audit()
    with _audit_lock:
        return list(_audit)[-limit:][::-1]


# ------------------------------------------------------------------ resource readers
_cpu_prev = {}


def _cgroup_cpus():
    """CPUs this container may use (cgroup quota, else every CPU)."""
    txt = _read("/sys/fs/cgroup/cpu.max")
    if txt:
        quota, _, period = txt.strip().partition(" ")
        if quota != "max":
            q, p = _int(quota), _int(period)
            if q and p:
                return max(0.01, q / p)
    return float(os.cpu_count() or 1)


def _cpu_percent(now):
    """CPU use as a percentage of what this instance may use (container aware)."""
    pct = None
    txt = _read("/sys/fs/cgroup/cpu.stat")
    usec = None
    for line in (txt or "").splitlines():
        if line.startswith("usage_usec"):
            usec = _int(line.split()[1])
    if usec is not None:
        prev = _cpu_prev.get("cg")
        _cpu_prev["cg"] = (now, usec)
        if prev and now > prev[0]:
            pct = (usec - prev[1]) / 1e6 / (now - prev[0]) / _cgroup_cpus() * 100
    else:
        line = (_read("/proc/stat") or "").split("\n", 1)[0].split()
        if len(line) > 5 and line[0] == "cpu":
            vals = [int(v) for v in line[1:9] if v.isdigit()]
            total, idle = sum(vals), vals[3] + (vals[4] if len(vals) > 4 else 0)
            prev = _cpu_prev.get("proc")
            _cpu_prev["proc"] = (total, idle)
            if prev and total > prev[0]:
                pct = (1 - (idle - prev[1]) / (total - prev[0])) * 100
    return None if pct is None else round(max(0.0, min(100.0, pct)), 1)


def _memory():
    """``(used, total)`` bytes for this container (cgroup v2/v1), else the machine; ``None`` if unknown."""
    cur, mx = _int(_read("/sys/fs/cgroup/memory.current")), (_read("/sys/fs/cgroup/memory.max") or "").strip()
    if cur is not None and mx.isdigit():
        inactive = 0
        for line in (_read("/sys/fs/cgroup/memory.stat") or "").splitlines():
            if line.startswith("inactive_file "):
                inactive = _int(line.split()[1]) or 0
        return max(0, cur - inactive), int(mx)
    cur, lim = _int(_read("/sys/fs/cgroup/memory/memory.usage_in_bytes")), \
        _int(_read("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
    if cur is not None and lim and lim < (1 << 50):
        return cur, lim
    info = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            info[k] = int(parts[0]) * 1024
    if "MemTotal" in info and "MemAvailable" in info:
        return info["MemTotal"] - info["MemAvailable"], info["MemTotal"]
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total - vm.available, vm.total
    except Exception:
        return None


def _network():
    rx = tx = 0
    seen = False
    for line in (_read("/proc/net/dev") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if name.strip() != "lo" and len(f) >= 9 and f[0].isdigit() and f[8].isdigit():
            rx, tx, seen = rx + int(f[0]), tx + int(f[8]), True
    return (rx, tx) if seen else None


def _disk():
    for p in (config.DATA_DIR, config.HERE):
        try:
            u = shutil.disk_usage(p)
            return u.used, u.total
        except OSError:
            continue
    return None


def _self_rss():
    for line in (_read("/proc/self/status") or "").splitlines():
        if line.startswith("VmRSS:"):
            return (_int(line.split()[1]) or 0) * 1024
    return None


def sample(now=None):
    """Take one resource sample and add it to the history."""
    now = time.time() if now is None else now
    s = {"t": int(now), "cpu": _cpu_percent(now), "mem": None, "mem_total": None, "mem_pct": None,
         "disk": None, "disk_total": None, "disk_pct": None, "rx": None, "tx": None, "load": None,
         "procs": len(_proc_ids()) or None, "self_rss": _self_rss()}
    mem = _memory()
    if mem and mem[1]:
        s.update(mem=mem[0], mem_total=mem[1], mem_pct=round(mem[0] / mem[1] * 100, 1))
    d = _disk()
    if d and d[1]:
        s.update(disk=d[0], disk_total=d[1], disk_pct=round(d[0] / d[1] * 100, 1))
    net = _network()
    prev = _prev.get("net")
    if net:
        _prev["net"] = (now, net[0], net[1])
        if prev and now > prev[0]:
            s["rx"] = max(0, int((net[0] - prev[1]) / (now - prev[0])))
            s["tx"] = max(0, int((net[1] - prev[2]) / (now - prev[0])))
    try:
        s["load"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    c = _counts()
    s.update(terms=c["terminals"], jobs=c["jobs_running"], svc=c["services_running"])
    with _lock:
        _hist.append(s)
    return s


def _loop():
    while not _stop.is_set():
        try:
            sample()
        except Exception:
            pass
        _stop.wait(config.METRICS_INTERVAL)


def start_background():
    global _thread
    if _thread and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="dashboard-sampler", daemon=True)
    _thread.start()
    return True


def stop():
    _stop.set()


def history(minutes=60):
    """Samples of the last ``minutes`` minutes, oldest first."""
    cutoff = time.time() - max(1, minutes) * 60
    with _lock:
        rows = [s for s in _hist if s["t"] >= cutoff]
    return rows


def latest():
    with _lock:
        return dict(_hist[-1]) if _hist else sample()


# ------------------------------------------------------------------ counts
def _all_jobs():
    out = []
    for owner in list(state.STATE.keys()):
        for jid, j in jobs.list_jobs(owner):
            out.append((owner, jid, j))
    return out


def _terminals():
    with termsess.SESSIONS_LOCK:
        return [(k[0], k[1], s) for k, s in termsess.SESSIONS.items()]


def _counts():
    running_jobs = sum(1 for _o, _i, j in _all_jobs() if j.alive)
    terms = sum(1 for _o, _t, s in _terminals() if getattr(s, "alive", False))
    svc = sum(1 for s in services.list_all() if (services.info(s["name"]) or {}).get("state") == "running")
    return {"terminals": terms, "jobs_running": running_jobs, "services_running": svc,
            "services_total": len(services.list_all()), "users": len(auth.list_users()),
            "exposures": len(exposure.list_all())}


# ------------------------------------------------------------------ host processes
_proc_prev = {"t": 0.0, "ticks": {}}


def _stat(pid):
    try:
        data = _read(f"/proc/{pid}/stat")
        head, rest = data.split("(", 1)
        comm, tail = rest.rsplit(")", 1)
        f = tail.split()
        return {"comm": comm, "state": f[0], "ppid": int(f[1]), "ticks": int(f[11]) + int(f[12]),
                "rss": int(f[21]) * os.sysconf("SC_PAGE_SIZE")}
    except (AttributeError, OSError, ValueError, IndexError):
        return None


def _cmdline(pid, fallback):
    raw = _read(f"/proc/{pid}/cmdline")
    text = (raw or "").replace("\0", " ").strip()
    return (text or fallback)[:240]


def _labels():
    """host pid -> who it belongs to (job / terminal / the server), for naming process trees."""
    lab = {os.getpid(): "rconsole server"}
    for owner, jid, j in _all_jobs():
        if j.alive and j.pid:
            lab[int(j.pid)] = f"job {owner}%{jid}"
    for owner, tab, s in _terminals():
        p = getattr(getattr(s, "proc", None), "pid", None)
        if p and getattr(s, "alive", False):
            lab[int(p)] = f"terminal {owner}:{tab}"
    return lab


def host_processes(limit=30):
    """The busiest processes of this container, with CPU % since the previous call."""
    ids = _proc_ids()
    if not ids:
        return {"supported": False, "total": 0, "rows": []}
    now = time.time()
    hz = os.sysconf("SC_CLK_TCK")
    rows, ticks, parents = [], {}, {}
    for name in ids:
        st = _stat(name)
        if not st:
            continue
        pid = int(name)
        ticks[pid] = st["ticks"]
        parents[pid] = st["ppid"]
        rows.append((pid, st))
    dt = now - _proc_prev["t"] if _proc_prev["t"] else 0
    prev = _proc_prev["ticks"]
    _proc_prev.update(t=now, ticks=ticks)
    labels = _labels()

    def owner_of(pid):
        for _ in range(12):
            if pid in labels:
                return labels[pid]
            pid = parents.get(pid)
            if not pid:
                break
        return ""

    out = []
    for pid, st in rows:
        cpu = None
        if dt >= 1 and pid in prev:
            cpu = round(max(0, st["ticks"] - prev[pid]) / hz / dt * 100, 1)
        out.append({"pid": pid, "ppid": st["ppid"], "name": st["comm"], "state": st["state"], "cpu": cpu,
                    "rss": st["rss"], "cpu_s": st["ticks"] // hz, "cmd": _cmdline(pid, st["comm"]),
                    "owner": owner_of(pid)})
    out.sort(key=lambda r: ((r["cpu"] or 0), r["rss"]), reverse=True)
    return {"supported": True, "total": len(out), "rows": out[:limit]}


# ------------------------------------------------------------------ sections
def _service_rows():
    rows = []
    for s in services.list_all():
        i = services.info(s["name"]) or s
        rows.append({"name": i["name"], "owner": i["owner"], "target": i["target"], "cmd": i["cmd"],
                     "state": i.get("state"), "uptime": i.get("uptime", 0), "restarts": i.get("restarts", 0),
                     "last_exit": i.get("last_exit"), "last_error": i.get("last_error", ""),
                     "retry_in": i.get("retry_in", 0), "enabled": i.get("enabled", True),
                     "expose": i.get("expose")})
    return sorted(rows, key=lambda r: r["name"])


def _job_rows():
    now = time.time()
    rows = []
    for owner, jid, j in _all_jobs():
        rows.append({"owner": owner, "jid": jid, "cmd": j.cmd[:200], "alive": bool(j.alive), "pid": j.pid,
                     "vm": j.vm, "started": int(j.start), "runtime": int((j.end or now) - j.start)})
    return sorted(rows, key=lambda r: (not r["alive"], r["owner"], r["jid"]))


def _terminal_rows():
    rows = []
    for owner, tab, s in _terminals():
        rows.append({"owner": owner, "tab": tab, "cmd": (s.cmd or "")[:120], "vm": getattr(s, "vm", ""),
                     "alive": bool(getattr(s, "alive", False)), "clients": len(getattr(s, "clients", []) or []),
                     "pid": getattr(getattr(s, "proc", None), "pid", None)})
    return sorted(rows, key=lambda r: (r["owner"], r["tab"]))


def _vm_rows():
    rows = []
    for owner in sorted(auth.list_users()):
        ids = list(sandbox.installed_vms(owner))
        for vm in sandbox.installing(owner):
            if vm not in ids:
                ids.append(vm)
        for vm in ids:
            st = sandbox.install_state(owner, vm)
            try:
                meta = sandbox.distro_info(owner, vm) or {}
            except Exception:
                meta = {}
            procs = []
            try:
                if sys.platform != "win32" and st["state"] == "ready":
                    procs = pidns.vm_procs(owner, vm)
            except Exception:
                procs = []
            rows.append({"owner": owner, "vm": vm, "state": st["state"], "message": st.get("message", ""),
                         "distro": meta.get("label") or meta.get("id") or vm,
                         "version": str(meta.get("release") or ""),
                         "procs": procs[:60], "proc_count": len(procs)})
    return rows


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.15):
            return True
    except (OSError, ValueError):
        return False


def _app_rows():
    rows = []
    for e in exposure.list_all():
        rows.append({"name": e["name"], "port": e["port"], "owner": e["owner"], "public": bool(e["public"]),
                     "listening": _port_open(e["port"])})
    hosted = []
    for (folder, port), rec in list(state.ACTIVE_HOSTS.items()):
        proc = rec.get("proc")
        hosted.append({"folder": folder, "port": port, "owner": rec.get("sid"),
                       "alive": bool(proc and getattr(proc, "poll", lambda: 1)() is None),
                       "pid": getattr(proc, "pid", None)})
    return rows, hosted


_default_pw = {"t": 0.0, "bad": False}


def _default_password_in_use():
    """Does the factory admin (``ark``) still have the factory password? (cached: hashing is slow)"""
    if time.time() - _default_pw["t"] > 300:
        _default_pw.update(t=time.time(), bad=bool(auth.get_user(auth.DEFAULT_ADMIN)) and
                           auth.verify(auth.DEFAULT_ADMIN, auth.DEFAULT_PASSWORD))
    return _default_pw["bad"]


def _alerts(cur):
    out = []

    def add(level, text):
        out.append({"level": level, "text": text})

    with _lock:
        recent = list(_hist)[-6:]
    avg = lambda key: (sum(s[key] for s in recent if s.get(key) is not None) /  # noqa: E731
                       max(1, sum(1 for s in recent if s.get(key) is not None))) if recent else 0
    if avg("cpu") > 90:
        add("warn", f"CPU has averaged {avg('cpu'):.0f}% for the last {len(recent) * config.METRICS_INTERVAL}s.")
    if (cur.get("mem_pct") or 0) > 90:
        add("warn", f"Memory is {cur['mem_pct']:.0f}% full; the instance may be killed for using too much.")
    if (cur.get("disk_pct") or 0) > 90:
        add("warn", f"The data disk is {cur['disk_pct']:.0f}% full.")
    for s in _service_rows():
        if s["enabled"] and s["state"] in ("backoff", "restarting"):
            add("warn", f"Service '{s['name']}' ({s['owner']}) keeps stopping"
                        f"{': ' + s['last_error'] if s['last_error'] else ''}.")
    for v in _vm_rows_light():
        if v["state"] == "failed":
            add("warn", f"System {v['vm']} of {v['owner']} failed: {v['message']}")
    try:
        import backup
        b = backup.status()
        if not b.get("configured"):
            add("info", "Backups are not configured; data is lost when the instance is rebuilt.")
        for p in b.get("problems") or []:
            add("warn", f"Backup: {p}")
        if b.get("configured") and b.get("last_error"):
            add("warn", f"The last backup failed: {b['last_error']}")
    except Exception:
        pass
    if _default_password_in_use():
        add("warn", "The default admin account 'ark' still uses its factory password. Run `sudo cpass`.")
    return out


def _vm_rows_light():
    rows = []
    for owner in auth.list_users():
        for vm in sandbox.installed_vms(owner) + [v for v in sandbox.installing(owner)]:
            rows.append({"owner": owner, "vm": vm, **{k: v for k, v in sandbox.install_state(owner, vm).items()
                                                       if k in ("state", "message")}})
    return rows


def _users_view(actor):
    now = time.time()
    terms, js = _terminal_rows(), _job_rows()
    svcs, exps = _service_rows(), exposure.list_all()
    out = []
    for name in sorted(auth.list_users()):
        u = auth.get_user(name) or {}
        seen = last_seen(name)
        out.append({"name": name, "admin": bool(u.get("is_admin")), "you": name == actor,
                    "online": bool(seen and now - seen[0] < ONLINE_WINDOW),
                    "last_seen": int(seen[0]) if seen else None, "ip": seen[1] if seen else "",
                    "terminals": sum(1 for t in terms if t["owner"] == name and t["alive"]),
                    "jobs": sum(1 for j in js if j["owner"] == name and j["alive"]),
                    "services": sum(1 for s in svcs if s["owner"] == name),
                    "services_running": sum(1 for s in svcs if s["owner"] == name and s["state"] == "running"),
                    "exposures": sum(1 for e in exps if e["owner"] == name),
                    "systems": len(sandbox.installed_vms(name)),
                    "host_access": bool(sandbox.host_allowed(name))})
    return {"users": out, "max_users": config.MAX_USERS, "min_password": config.MIN_PASSWORD_LEN}


def _health_view():
    out = {"server": {"version": __version__, "python": platform.python_version(),
                      "platform": platform.platform(), "pid": os.getpid(), "threads": threading.active_count(),
                      "uptime": int(time.time() - STARTED), "host": config.PLATFORM or "self-hosted",
                      "cpus": os.cpu_count(), "data_dir": str(config.DATA_DIR), "max_users": config.MAX_USERS,
                      "metrics_interval": config.METRICS_INTERVAL},
           "pid_namespaces": None, "backup": None, "keepalive": None, "update": None}
    for key, fn in (("pid_namespaces", lambda: pidns.status()),
                    ("backup", lambda: _backup_status()),
                    ("keepalive", lambda: _keepalive_status()),
                    ("update", lambda: _update_status())):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _backup_status():
    import backup
    b = backup.status()
    return {k: b.get(k) for k in ("configured", "target", "problems", "interval", "encrypted", "running",
                                  "last_ok", "last_error", "last_time", "last_reason", "last_size") if k in b}


def _keepalive_status():
    import keepalive
    k = keepalive.status()
    return {x: k.get(x) for x in ("enabled", "interval", "running", "targets", "platform") if x in k} | \
           {"last": {a: b for a, b in k.items() if a not in ("enabled", "interval", "running", "targets",
                                                             "platform", "public_url")}}


def _update_status():
    import updater
    u = updater.status()
    return {k: u.get(k) for k in ("version", "updating", "stage", "error", "last") if k in u}


# disk usage by area (walking a big VM is slow, so it runs in the background and is cached)
_storage = {"t": 0.0, "items": [], "busy": False, "partial": False}


def _walk_size(path, deadline):
    total = 0
    stack = [path]
    while stack:
        if time.time() > deadline:
            return total, True
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, False


def _scan_storage():
    items, partial = [], False
    deadline = time.time() + 25
    try:
        tops = sorted(os.listdir(config.DATA_DIR))
    except OSError:
        tops = []
    for name in tops:
        p = config.DATA_DIR / name
        if not p.is_dir():
            continue
        if name == "vm":
            for user in sorted(os.listdir(p)):
                up = p / user
                if not up.is_dir() or user in ("bin", "cache", "tmp"):
                    if up.is_dir():
                        size, cut = _walk_size(up, deadline)
                        items.append({"path": f"vm/{user}", "bytes": size})
                        partial = partial or cut
                    continue
                for vm in sorted(os.listdir(up)):
                    if (up / vm).is_dir():
                        size, cut = _walk_size(up / vm, deadline)
                        items.append({"path": f"vm/{user}/{vm}", "bytes": size})
                        partial = partial or cut
        else:
            size, cut = _walk_size(p, deadline)
            items.append({"path": name, "bytes": size})
            partial = partial or cut
    items.sort(key=lambda r: r["bytes"], reverse=True)
    _storage.update(t=time.time(), items=items[:30], partial=partial, busy=False)


def storage(refresh=False):
    if (refresh or time.time() - _storage["t"] > 600) and not _storage["busy"]:
        _storage["busy"] = True
        threading.Thread(target=_scan_storage, daemon=True, name="dashboard-storage").start()
    return {"items": _storage["items"], "age": int(time.time() - _storage["t"]) if _storage["t"] else None,
            "busy": _storage["busy"], "partial": _storage["partial"]}


SECTIONS = ("overview", "processes", "users", "systems", "apps", "activity", "health")


def view(section, actor=""):
    """One dashboard section as JSON-ready data (``ValueError`` for an unknown section)."""
    if section == "overview":
        cur = latest()
        return {"current": cur, "counts": _counts(), "alerts": _alerts(cur),
                "server": {"version": __version__, "uptime": int(time.time() - STARTED),
                           "host": config.PLATFORM or "self-hosted", "cpus": os.cpu_count()},
                "users_online": sum(1 for u in auth.list_users()
                                    if (_seen.get(u) and time.time() - _seen[u][0] < ONLINE_WINDOW))}
    if section == "processes":
        return {"services": _service_rows(), "jobs": _job_rows(), "terminals": _terminal_rows(),
                "host": host_processes()}
    if section == "users":
        return _users_view(actor)
    if section == "systems":
        return {"systems": _vm_rows(), "storage": storage(), "pid_namespaces": pidns.status()}
    if section == "apps":
        exps, hosted = _app_rows()
        return {"exposures": exps, "hosted": hosted}
    if section == "activity":
        return {"events": recent_audit(150)}
    if section == "health":
        return _health_view()
    if section == "settings":
        import settings
        return settings.snapshot()
    if section == "repo":
        return _repo_view()
    raise ValueError(f"unknown section '{section}'")


def log_tail(kind, owner="", ident="", lines=200):
    """Recent output of a job or service."""
    lines = max(10, min(int(lines or 200), 1000))
    if kind == "job":
        j = jobs.get_job(owner, _int(ident))
        return j.tail(lines) if j else None
    if kind == "service":
        return services.logs(ident, lines) if services.get(ident) else None
    return None


# ------------------------------------------------------------------ admin actions
def _need(data, key):
    v = data.get(key)
    return v.strip() if isinstance(v, str) else v


def remove_user_everything(name):
    """Delete an account and everything it owns (shared by ``sudo ruser`` and the dashboard)."""
    import cmd_users
    return cmd_users.remove_user(name)


def _repo_view():
    import gitstore
    import settings
    import backup
    snap = {s["env"]: s for g in settings.snapshot()["groups"] for s in g["settings"]}
    return {"status": gitstore.status(),
            "repo": {"url": gitstore._scrub(config.BACKUP_URL), "token_set": bool(config.BACKUP_TOKEN),
                     "passphrase_set": bool(config.BACKUP_PASSPHRASE), "branch": config.STORAGE_BRANCH,
                     "backup_branch": config.BACKUP_BRANCH},
            "backup": backup.status() if hasattr(backup, "status") else {},
            "settings": {k: snap[k] for k in snap if k.startswith("RCONSOLE_STORAGE")},
            "restart_needed": settings.pending_restart()}


def _threaded(fn, name):
    threading.Thread(target=fn, daemon=True, name=name).start()


def _perform_settings(actor, action, data):
    """Settings and repo-storage actions. Returns ``(ok, message, target)`` or ``None`` if not ours."""
    import settings
    import gitstore
    if action == "settings.save":
        changes = data.get("changes")
        ok, msg, _r = settings.set_values(changes if isinstance(changes, dict) else {}, actor)
        return ok, msg, "settings"
    if action == "settings.reset":
        names = data.get("names")
        if not isinstance(names, list) or not names:
            return False, "nothing selected", "settings"
        ok, msg, _r = settings.set_values({str(n): None for n in names}, actor)
        return ok, ("reset: " + msg.split(": ", 1)[-1]) if ok else msg, "settings"
    if action == "server.restart":
        import cmd_reboot
        threading.Timer(cmd_reboot.DELAY, cmd_reboot.perform).start()
        return True, "restarting: the page reconnects in a few seconds", ""
    if action == "repo.configure":
        ch = {}
        url = (data.get("url") or "").strip()
        if "url" in data:
            ch["RCONSOLE_BACKUP_URL"] = url
        for field, env in (("token", "RCONSOLE_BACKUP_TOKEN"), ("passphrase", "RCONSOLE_BACKUP_PASSPHRASE")):
            val = data.get(field)
            if isinstance(val, str) and val != "":
                ch[env] = val
            if data.get("clear_" + field):
                ch[env] = None
        if data.get("branch"):
            ch["RCONSOLE_STORAGE_BRANCH"] = str(data["branch"]).strip()
        if not ch:
            return False, "nothing to change", "repo"
        ok, msg, _r = settings.set_values(ch, actor)
        return ok, msg, "repo"
    if action == "repo.mode":
        mode = str(data.get("mode") or "")
        if mode not in ("host", "github"):
            return False, "mode must be host or github", "repo"
        ok, msg, _r = settings.set_values({"RCONSOLE_STORAGE": mode}, actor)
        if not ok:
            return False, msg, "repo"
        probs = gitstore.problems()
        if mode == "github" and probs:
            return True, "saved, but files stay on the host for now: " + "; ".join(probs), "repo"
        if mode == "github":
            return True, "files now live in the repo (the first upload runs in the background)", "repo"
        return True, "files live on the host again; bringing any placeholders back to this disk", "repo"
    if action == "repo.encrypt":
        on = bool(data.get("on"))
        ok, msg, _r = settings.set_values({"RCONSOLE_STORAGE_ENCRYPT": "1" if on else "0"}, actor)
        if not ok:
            return False, msg, "repo"
        if data.get("rewrite") and gitstore.active():
            _threaded(lambda: gitstore.reencode(), "dashboard-reencode")
            return True, f"encryption {'on' if on else 'off'}; rewriting stored files in the background", "repo"
        return True, f"encryption {'on' if on else 'off'} for new files (use 'rewrite everything' to convert old ones)", "repo"
    if action == "repo.sync":
        if not gitstore.active():
            return False, "repo storage is off or not set up: " + ("; ".join(gitstore.problems()) or "mode is host"), "repo"
        _threaded(lambda: gitstore.sync_once("dashboard", force=True), "dashboard-sync")
        return True, "sync started", "repo"
    if action == "repo.hydrate":
        _threaded(lambda: gitstore.hydrate_all(), "dashboard-hydrate")
        return True, "bringing every file back to this disk", "repo"
    if action == "repo.test":
        probs = gitstore.problems()
        if probs:
            return False, "; ".join(probs), "repo"
        try:
            with gitstore.Reader() as rd:
                if not rd.tip:
                    return True, "repository reachable; nothing is stored there yet", "repo"
                gitstore._load_remote(rd)
                return True, "repository reachable and readable", "repo"
        except gitstore.StoreError as e:
            return False, gitstore._scrub(str(e)), "repo"
    return None


def perform(actor, action, data, ip=""):
    """Run one admin action. Returns ``(ok, message)``; every attempt is audited."""
    ok, msg, target = _perform(actor, action, data)
    audit(actor, action, target, msg, ip=ip, ok=ok)
    return ok, msg


def _perform(actor, action, data):          # noqa: C901 - a flat dispatch table reads best
    if action.startswith(("settings.", "repo.")) or action == "server.restart":
        res = _perform_settings(actor, action, data)
        if res is not None:
            return res
    user = _need(data, "username") or ""
    # ---- accounts
    if action == "user.create":
        ok, err = auth.create_user(user, data.get("password") or "", bool(data.get("admin")))
        return ok, (f"created {'admin ' if data.get('admin') else ''}user '{user}'" if ok else err), user
    if action in ("user.promote", "user.demote"):
        if user == actor:
            return False, "you cannot change your own role here (use sudo auser / nauser)", user
        up = action == "user.promote"
        ok, err = auth.set_admin(user, up)
        return ok, (f"'{user}' is now {'an admin' if up else 'a normal user'}" if ok else err), user
    if action == "user.delete":
        if user == actor:
            return False, "you cannot delete your own account here (use sudo ruser)", user
        if sandbox.updating(user):
            return False, f"{user}'s system is being updated; try again when it has finished", user
        ok, err = remove_user_everything(user)
        return ok, (f"deleted user '{user}'" if ok else err), user
    if action == "user.password":
        if user == actor:
            return False, "use sudo cpass to change your own password", user
        ok, err = auth.set_password(user, data.get("password") or "")
        return ok, (f"password changed for '{user}'" if ok else err), user
    if action == "user.stop":
        if user == actor:
            return False, "that would stop your own console", user
        if auth.get_user(user) is None:
            return False, "No such user", user
        jobs.kill_all_jobs(user)
        state.kill_all_hosts(user)
        termsess.kill_all_for(user)
        stopped = services.stop_for(user)
        return True, f"stopped everything of '{user}'" + (f" (services stay enabled: {', '.join(stopped)})"
                                                         if stopped else ""), user
    # ---- processes
    if action == "job.kill":
        owner, jid = _need(data, "owner"), _int(data.get("jid"))
        if jid is None or not jobs.kill_job(owner, jid):
            return False, "no such job", f"{owner}%{jid}"
        return True, f"killed job %{jid} of {owner}", f"{owner}%{jid}"
    if action in ("service.start", "service.stop", "service.restart", "service.remove"):
        name = (_need(data, "name") or "").lower()
        if action == "service.restart":
            ok, msg = services.restart(name, actor, True)
        elif action == "service.remove":
            ok, msg = services.remove(name, actor, True)
        else:
            ok, msg = services.set_enabled(name, actor, True, action == "service.start")
        return ok, (f"service '{name}': {msg}" if ok else msg), name
    if action == "term.kill":
        owner, tab = _need(data, "owner"), _need(data, "tab")
        with termsess.SESSIONS_LOCK:
            sess = termsess.SESSIONS.pop((owner, tab), None)
        if sess is None:
            return False, "no such terminal", f"{owner}:{tab}"
        sess.kill()
        return True, f"closed terminal {tab} of {owner}", f"{owner}:{tab}"
    if action == "vmproc.kill":
        owner, vm, pid = _need(data, "owner"), _need(data, "vm"), _int(data.get("pid"))
        if pid is None or not pidns.kill_proc(owner, vm, pid):
            return False, "no such process in that system", f"{owner}/{vm}:{pid}"
        return True, f"sent SIGTERM to {pid} in {owner}/{vm}", f"{owner}/{vm}:{pid}"
    # ---- apps and backups
    if action == "expose.remove":
        name = (_need(data, "name") or "").lower()
        ok, msg = exposure.remove(name, actor, is_admin=True)
        return ok, (f"removed /{name}/" if ok else msg), name
    if action == "backup.now":
        import backup
        if not backup.configured():
            return False, "backups are not configured (see `sudo backup`)", ""
        threading.Thread(target=lambda: backup.backup_now(force=True, reason="dashboard"), daemon=True,
                         name="dashboard-backup").start()
        return True, "backup started", ""
    if action == "storage.rescan":
        storage(refresh=True)
        return True, "disk usage is being measured", ""
    return False, f"unknown action '{action}'", ""
