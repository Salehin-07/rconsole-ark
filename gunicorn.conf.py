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
graceful_timeout = 20
keepalive = 5
forwarded_allow_ips = "*"                    # the platform proxy (Render) is trusted
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("RCONSOLE_LOG_LEVEL", "info")
