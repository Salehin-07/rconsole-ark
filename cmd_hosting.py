"""Process housekeeping: ``kprocess`` (kill a process by PID, or everything this session hosts).

Hosting itself lives in ``expose`` (publish a server you started) and ``service``
(run it under the supervisor); the old ``serve`` / ``gunicorn`` launchers were
removed in 1.2.2.
"""
import os
import signal

import commands
import state


@commands.command("kprocess")
def cmd_kprocess(ctx, args):
    """Kill a hosted process by PID, or all hosted processes if no PID given.

    Usage:
      sudo kprocess            -> stop every hosted process this session owns
      sudo kprocess <pid>      -> kill the process with that PID (hosted or not)
    """
    sid = ctx["sid"]

    if args:
        try:
            pid = int(args[0])
        except ValueError:
            return f"kprocess: invalid pid: {args[0]!r}"

        # Prefer a tracked hosted process (clean teardown + proxy deregister).
        sess = state.get_session(sid)
        target = None
        if sess:
            for (folder, port), proc in sess["hosts"].items():
                if proc is not None and proc.pid == pid:
                    target = (folder, port)
                    break
        if target:
            state.kill_host(sid, target[0], target[1])
            return f"Killed hosted process {pid} (port {target[1]})."
        # Not a tracked host: attempt a direct OS kill as a fallback — but never
        # the server itself (or its supervisor), which would take everyone down.
        if pid <= 1 or pid in (os.getpid(), os.getppid()):
            return f"kprocess: refusing to kill pid {pid} (the rconsole server)."
        try:
            os.kill(pid, signal.SIGTERM)
            return f"Sent SIGTERM to process {pid}."
        except ProcessLookupError:
            return f"kprocess: no process with pid {pid}."
        except Exception as e:
            return f"kprocess: could not kill pid {pid}: {e}"

    # No PID -> kill everything this session is hosting.
    sess = state.get_session(sid)
    count = len(sess.get("hosts", {})) if sess else 0
    state.kill_all_hosts(sid)
    return f"Killed all hosted processes ({count} stopped)."
