"""Tiny launcher that gives a PTY child its own session *and* controlling terminal.

``subprocess.Popen(start_new_session=True)`` calls setsid() but never makes the
pty slave the controlling terminal. Shells that do job control — bash, which is
what Debian/Ubuntu/Kali use — then complain ("cannot set terminal process group
… no job control in this shell") and Ctrl+C / Ctrl+Z stop working. This helper
does what a real terminal emulator does: setsid, TIOCSCTTY on stdin, exec.

(Doing it in ``preexec_fn`` would be unsafe: the server is multithreaded.)

Usage: python -S _ptyexec.py <program> [args...]
"""
import fcntl
import os
import sys
import termios

if len(sys.argv) < 2:
    sys.exit("usage: _ptyexec.py <program> [args...]")

try:
    os.setsid()
except OSError:
    pass                       # already a session leader
try:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
except OSError:
    pass                       # stdin is not a tty: nothing to adopt
os.execvp(sys.argv[1], sys.argv[1:])
