"""PID 1 of a VM's private PID namespace (started by :mod:`pidns`, never by hand).

It does the two jobs an init has: it reaps orphaned children (so a VM does not
fill up with zombies) and it exits on SIGTERM/SIGINT, which takes every process
of that VM down with it.
"""
import os
import signal
import sys

WAIT = {signal.SIGCHLD, signal.SIGTERM, signal.SIGINT}
signal.pthread_sigmask(signal.SIG_BLOCK, WAIT)
while True:
    if signal.sigwait(WAIT) != signal.SIGCHLD:
        sys.exit(0)
    while True:
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                break
        except ChildProcessError:
            break
