"""Active Ports — every TCP port a VM is listening on, with a one-click "Open in Proxy".

A VM is only a path-translation layer (proot): it shares the server's network, so a program inside a VM that
listens on ``:8000`` listens on the host's ``127.0.0.1:8000``. What belongs to *which* VM is decided by the
processes: :func:`pidns.vm_procs` already knows the host PIDs of one VM's processes (through its PID
namespace, or through the proot tracer where there is none). Here those PIDs are mapped to socket inodes
(``/proc/<pid>/fd``) and matched against the kernel's listening TCP tables (``/proc/net/tcp``, ``tcp6``).
Nothing is opened, probed or connected to; the lists are only read, so listing ports is cheap and safe.

The "Open in Proxy" toggle is just :mod:`exposure` (the same registry ``expose <port> <name>`` writes), so a
port opened from here shows up in ``expose list`` and the Apps page, and vice versa. A port can only be
toggled while the VM really is listening on it: you cannot publish somebody else's port by guessing a number.
"""
import os
import re
import socket
import threading
import time

import exposure
import pidns

LISTEN = "0A"                       # TCP_LISTEN in /proc/net/tcp
TABLES = (("tcp", "/proc/net/tcp"), ("tcp6", "/proc/net/tcp6"))
CACHE_TTL = 2.0                     # seconds a scan is reused (the dashboard polls every 5 s)
MAX_PORTS = 200                     # never list more than this per VM

_lock = threading.Lock()
_cache = {}                         # (sid, vm) -> (time, rows)


# ----------------------------------------------------------------- reading the kernel tables
def _addr(hexaddr, family):
    """``0100007F`` -> ``127.0.0.1`` (tcp) and the 32-digit form -> an IPv6 address (tcp6)."""
    try:
        raw = bytes.fromhex(hexaddr)
        if family == "tcp":
            return socket.inet_ntoa(raw[::-1])
        # tcp6 stores four 32-bit words, each in host (little-endian) byte order
        fixed = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
        return socket.inet_ntop(socket.AF_INET6, fixed)
    except (ValueError, OSError):
        return hexaddr


def parse_table(text, family):
    """Listening sockets of one ``/proc/net/tcp*`` table: ``[{addr, port, inode}]``."""
    rows = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 10 or f[3] != LISTEN:
            continue
        local, _, port_hex = f[1].rpartition(":")
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        rows.append({"addr": _addr(local, family), "port": port, "inode": f[9], "family": family})
    return rows


def _listening_sockets():
    out = []
    for family, path in TABLES:
        try:
            with open(path, "r") as fh:
                out += parse_table(fh.read(), family)
        except OSError:
            continue
    return out


def _inodes_of(pid):
    """``{inode}`` of the sockets one process holds (the server may look; it runs as the same user)."""
    found = set()
    base = f"/proc/{pid}/fd"
    try:
        names = os.listdir(base)
    except OSError:
        return found
    for n in names:
        try:
            t = os.readlink(f"{base}/{n}")
        except OSError:
            continue
        if t.startswith("socket:["):
            found.add(t[8:-1])
    return found


def is_loopback_or_any(addr):
    return addr in ("0.0.0.0", "::", "127.0.0.1", "::1") or addr.startswith("127.") or addr == "::ffff:127.0.0.1"


