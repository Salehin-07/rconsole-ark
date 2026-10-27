"""Stay awake on free hosting by pinging our own public URL.

Free tiers (Render, ...) put an instance to sleep after ~15 minutes without an
*inbound* request, which would also stop your bots and background workers. A
request to the site's **public** URL goes through the platform's proxy and counts
as inbound traffic, so a thread that fetches ``<public-url>/healthz`` every
10 minutes is enough — no external uptime monitor needed (you can still use one
as a second opinion; pointing UptimeRobot at ``/healthz`` works too).

It can also ping *other* URLs (``RCONSOLE_KEEPALIVE_EXTRA``) — handy for keeping
your other free-tier sites awake from here.

Notes
-----
* The ping always targets the public URL, never ``localhost`` (that would not
  reach the platform's proxy).
* Check your host's terms: a few providers do not like keep-alive pings. Render
  documents free-instance spin-down but does not forbid this.
"""
import random
import threading
import time

import requests

import config

_lock = threading.Lock()
_thread = None
_stop = threading.Event()
_status = {"enabled": False, "last_time": None, "last_ok": None, "last_error": "",
           "pings": 0, "failures": 0, "last_ms": None}
_enabled = None          # runtime override (None = follow config)


def targets():
    """Every URL we keep alive: our own ``/healthz`` plus any extras."""
    urls = []
    if config.PUBLIC_URL:
        urls.append(config.PUBLIC_URL.rstrip("/") + "/healthz")
    urls.extend(config.KEEPALIVE_EXTRA)
    return urls


def is_enabled():
    return config.KEEPALIVE if _enabled is None else _enabled


def ping_once(timeout=20):
    """Ping every target once. Returns ``[(url, ok, detail), ...]``."""
    results = []
    for url in targets():
        t0 = time.time()
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "rconsole-keepalive"})
            ok, detail = r.status_code < 500, f"HTTP {r.status_code}"
        except requests.RequestException as e:
            ok, detail = False, str(e)[:160]
        results.append((url, ok, detail))
        with _lock:
            _status["pings"] += 1
            _status["last_time"] = time.time()
            _status["last_ms"] = int((time.time() - t0) * 1000)
            _status["last_ok"] = ok
            if ok:
                _status["last_error"] = ""
            else:
                _status["failures"] += 1
                _status["last_error"] = f"{url}: {detail}"
    return results


def _loop():
    # Don't ping at boot: the server isn't accepting connections yet. Then go on a
    # steady beat with a little jitter so we never look like a metronome.
    if _stop.wait(min(60, config.KEEPALIVE_INTERVAL / 2)):
        return
    while not _stop.is_set():
        if is_enabled():
            ping_once()
        if _stop.wait(config.KEEPALIVE_INTERVAL * random.uniform(0.9, 1.1)):
            return


def start_background():
    """Start the ping thread (idempotent). Returns True if it is running."""
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return True
        if not config.KEEPALIVE and _enabled is not True:
            return False
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="rconsole-keepalive", daemon=True)
        _thread.start()
        return True


def set_enabled(on):
    """Turn pinging on/off at runtime (starts the thread if needed)."""
    global _enabled
    _enabled = bool(on)
    if on:
        start_background()


def stop():
    _stop.set()


def status():
    """A snapshot for the `keepalive` command."""
    with _lock:
        s = dict(_status)
    s.update(enabled=is_enabled(), interval=config.KEEPALIVE_INTERVAL, targets=targets(),
             running=bool(_thread and _thread.is_alive()), platform=config.PLATFORM,
             public_url=config.PUBLIC_URL)
    return s
