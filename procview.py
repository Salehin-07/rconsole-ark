"""A safe ``/proc`` for VMs that have no PID namespace of their own.

Where the host allows user namespaces every VM gets a private PID namespace and the real ``/proc`` (see
:mod:`pidns`). Where it does not (Render and most containers) the real ``/proc`` must NOT be handed to a VM:
every process runs as the server's uid, so ``/proc/<pid>/fd/N``, ``environ`` and friends would open other
people's files and secrets (checked: a guest could ``cat /proc/<other pid>/fd/0`` and read a file outside
its own root).

So such a VM gets a ``/proc`` that is assembled from three kinds of harmless things:

* **system-wide files and folders**, bound live from the host (``uptime``, ``meminfo``, ``stat``,
  ``vmstat``, ``diskstats``, ``filesystems``, ``sys/kernel/...`` ...): exact, current, read-only in effect;
* **a view of the VM's own processes**: for each process that belongs to this VM a *plain copy* of its
  ``stat``, ``status``, ``statm``, ``cmdline`` and ``comm`` in a private folder, refreshed every couple of
  seconds while the VM is in use. ``ps``, ``top``, ``pgrep``, ``pidof`` and ``free`` work and show only this
  VM's processes. They are ordinary files, never links into the host, so nothing leads out of the VM;
* **a ``mounts`` file** describing the VM (so ``df`` and ``mount`` have something sensible to read), and
  **``net/``** with ``tcp``, ``tcp6``, ``udp``, ``udp6`` filtered to the sockets this VM's processes hold (so
  ``netstat`` shows this VM's ports only) plus the interface tables (``dev``, ``route``).

Not available here (they need the kernel's per-process view): ``/proc/self``, ``/proc/<pid>/fd``,
``environ``, ``exe``, ``cwd``, ``root``. A host with user namespaces has all of them, safely.

The folder the VM sees is writable by the VM, so every write below is defensive: directories are opened
with ``O_NOFOLLOW`` and files are written to a temporary name and renamed into place (a rename replaces a
planted symlink instead of following it), so a VM cannot trick the server into writing outside the folder.
"""
import os
import shutil
import stat as _stat
import threading
import time
from pathlib import Path

# Harmless, host-wide information. Files and folders are bound at the same path in the VM when they exist.
SAFE_ENTRIES = (
    "uptime", "meminfo", "cpuinfo", "loadavg", "version", "stat", "vmstat", "diskstats", "filesystems",
    "partitions", "swaps", "devices", "misc", "cgroups", "pressure",
    "sys/kernel/random", "sys/kernel/hostname", "sys/kernel/osrelease", "sys/kernel/ostype",
    "sys/kernel/version", "sys/kernel/pid_max", "sys/kernel/threads-max", "sys/vm", "sys/fs/file-nr",
    "sys/fs/file-max", "sys/net/ipv4/ip_local_port_range", "sys/net/core/somaxconn",
)
PROC_FILES = ("stat", "status", "statm", "cmdline", "comm")      # copied per process
REFRESH_EVERY = 2.0           # seconds between refreshes while a VM is in use
LINGER = 90.0                 # keep refreshing this long after the VM's last process was seen
MAX_PROCS = 400               # never mirror more than this many processes of one VM

_lock = threading.Lock()
_active = {}                  # view dir -> {"root": str, "seen": float, "touched": float}
_thread = None


def view_dir(vm_dir):
    return Path(vm_dir) / "procview"


def system_binds():
    """``(host, guest)`` pairs for the live, harmless system files that exist on this host."""
    return [(f"/proc/{n}", f"/proc/{n}") for n in SAFE_ENTRIES if os.path.exists(f"/proc/{n}")]


def binds(vm_dir, root):
    """Everything to mount for one VM's ``/proc``: the process view at ``/proc`` itself, then the live
    system files on top of it. ``root`` is the VM's rootfs path (used to recognise its processes)."""
    d = view_dir(vm_dir)
    refresh(d, root)
    touch(d, root)
    return [(str(d), "/proc")] + system_binds()


NET_TABLES = ("tcp", "tcp6", "udp", "udp6")          # filtered to this VM's sockets
NET_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
NET_COPIES = ("dev", "route", "if_inet6")             # harmless interface information, copied as is


def socket_inodes(pids):
    """Inodes of the sockets the given processes hold open (read from the server, which may look)."""
    found = set()
    for p in pids:
        base = f"/proc/{p}/fd"
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for n in names:
            try:
                t = os.readlink(f"{base}/{n}")
            except OSError:
                continue
            if t.startswith("socket:["):
                found.add(t[8:-1])
    return found


def filter_table(text, inodes):
    """The header and the lines of a /proc/net/{tcp,udp}* table whose socket belongs to ``inodes``."""
    lines = text.splitlines()
    out = lines[:1]
    for ln in lines[1:]:
        f = ln.split()
        if len(f) > 9 and f[9] in inodes:
            out.append(ln)
    return ("\n".join(out) + "\n").encode()