# ------------------------------------------------------------------------------- public API
def scan(sid, vm, procs=None):
    """Listening TCP ports of one VM, sorted by port.

    ``[{port, addr, proto, pid, cmd, proxyable, why, exposed}]`` where ``exposed`` is the name of the
    exposure that already points at the port (or ``None``). ``procs`` may be passed in to reuse a process
    listing the caller already has.
    """
    if procs is None:
        try:
            procs = pidns.vm_procs(sid, vm)
        except Exception:
            procs = []
    owner_of = {}                                   # inode -> process row
    for p in procs:
        for ino in _inodes_of(p["host_pid"]):
            owner_of.setdefault(ino, p)
    if not owner_of:
        return []
    mine = {e["port"]: e["name"] for e in exposure.list_for(sid)}
    server = exposure.server_port()
    seen, rows = set(), []
    for s in _listening_sockets():
        p = owner_of.get(s["inode"])
        if p is None:
            continue
        key = (s["port"], s["family"])
        if key in seen:
            continue
        seen.add(key)
        why = ""
        if s["port"] == server:
            why = "this is the console itself"
        elif exposure.validate_port(s["port"]):
            why = exposure.validate_port(s["port"])
        elif not is_loopback_or_any(s["addr"]):
            why = f"bound to {s['addr']}, which the proxy cannot reach (bind to 127.0.0.1 or 0.0.0.0)"
        rows.append({"port": s["port"], "addr": s["addr"], "proto": s["family"], "pid": p["pid"],
                     "cmd": p["cmd"][:160], "proxyable": not why, "why": why,
                     "exposed": mine.get(s["port"])})
    rows.sort(key=lambda r: (r["port"], r["proto"]))
    return rows[:MAX_PORTS]


def cached_scan(sid, vm, procs=None):
    """:func:`scan` reused for a couple of seconds (several browsers polling must not each walk ``/proc``)."""
    now = time.time()
    with _lock:
        hit = _cache.get((sid, vm))
        if hit and now - hit[0] < CACHE_TTL:
            return [dict(r) for r in hit[1]]
    rows = scan(sid, vm, procs)
    with _lock:
        _cache[(sid, vm)] = (now, rows)
        if len(_cache) > 256:                       # forget old entries; the cache is only a courtesy
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:128]:
                _cache.pop(k, None)
    return [dict(r) for r in rows]


def forget(sid=None, vm=None):
    with _lock:
        for k in [k for k in _cache if (sid is None or k[0] == sid) and (vm is None or k[1] == vm)]:
            _cache.pop(k, None)


def default_name(sid, vm, port):
    """A valid, free exposure name such as ``debian-8000`` (falls back to one with the user's name)."""
    def ok(n):
        e = exposure.get(n)
        return (not exposure.validate_name(n)) and (e is None or e["owner"] == sid)
    base = re.sub(r"[^a-z0-9-]", "-", vm.lower()).strip("-") or "vm"
    for cand in (f"{base}-{port}", f"{sid.lower()}-{base}-{port}"):
        cand = re.sub(r"[^a-z0-9-]", "-", cand)[:32].strip("-")
        if ok(cand):
            return cand
    return f"p{port}-{int(time.time()) % 100000}"


def toggle(sid, vm, port, on, name=None, actor=None):
    """Turn "Open in Proxy" on or off for one port of one VM. Returns ``(ok, message, name)``.

    ``sid`` owns the VM (and the exposure); ``actor`` is who asked (an admin may act for someone else).
    """
    if not isinstance(port, int) or isinstance(port, bool):
        return False, "port must be a number", None
    rows = scan(sid, vm)
    row = next((r for r in rows if r["port"] == port), None)
    if on:
        if row is None:
            return False, f"nothing in {vm} is listening on port {port} right now", None
        if not row["proxyable"]:
            return False, row["why"], None
        if row["exposed"]:
            return True, f"already open at /{row['exposed']}/", row["exposed"]
        chosen = (name or "").strip().lower() or default_name(sid, vm, port)
        ok, msg = exposure.add(chosen, port, sid)
        forget(sid, vm)
        return ok, (f"port {port} is open at /{chosen}/" if ok else msg), chosen if ok else None
    # off: drop every exposure of this owner that points at the port
    names = [e["name"] for e in exposure.list_for(sid) if e["port"] == port]
    if not names:
        return False, f"port {port} is not open in the proxy", None
    for n in names:
        exposure.remove(n, sid, is_admin=True)
    forget(sid, vm)
    return True, f"closed /{names[0]}/" + (f" (+{len(names) - 1} more)" if len(names) > 1 else ""), names[0]
