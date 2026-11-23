"""Keep the server's own process out of reach of VM processes.

Everything rconsole runs, VMs included, runs as the same uid, and a process may read ``/proc/<pid>/environ``,
``fd`` and ``root`` of any other process of that uid *unless* it is marked non-dumpable. The server holds the
secrets (backup token and passphrase, the session key), so it marks itself non-dumpable at start. The mark is
inherited by forked workers but reset by ``exec``, so the shells, VMs and apps it launches behave normally.
The price is that the server cannot be core-dumped or attached to by a debugger running as the same user.
"""
import sys

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


def harden():
    """Mark this process non-dumpable. ``True`` when it worked (Linux only)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except Exception:
        return False


def dumpable():
    """``True``/``False``, or ``None`` where this cannot be asked."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) == 1
    except Exception:
        return None
