"""``reboot`` — restart rconsole without redeploying.

::

    sudo reboot --yes           graceful restart (same container: files and VMs stay)
    sudo reboot --hard --yes    exit the process; the platform starts a fresh one

A graceful restart takes a final backup, stops every job, terminal and hosted app,
then asks the server to reload itself (gunicorn: SIGHUP; ``python main.py``: re-exec).
Your **services start again automatically** and the page reconnects. It is the
quick way to apply new environment-variable settings or recover from a wedged
state on a free host where redeploying means starting from a blank disk.

``--hard`` exits instead; hosting platforms restart the process, possibly on a
blank disk (then the last backup is restored). Use it only if a graceful restart
is not enough.
"""
import os
import signal
import sys
import threading

import commands
import lifecycle

DELAY = 1.5          # seconds: lets this reply reach the browser first


def _is_gunicorn(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"gunicorn" in f.read()
    except OSError:
        return False


def _reload():
    """Ask the server to restart itself (gunicorn: SIGHUP to the master)."""
    ppid = os.getppid()
    if _is_gunicorn(ppid):
        os.kill(ppid, signal.SIGHUP)
    else:                                   # `python main.py`: replace this process
        os.execv(sys.executable, [sys.executable] + sys.argv)


def _hard_exit():
    os._exit(3)


def perform(hard=False):
    """The actual restart sequence (runs on a timer thread after the reply):
    pause the supervisor, stop everything, final backup — then reload or exit."""
    lifecycle.begin_shutdown()
    (_hard_exit if hard else _reload)()


@commands.command("reboot")
def cmd_reboot(ctx, args):
    hard = "--hard" in args
    if "--yes" not in args:
        what = ("EXITS the process (the platform restarts it, possibly on a blank disk — "
                "the last backup is then restored)" if hard else
                "restarts rconsole in place: your files and VMs stay, services start again")
        return (f"This {what}. Running jobs and terminals end.\n"
                f"Run:  sudo reboot {'--hard ' if hard else ''}--yes")
    threading.Timer(DELAY, perform, args=(hard,)).start()
    return ("Restarting " + ("(hard) " if hard else "") +
            "in a moment — the page reconnects by itself. A final backup is taken first "
            "if backups are configured.")
