"""Orderly shutdown: what has to happen when this server process is going away.

Used by ``sudo reboot`` and by the gunicorn hooks (SIGTERM on every deploy and
restart, SIGHUP reloads). In this order:

1. **pause the supervisor**, so nothing it supervises is restarted while we are
   going down (an old worker relaunching a service would keep the port busy and
   make the new worker's copy crash with "address already in use");
2. **stop every process we started** — services, background jobs, terminals,
   hosted apps — otherwise they would outlive this worker as orphans;
3. take a **final backup** (bounded, so a slow remote can't block shutdown).

It is idempotent: the first caller does the work, later callers wait for it.
"""
import threading

import backup
import jobs
import services
import state
import termsess

_lock = threading.Lock()
_done = threading.Event()
_started = False
FINAL_BACKUP_TIMEOUT = 22


def stop_everything():
    """Kill all supervised processes, jobs, terminals and hosted apps."""
    services.pause()
    services.stop_all_processes()
    for sid in {k[0] for k in list(jobs.JOBS)}:
        jobs.kill_all_jobs(sid)
    for s in list(termsess.SESSIONS.values()):
        try:
            s.kill()
        except Exception:
            pass
    for (folder, port), rec in list(state.ACTIVE_HOSTS.items()):
        try:
            state.kill_host(rec["sid"], folder, port)
        except Exception:
            pass


def begin_shutdown():
    """Run the shutdown sequence once; concurrent callers wait for it to finish."""
    global _started
    with _lock:
        first = not _started
        _started = True
    if not first:
        _done.wait(FINAL_BACKUP_TIMEOUT + 8)
        return
    try:
        stop_everything()
        backup.final(timeout=FINAL_BACKUP_TIMEOUT)
    finally:
        _done.set()


def begin_shutdown_async():
    """Start :func:`begin_shutdown` on a *non-daemon* thread.

    Non-daemon on purpose: the interpreter must not exit before the final backup
    has finished (gunicorn's own graceful timeout bounds the wait)."""
    t = threading.Thread(target=begin_shutdown, name="rconsole-shutdown", daemon=False)
    t.start()
    return t


def reset():
    """Forget a previous shutdown (tests, and a worker that is kept alive)."""
    global _started
    with _lock:
        _started = False
        _done.clear()
    services.resume()
