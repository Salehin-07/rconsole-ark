"""Gunicorn settings for rconsole (``gunicorn -c gunicorn.conf.py app:app``).

rconsole keeps sessions, terminal PTYs, background jobs and the hosted-app
registry in this process's memory, so it MUST run exactly one worker process.
Concurrency comes from threads instead: every open terminal websocket holds one
thread for as long as the tab is connected, so size ``RCONSOLE_THREADS`` for the
number of terminals you expect open at once (default 64).
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
workers = 1                                  # in-memory state: never raise this
worker_class = "gthread"                     # flask-sock websockets need threads
threads = int(os.environ.get("RCONSOLE_THREADS", "64"))
timeout = 120                                # worker-heartbeat; requests may run longer
graceful_timeout = 30                         # room for the final backup on shutdown
# No upstream keep-alive: an idle client connection otherwise holds a graceful shutdown for the
# whole graceful_timeout (measured: 30 s -> 0.3 s). The platform proxy in front pools connections.
keepalive = 0
forwarded_allow_ips = "*"                    # the platform proxy (Render) is trusted
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("RCONSOLE_LOG_LEVEL", "info")


def post_worker_init(worker):
    """On SIGTERM (every deploy/restart): let gunicorn start stopping, and *at the same time*
    pause the supervisor, kill what we started and take the final backup."""
    import signal
    original = worker.handle_exit

    def handle_exit(sig, frame):
        original(sig, frame)
        try:
            import lifecycle
            lifecycle.begin_shutdown_async()
        except Exception:                   # never get in the way of shutting down
            pass

    signal.signal(signal.SIGTERM, handle_exit)


def worker_exit(server, worker):
    """Last hook before the worker process ends: make sure the shutdown work finished."""
    try:
        import lifecycle
        lifecycle.begin_shutdown()          # no-op if the SIGTERM handler already did it
    except Exception:
        pass
