"""One private PID namespace per VM.

A proot VM is only a path-translation layer: without help every VM of every
user lives in the **server's** PID namespace, so ``ps`` shows other people's
processes and ``kill`` reaches them. This module gives each VM its own
namespace (``unshare --user --pid``), kept alive by a tiny init
(:file:`pidns_init.py`, PID 1 of the namespace). Every command of that VM —
terminals, jobs, services, hosted apps — joins it with ``nsenter``, so all of
a VM's processes see each other (PIDs start at 1) and nothing else.

Whether the host allows user namespaces is probed once. When it does not
(some containers block ``unshare``), rconsole carries on without isolation and
says so in ``vm doctor`` — see ``RCONSOLE_VM_PIDNS`` (auto | on | off).
"""
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import config

INIT = Path(__file__).with_name("pidns_init.py")
UNSHARE = ["--user", "--map-root-user", "--pid", "--fork", "--kill-child", "--mount-proc"]

_lock = threading.RLock()
_holders = {}                    # (sid, vm) -> {"proc": Popen, "pid": ns-init host pid}
_probe_cache = {}                # {"ok": bool, "reason": str}


def requested():
    return config.VM_PIDNS not in ("off", "0", "no", "false")


def probe(force=False):
    """``(ok, reason)``: can this host give a VM its own PID namespace?"""
    with _lock:
        if _probe_cache and not force:
            return _probe_cache["ok"], _probe_cache["reason"]
        ok, why = False, ""
        if not requested():
            why = "disabled (RCONSOLE_VM_PIDNS=off)"
        elif not sys.platform.startswith("linux"):
            why = "needs Linux"
        elif not (shutil.which("unshare") and shutil.which("nsenter")):
            why = "unshare/nsenter (util-linux) are not installed"
        else:
            try:
                r = subprocess.run(["unshare", *UNSHARE, "sh", "-c", "test $$ -eq 1 && echo ok"],
                                   capture_output=True, text=True, timeout=15)
                if r.stdout.strip() == "ok":
                    ok = True
                else:
                    why = (r.stderr.strip().splitlines() or ["user namespaces are not allowed here"])[-1]
            except (OSError, subprocess.SubprocessError) as e:
                why = f"{type(e).__name__}: {e}"
        _probe_cache.update(ok=ok, reason=why)
        return ok, why


def active():
    return probe()[0]


def _children(pid):
    out = []
    try:
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    with open(f"/proc/{name}/stat") as f:
                        if int(f.read().rsplit(")", 1)[1].split()[1]) == pid:
                            out.append(int(name))
                except (OSError, ValueError, IndexError):
                    pass
    except OSError:
        pass
    return out


def _ready(pid):
    """The namespace's init has exec'd and its uid/gid maps are written (joining earlier fails with EINVAL)."""
    try:
        with open(f"/proc/{pid}/gid_map") as g, open(f"/proc/{pid}/uid_map") as u:
            if not (g.read().strip() and u.read().strip()):
                return False
        return INIT.name in _cmdline(pid)
    except OSError:
        return False