def mounts_text():
    return ("rootfs / ext4 rw,relatime 0 0\n"
            "dev /dev devtmpfs rw,nosuid,relatime 0 0\n"
            "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"
            "home /root ext4 rw,relatime 0 0\n")


# -------------------------------------------------------------------- finding a VM's processes
def _read(path, limit=65536):
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def vm_pids(root):
    """Host PIDs of the processes that belong to the VM rooted at ``root``: the tracees of its proot
    processes (PRoot is the VM's only link to the host, so being traced by it means being in the VM)."""
    root_b = str(root).encode()
    tracers, tracer_of = set(), {}
    try:
        names = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return []
    for n in names:
        cmd = _read(f"/proc/{n}/cmdline", 4096).split(b"\0")
        if b"-r" in cmd and root_b in cmd:
            tracers.add(int(n))
    if not tracers:
        return []
    for n in names:
        for line in _read(f"/proc/{n}/status", 4096).splitlines():
            if line.startswith(b"TracerPid:"):
                try:
                    tp = int(line.split()[1])
                except (ValueError, IndexError):
                    tp = 0
                if tp:
                    tracer_of[int(n)] = tp
                break
    return sorted(p for p, t in tracer_of.items() if t in tracers and p not in tracers)[:MAX_PROCS]


# ----------------------------------------------------------------- writing the view, defensively
def _open_dir(name, dir_fd):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def _clear(name, dir_fd):
    """Remove whatever the VM left at ``name`` (a symlink is removed itself, never followed)."""
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except OSError:
        return
    try:
        if _stat.S_ISDIR(st.st_mode):
            fd = _open_dir(name, dir_fd)
            try:
                for child in os.listdir(fd):
                    _clear(child, fd)
            finally:
                os.close(fd)
            os.rmdir(name, dir_fd=dir_fd)
        else:
            os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _put(dir_fd, name, data):
    tmp = f".{name}.new"
    _clear(tmp, dir_fd)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:                      # something (a directory) is in the way: clear it and retry once
        _clear(name, dir_fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _sub(root_fd, name):
    """A real directory called ``name`` inside ``root_fd`` (created, or re-created if the VM replaced it)."""
    try:
        return _open_dir(name, root_fd)
    except OSError:
        _clear(name, root_fd)
        os.mkdir(name, 0o755, dir_fd=root_fd)
        return _open_dir(name, root_fd)


def refresh(d, root):
    """Bring the process view in ``d`` up to date. Never raises: a failed refresh leaves the old view."""
    d = Path(d)
    try:
        d.mkdir(parents=True, exist_ok=True)
        root_fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        pids = vm_pids(root)
        keep = {str(p) for p in pids} | {"mounts", "net"}
        for name in os.listdir(root_fd):       # drop vanished processes and anything the VM planted
            if name not in keep:
                _clear(name, root_fd)
        for p in pids:
            try:
                pfd = _sub(root_fd, str(p))
            except OSError:
                continue
            try:
                for fname in PROC_FILES:
                    data = _read(f"/proc/{p}/{fname}")
                    if data or fname in ("cmdline",):
                        try:
                            _put(pfd, fname, data)
                        except OSError:
                            pass
            finally:
                os.close(pfd)
        try:
            _put(root_fd, "mounts", mounts_text().encode())
        except OSError:
            pass
        try:                                   # /proc/net: only the sockets of this VM's own processes
            nfd = _sub(root_fd, "net")
        except OSError:
            nfd = None
        if nfd is not None:
            try:
                inodes = socket_inodes(pids)
                for name in NET_TABLES:
                    data = _read(f"/proc/net/{name}", 4 << 20).decode(errors="replace") or NET_HEADER
                    _put(nfd, name, filter_table(data, inodes))
                for name in NET_COPIES:
                    data = _read(f"/proc/net/{name}")
                    if data:
                        _put(nfd, name, data)
                for name in os.listdir(nfd):
                    if name not in NET_TABLES + NET_COPIES:
                        _clear(name, nfd)
            except OSError:
                pass
            finally:
                os.close(nfd)
        with _lock:
            rec = _active.get(str(d))
            if rec and pids:
                rec["seen"] = time.time()
        return True
    except OSError:
        return False
    finally:
        os.close(root_fd)


# ------------------------------------------------------------------------- keeping it fresh
def touch(d, root):
    """Mark a VM's view as in use and make sure the refresher thread runs."""
    global _thread
    now = time.time()
    with _lock:
        _active[str(d)] = {"root": str(root), "seen": now, "touched": now}
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="procview", daemon=True)
            _thread.start()


def _loop():
    while True:
        time.sleep(REFRESH_EVERY)
        with _lock:
            items = list(_active.items())
        if not items:
            continue
        for key, rec in items:
            if time.time() - rec["seen"] > LINGER:
                with _lock:
                    _active.pop(key, None)
                continue
            refresh(key, rec["root"])


def forget(vm_dir):
    """Stop refreshing and delete a VM's view (the VM is being removed)."""
    d = view_dir(vm_dir)
    with _lock:
        _active.pop(str(d), None)
    shutil.rmtree(d, ignore_errors=True)
