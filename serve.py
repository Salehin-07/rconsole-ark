"""rconsole production launcher with a fallback for systems that cannot run gunicorn.

    uv run python serve.py

* **Linux / macOS / containers (gunicorn available):** replaces this process with
  ``gunicorn -c gunicorn.conf.py app:app`` -- exactly the command the Dockerfile and
  the systemd unit use, so nothing changes in production.
* **Windows, or any system where gunicorn cannot be imported (it needs ``fcntl``, which
  Windows does not have):** serves the same ``app:app`` with Werkzeug's threaded server.
  It handles the terminal websockets, keeps one process (rconsole's state is in memory)
  and runs the orderly shutdown (stop services, final backup) on Ctrl+C / Ctrl+Break /
  SIGTERM, like the gunicorn hooks do.

``RCONSOLE_SERVER=gunicorn|builtin`` forces one of the two. ``PORT`` and ``RCONSOLE_THREADS``
mean the same in both. Use ``gunicorn -c gunicorn.conf.py app:app`` directly on Linux if you
prefer; this file only exists so one command works everywhere (``uv run python serve.py``).
"""
import importlib.util
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GUNICORN_ARGS = ["-c", "gunicorn.conf.py", "app:app"]


def gunicorn_usable():
    """True when gunicorn can really run here: installed AND the platform supports it."""
    if os.name == "nt":
        return False                         # gunicorn imports fcntl at start-up
    return importlib.util.find_spec("gunicorn") is not None


def choose_server(env=None):
    """'gunicorn' or 'builtin', honouring RCONSOLE_SERVER, else the best available."""
    env = os.environ if env is None else env
    forced = (env.get("RCONSOLE_SERVER") or "").strip().lower()
    if forced in ("builtin", "werkzeug", "dev"):
        return "builtin"
    if forced == "gunicorn":
        return "gunicorn"                    # the caller asked for it: fail loudly if it is missing
    return "gunicorn" if gunicorn_usable() else "builtin"


def run_gunicorn():
    os.chdir(ROOT)
    if not gunicorn_usable():
        sys.exit("gunicorn is not available on this system (it does not run on Windows). "
                 "Run `uv run python serve.py` without RCONSOLE_SERVER=gunicorn to use the built-in server.")
    os.execvp(sys.executable, [sys.executable, "-m", "gunicorn", *GUNICORN_ARGS])


def run_builtin():
    """Threaded Werkzeug server for systems without gunicorn (e.g. Windows)."""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from werkzeug.serving import make_server

    import lifecycle
    from app import app

    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("RCONSOLE_HOST", "0.0.0.0")
    server = make_server(host, port, app, threaded=True)
    print(f"[rconsole] gunicorn is not available here -> built-in threaded server on http://{host}:{port} "
          f"(single process; fine for testing and personal use)", flush=True)

    def stop(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):     # SIGBREAK exists on Windows only (Ctrl+Break)
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, stop)
            except (ValueError, OSError):
                pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[rconsole] shutting down ...", flush=True)
    finally:
        try:
            server.server_close()
        except Exception:
            pass
        try:
            lifecycle.begin_shutdown()       # pause supervisor, stop children, final backup
        except Exception:
            pass


def main():
    if choose_server() == "gunicorn":
        run_gunicorn()
    else:
        run_builtin()


if __name__ == "__main__":
    main()