def _joinable(pid):
    """Really try to enter the namespace once: the maps can be visible a moment before joining works."""
    try:
        r = subprocess.run(["nsenter", "-t", str(pid), "-U", "-p", "-m", "--preserve-credentials", "true"],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _alive(rec):
    return rec["proc"].poll() is None and os.path.exists(f"/proc/{rec['pid']}")


def _start(key):
    proc = subprocess.Popen(
        ["unshare", *UNSHARE, sys.executable, str(INIT)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        kids = _children(proc.pid)
        if kids:
            return {"proc": proc, "pid": kids[0]}
        if proc.poll() is not None:
            break
        time.sleep(0.02)
    try:
        proc.kill()
    except OSError:
        pass
    raise RuntimeError("could not start the VM's PID namespace")


def holder_pid(sid, vm):
    """Host PID of the namespace's init for this VM (started on first use)."""
    key = (sid, vm)
    with _lock:
        rec = _holders.get(key)
        if rec and _alive(rec):
            return rec["pid"]
        rec = _holders[key] = _start(key)
        return rec["pid"]


def ensure(sid, vm):
    """Make sure the VM's namespace exists. ``False`` means: run without isolation."""
    if not active():
        return False
    try:
        holder_pid(sid, vm)
        return True
    except Exception as e:
        _probe_cache.update(ok=False, reason=f"could not start a PID namespace ({e}); running without isolation")
        return False


def wrap(sid, vm, argv, cwd=None):
    """Prefix ``argv`` so it runs inside the VM's namespace (call :func:`ensure` first)."""
    pid = holder_pid(sid, vm)
    pre = ["nsenter", "-t", str(pid), "-U", "-p", "-m", "--preserve-credentials"]
    if cwd:
        pre.append(f"--wd={cwd}")
    return pre + list(argv)


def stop(sid, vm):
    """Kill every process of this VM (its namespace dies with its init)."""
    with _lock:
        rec = _holders.pop((sid, vm), None)
    if not rec:
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(rec["pid"], sig)
        except OSError:
            break
        for _ in range(20):
            if not os.path.exists(f"/proc/{rec['pid']}"):
                break
            time.sleep(0.05)
    try:
        rec["proc"].kill()
        rec["proc"].wait(timeout=2)
    except Exception:
        pass
    return True


def stop_all():
    for key in list(_holders):
        stop(*key)


def running(sid, vm):
    rec = _holders.get((sid, vm))
    return bool(rec and _alive(rec))


def status():
    ok, why = probe()
    return {"active": ok, "reason": why, "vms": len(_holders)}


# ------------------------------------------------------- listing a VM's processes
def _read_stat(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        head, rest = data.split("(", 1)
        comm, tail = rest.rsplit(")", 1)
        fields = tail.split()
        return {"comm": comm, "state": fields[0], "ppid": int(fields[1]),
                "ticks": int(fields[11]) + int(fields[12]), "rss_kb": int(fields[21]) * 4}
    except (OSError, ValueError, IndexError):
        return None


def _status_field(pid, key):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith(key + ":"):
                    return line.split(":", 1)[1].split()
    except OSError:
        pass
    return []


def _cmdline(pid, fallback=""):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        return raw or fallback
    except OSError:
        return fallback


def vm_procs(sid, vm):
    """Processes of one VM: ``[{pid, host_pid, state, rss_kb, cpu_s, cmd}]``.

    ``pid`` is what the VM itself sees (namespace PID); in the fallback mode (no namespace)
    processes are found through their proot tracer and ``pid`` is the host PID."""
    import sandbox
    root = str(sandbox.rootfs_path(sid, vm))
    hz = os.sysconf("SC_CLK_TCK")
    rows = []
    rec = _holders.get((sid, vm))
    if rec and _alive(rec):
        ns = os.readlink(f"/proc/{rec['pid']}/ns/pid")
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                if os.readlink(f"/proc/{name}/ns/pid") != ns:
                    continue
            except OSError:
                continue
            st = _read_stat(name)
            nspid = _status_field(name, "NSpid")
            cmd = _cmdline(name, st["comm"] if st else "")
            if int(name) == rec["pid"] or ("-r" in cmd.split() and root in cmd.split()):
                continue                    # the namespace's init and proot itself are plumbing, not "yours"
            if st and len(nspid) >= 2:
                rows.append({"pid": int(nspid[-1]), "host_pid": int(name), "state": st["state"],
                             "rss_kb": st["rss_kb"], "cpu_s": st["ticks"] // hz,
                             "cmd": cmd})
    else:
        tracers = set()
        for name in os.listdir("/proc"):
            if name.isdigit() and ("-r" in (c := _cmdline(name).split())) and root in c:
                tracers.add(int(name))
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            tp = _status_field(name, "TracerPid")
            if int(name) in tracers or (tp and int(tp[0]) in tracers):
                st = _read_stat(name)
                if st and int(name) not in tracers:
                    rows.append({"pid": int(name), "host_pid": int(name), "state": st["state"],
                                 "rss_kb": st["rss_kb"], "cpu_s": st["ticks"] // hz,
                                 "cmd": _cmdline(name, st["comm"])})
    return sorted(rows, key=lambda r: r["pid"])


def kill_proc(sid, vm, pid, sig=signal.SIGTERM):
    """Signal one process of this VM (by the PID :func:`vm_procs` shows). ``False`` if it is not one of its processes."""
    for r in vm_procs(sid, vm):
        if r["pid"] == pid and r["pid"] != 1:
            try:
                os.kill(r["host_pid"], sig)
                return True
            except OSError:
                return False
    return False
