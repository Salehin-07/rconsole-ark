"""rconsole — a Flask-powered simulated Linux console with hosted apps.

Request flow: ``before_request`` runs an Origin (CSRF) check, then ``guard``
(authentication, inactivity timeout, forced password change). Command and file
access is authorized by :mod:`sandbox`, never by this module, so every entry
point — the line console, the terminal websocket and the file APIs — enforces
the same rules.
"""
import _pathfix  # noqa: F401  (first: makes dependencies installed by `rconsole update` importable)
import functools
import gzip
import logging
import os
import posixpath
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from datetime import timedelta
from urllib.parse import urlsplit

import requests
from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from flask_sock import Sock
from requests.adapters import HTTPAdapter
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import bootstrap  # noqa: F401  (must be first: restores a backup before secrets are created)
import auth
import backup
import config
import exposure
import keepalive
import services
import interp
import jobs
import ptyhost
import sandbox
import state
import termsess
import updater
from version import __version__

app = Flask(__name__)
app.secret_key = config.persistent_secret("RCONSOLE_SECRET", "secret.key")
app.config.update(
    # A distinct cookie name: hosted Flask apps proxied under this origin also
    # default to a cookie called "session", and the two would collide.
    SESSION_COOKIE_NAME="rconsole_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
    # Static assets change only on deploy; let browsers cache them for a week.
    SEND_FILE_MAX_AGE_DEFAULT=7 * 24 * 3600,
    # Bounds request bodies (editor saves, file-browser uploads, proxied uploads).
    MAX_CONTENT_LENGTH=max(64, config.MAX_UPLOAD_MB + 1) * 1024 * 1024,
)
# Long-lived sessions: a user should be able to return days later and log back
# in to the exact same state. Forced logout only clears the cookie; server-side
# state is keyed by username and survives it.
app.permanent_session_lifetime = timedelta(days=360)
if config.TRUST_PROXY:
    # Behind Render/nginx: trust one proxy hop so request.remote_addr, the
    # scheme and the host reflect the real client (needed for login throttling
    # and the Origin check).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

SESSION_TIMEOUT = 30 * 24 * 3600  # seconds of inactivity before forced re-login
MAX_EDIT_BYTES = 5 * 1024 * 1024  # largest file the browser editor will open
MAX_DIR_ENTRIES = 5000
HOSTS = state.ACTIVE_HOSTS
sock = Sock(app)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

# Per-(ip, username) throttle, plus a looser per-ip one against username spraying.
USER_THROTTLE = auth.THROTTLE
IP_THROTTLE = auth.LoginThrottle(max_attempts=config.LOGIN_MAX_ATTEMPTS * 4)

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# Endpoints a user may reach while they still have to change the default password.
_MUST_CHANGE_OK = {"run", "console_page", "static", "login", "api_tabs", "healthz"}
_MUST_CHANGE_CMDS = {"cpass", "logout", "help", "clear", "cls", "rwhoami", "history"}


# ---------------------------------------------------------------------------
# Response optimizations: gzip compress text responses (HTML/JS/CSS/JSON) and
# add basic security/optimization headers. Skips websocket upgrades and
# responses that are already encoded (e.g. proxied gzip from a hosted app).
# Compressed static assets are cached (keyed by path + ETag) so a repeat
# request costs a dict lookup instead of re-running gzip.
# ---------------------------------------------------------------------------
_GZIP_TYPES = (
    "text/", "javascript", "json", "css", "html", "svg", "xml", "font",
)
_GZIP_THRESHOLD = 1024
_GZ_CACHE = {}
_GZ_CACHE_MAX = 64
_GZ_LOCK = threading.Lock()


@app.after_request
def optimize_response(resp):
    # Basic hardening headers (cheap, helps caching/proxy behaviour).
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    # same-origin (not no-referrer): keeps links private from other sites but lets
    # the browser send a normal Origin header on our own form posts.
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    # The console is never meant to be framed by another site (clickjacking).
    # Hosted apps may set their own value; setdefault leaves theirs alone.
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if config.COOKIE_SECURE:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")

    # Never touch the websocket upgrade or already-compressed payloads.
    if resp.status_code == 101 or resp.headers.get("Content-Encoding"):
        return resp

    accept = request.headers.get("Accept-Encoding", "")
    if "gzip" not in accept.lower():
        return resp

    ctype = resp.headers.get("Content-Type", "") or ""
    if not any(t in ctype for t in _GZIP_TYPES):
        return resp

    etag = resp.headers.get("ETag") if request.endpoint == "static" else None
    cache_key = (request.path, etag) if etag else None
    compressed = None
    if cache_key:
        with _GZ_LOCK:
            compressed = _GZ_CACHE.get(cache_key)
    if compressed is None:
        try:
            # Flask serves static files with direct_passthrough; flip it so we
            # can read (and replace) the body for compression.
            resp.direct_passthrough = False
            data = resp.get_data()
        except Exception:
            return resp
        if data is None or len(data) < _GZIP_THRESHOLD:
            return resp
        compressed = gzip.compress(data, 6)
        if cache_key:
            with _GZ_LOCK:
                if len(_GZ_CACHE) >= _GZ_CACHE_MAX:
                    _GZ_CACHE.clear()
                _GZ_CACHE[cache_key] = compressed
    resp.direct_passthrough = False
    resp.set_data(compressed)
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Content-Length"] = str(len(compressed))
    resp.headers["Vary"] = "Accept-Encoding"
    return resp


def restore_terminal_sessions(username):
    """Re-spawn terminal tabs whose process is no longer alive (e.g. after a
    server restart). Skipped while the process is already running, so an active
    session is never duplicated."""
    sess = state.get_session(username)
    if not sess:
        return
    for tid, t in list(sess["tabs"].items()):
        if t.get("isTerminal") and t.get("cmd"):
            if termsess.get(username, tid) is None:
                try:
                    s = termsess.TermSession(username, tid, t["cmd"], t["cwd"])
                    with termsess.SESSIONS_LOCK:
                        termsess.SESSIONS[(username, tid)] = s
                except Exception:
                    # Not permitted any more (e.g. admin revoked) or the VM is
                    # gone: leave the tab dormant rather than failing the request.
                    pass


# ---------------------------------------------------------------------------
# Request guards
# ---------------------------------------------------------------------------
def _json_error(msg, code):
    return {"error": msg, "code": code}, code


def _own_hosts():
    """Hostnames this console is legitimately served from.

    Behind a proxy the ``Host`` the app sees can differ from the one in the
    browser's ``Origin``, so also accept the proxy's ``X-Forwarded-Host``,
    Render's public hostname and any ``RCONSOLE_ALLOWED_ORIGINS``. Trusting
    ``X-Forwarded-Host`` here is safe for CSRF: a cross-site page cannot add
    custom request headers, so it cannot forge it.
    """
    hosts = {request.host.lower(), *config.ALLOWED_ORIGINS}
    fwd = request.headers.get("X-Forwarded-Host", "")
    hosts.update(h.strip().lower() for h in fwd.split(",") if h.strip())
    hosts.update(config.PLATFORM_HOSTS)   # e.g. rconsole.onrender.com, app.fly.dev
    return hosts


_UPDATE_OK = {"healthz", "update_status", "static"}
_UPDATE_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5"><title>rconsole is updating</title>
<body style="margin:0;display:grid;place-items:center;min-height:100vh;background:#0b0f14;color:#d6e2ef;font:16px system-ui">
<div style="max-width:30em;padding:2em;text-align:center"><h2>rconsole is updating</h2>
<p>Nothing else can be done until the update is finished. This page reloads by itself.</p>
<p style="opacity:.6">{what}</p></div>"""


@app.before_request
def update_gate():
    """While ``rconsole update`` runs, the console accepts nothing: no command, terminal, file operation,
    login or hosted app. Only the health probe, static files and the status endpoint answer."""
    if not updater.is_locked() or request.endpoint in _UPDATE_OK:
        return None
    st = updater.status()
    headers = {"Retry-After": "10", "X-Rconsole-Updating": "1", "Cache-Control": "no-store"}
    wants_html = (request.method == "GET" and not request.path.startswith("/api")
                  and "text/html" in request.headers.get("Accept", "")
                  and request.headers.get("Upgrade", "").lower() != "websocket")
    if wants_html:
        what = f"{st['mode']} {st['from']} → {st['to']}: {st['stage']}"
        return Response(_UPDATE_PAGE.replace("{what}", what), 503, headers=headers, mimetype="text/html")
    body = jsonify(error="rconsole is being updated; nothing else can be done until it finishes",
                   updating=True, stage=st["stage"], pct=st["pct"])
    body.status_code = 503
    body.headers.update(headers)
    return body


@app.get("/api/update/status")
def update_status():
    """Progress of a running update (also tells a freshly restarted server's last result)."""
    resp = jsonify(updater.status(detail="username" in session))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.before_request
def origin_check():
    """CSRF defence: reject cross-site state-changing requests and websockets.

    Browsers always attach ``Origin`` (or ``Sec-Fetch-Site``) to cross-site
    POSTs and websocket handshakes. Combined with ``SameSite=Lax`` cookies this
    stops a malicious page from driving a logged-in console. Requests with no
    such headers (curl, server-to-server) are not browser-forgeable and pass.
    Proxied hosted apps do their own CSRF handling and are exempt.
    """
    is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
    # Hosted/exposed apps do their own CSRF handling — but a websocket handshake is
    # never exempt (the console's own /ws terminal is one, and proxies don't carry
    # websockets anyway), so a cross-site page can't reach it through a proxy route.
    if (request.endpoint or "").startswith("proxy") and not is_ws:
        return None
    if request.method in _SAFE_METHODS and not is_ws:
        return None
    # Sec-Fetch-Site is set by the browser itself and cannot be forged by a web
    # page, so when present it is authoritative. It also copes with proxies that
    # rewrite Host and with `Origin: null`, which browsers send for same-origin
    # form posts when the page has `Referrer-Policy: no-referrer`.
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site in ("same-origin", "none"):
        return None
    if fetch_site in ("cross-site", "same-site"):
        return _json_error("cross-site request blocked", 403)
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        netloc = urlsplit(origin).netloc.lower()
        if netloc not in _own_hosts():
            app.logger.warning(
                "blocked cross-origin %s %s: Origin=%s Host=%s X-Forwarded-Host=%s "
                "(set RCONSOLE_ALLOWED_ORIGINS=%s if this is your own site)",
                request.method, request.path, netloc, request.host,
                request.headers.get("X-Forwarded-Host"), netloc)
            return _json_error("cross-origin request blocked", 403)
    elif origin == "null":
        return _json_error("cross-origin request blocked", 403)
    return None


def _public_exposure_request():
    """Is this request for an `expose --public` app (viewable without logging in)?"""
    ep = request.endpoint or ""
    va = request.view_args or {}
    if ep in ("proxy_named_bare", "proxy_named", "proxy_named_rest"):
        name = va.get("name")
    elif ep in ("proxy_noslash", "proxy_slash", "proxy_rest"):
        # /<name>/<digits>/… is an exposure only when no hosted app owns that path.
        if (va.get("folder"), va.get("port")) in HOSTS:
            return False
        name = va.get("folder")
    else:
        return False
    e = exposure.get(name or "")
    return bool(e and e["public"])


@app.before_request
def guard():
    # Allow login page, health probe and static assets without a session.
    if request.endpoint in ("login", "static", "healthz"):
        return
    if _public_exposure_request():
        return
    if "username" not in session:
        endpoint = request.endpoint or ""
        if endpoint in ("console_page", "run") or endpoint.startswith("proxy"):
            if endpoint == "run" or request.path.startswith("/api"):
                return {"error": "unauthorized"}, 401
            return redirect(url_for("login"))
        return
    # Enforce inactivity timeout (only forces a re-login; user state persists).
    last = session.get("last_activity", 0)
    if time.time() - float(last) > SESSION_TIMEOUT:
        session.clear()
        return redirect(url_for("login"))
    session["last_activity"] = time.time()

    # If the account no longer exists (e.g. after a global rboot), force login.
    if auth.get_user(session["username"]) is None:
        session.clear()
        return redirect(url_for("login"))

    # The factory password must be replaced before anything else is usable.
    if session.get("must_change") and request.endpoint not in _MUST_CHANGE_OK:
        return _json_error("password change required: run `sudo cpass`", 403)

    # Ensure server-side session state exists, then bring any persisted terminal
    # sessions (that aren't already running) back to life.
    username = session["username"]
    if username not in state.STATE:
        state.new_session(username)
    if request.endpoint in ("console_page", "run", "api_tabs"):
        restore_terminal_sessions(username)


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness probe (platform health checks)."""
    return {"ok": True, "version": __version__}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    status = 200
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        ip = request.remote_addr or "?"
        user_key, ip_key = (ip, username.lower()), (ip, "*")
        wait = max(USER_THROTTLE.retry_after(user_key), IP_THROTTLE.retry_after(ip_key))
        if wait:
            error = f"Too many failed attempts. Try again in {wait}s."
            status = 429
        elif auth.verify(username, password):
            USER_THROTTLE.success(user_key)
            IP_THROTTLE.success(ip_key)
            session.clear()
            session["username"] = username
            session["login_time"] = time.time()
            session["last_activity"] = time.time()
            session["must_change"] = auth.is_default_credential(username, password)
            session.permanent = True
            app.logger.info("login ok user=%s ip=%s", username, ip)
            # Keyed by username: a returning user is restored to their old state.
            state.new_session(username)
            restore_terminal_sessions(username)
            return redirect(url_for("console_page"))
        else:
            USER_THROTTLE.failure(user_key)
            IP_THROTTLE.failure(ip_key)
            app.logger.warning("login failed user=%r ip=%s", username[:32], ip)
            error = "Invalid username or password."
            status = 401
    resp = Response(render_template("login.html", error=error, default_user="ark"), status=status)
    if status == 429:
        resp.headers["Retry-After"] = str(wait)
    return resp


@app.route("/")
@app.route("/console")
def console_page():
    if "username" not in session:
        return redirect(url_for("login"))
    sess = state.get_session(session["username"])
    theme = sess.get("theme", state.DEFAULT_THEME) if sess else state.DEFAULT_THEME
    # Cache-bust static assets so the browser always picks up new JS/CSS.
    js_path = os.path.join(os.path.dirname(__file__), "static", "console.js")
    try:
        cache_v = int(os.path.getmtime(js_path))
    except OSError:
        cache_v = 1
    return render_template(
        "console.html", username=session["username"], theme=theme, cache_v=cache_v
    )


def _first_word(raw):
    parts = (raw or "").split()
    if parts and parts[0] == "sudo":
        parts = parts[1:]
    return parts[0] if parts else ""


@app.route("/api/run", methods=["POST"])
def run():
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    data = request.get_json(silent=True) or {}
    raw = data.get("cmd", "")
    tab_id = data.get("tab", "main")
    if not isinstance(raw, str) or not isinstance(tab_id, str) or len(tab_id) > 64:
        return {"error": "bad request"}, 400

    username = session["username"]
    sess = state.get_session(username) or state.new_session(username)
    tab = state.ensure_tab(sess, username, tab_id)
    is_admin = auth.is_admin(username)

    # Until the default password is replaced only a few commands may run (a
    # pending interactive prompt, e.g. cpass asking for the new password, must
    # still receive its answers).
    if session.get("must_change") and tab.get("gen") is None:
        if _first_word(raw) not in _MUST_CHANGE_CMDS:
            return {"output": "You are using the default password. "
                              "Run `sudo cpass` to set a new one first.",
                    "cwd": tab["cwd"]}

    # Record history only for real commands (not interactive prompt replies).
    # Mutating tab state under STATE_LOCK so the background flush thread can't
    # read a half-updated tab while serializing it.
    if tab.get("gen") is None and raw.strip():
        with state.STATE_LOCK:
            tab["history"].append(raw)
            if len(tab["history"]) > 500:
                tab["history"] = tab["history"][-500:]
            state.save(username)

    # The browser reports how many characters fit across its output area so
    # commands can adapt (neofetch on a phone vs a desktop).
    cols = data.get("cols")
    cols = max(20, min(int(cols), 300)) if isinstance(cols, (int, float)) and not isinstance(cols, bool) else None

    result = interp.process(tab, username, is_admin, username, raw, cols=cols)
    if isinstance(result, dict):
        result["cwd"] = tab["cwd"]
    else:
        result = {"output": result, "cwd": tab["cwd"]}
    # Name shown in the prompt: `rconsole` on the host, else the VM's name.
    result["target"] = sandbox.target_label(username)

    # Apply session updates requested by a command (e.g. cuser, cpass).
    if result.get("update_session"):
        for k, v in result["update_session"].items():
            session[k] = v

    if result.get("action") == "logout":
        # Logout only clears the cookie. Terminal sessions are keyed by username
        # and deliberately kept alive so the user can log back in and resume.
        session.clear()
        result["redirect"] = url_for("login")

    return result


# ---------------------------------------------------------------------------
# Hosted-app proxy:  /<folder>/<port>/...  ->  http://127.0.0.1:<port>/...
# ---------------------------------------------------------------------------
_tls = threading.local()
_HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive", "te",
               "trailers", "transfer-encoding", "upgrade", "proxy-authorization",
               "proxy-authenticate", "cookie"}


def _upstream():
    """A per-thread pooled HTTP session (avoids a new TCP connect per request)."""
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16)
        s.mount("http://", adapter)
        _tls.session = s
    return s


def _forward_cookies():
    """Cookies for the hosted app: everything except rconsole's own session."""
    return {k: v for k, v in request.cookies.items()
            if k != app.config["SESSION_COOKIE_NAME"]}


_REWRITE_TYPES = ("text/html", "application/xhtml", "text/css")


@functools.lru_cache(maxsize=64)
def _rewrite_patterns(prefix):
    """Regexes that put ``prefix`` in front of root-relative URLs (not yet prefixed)."""
    skip = re.escape(prefix.lstrip("/")) + "/"
    attr = re.compile(
        r"""(\b(?:href|src|action|formaction|poster|data-src|data-href)\s*=\s*)(["'])/(?!/|%s)""" % skip,
        re.I)
    css = re.compile(r"""(url\(\s*)(["']?)/(?!/|%s)""" % skip, re.I)
    return attr, css


def _rewrite_body(content, charset, prefix):
    """Best-effort: ``href="/x"`` -> ``href="/name/x"`` and ``url(/x)`` likewise."""
    text = content.decode(charset or "utf-8", errors="replace")
    attr, css = _rewrite_patterns(prefix)
    text = attr.sub(lambda m: f"{m.group(1)}{m.group(2)}{prefix}/", text)
    text = css.sub(lambda m: f"{m.group(1)}{m.group(2)}{prefix}/", text)
    return text.encode(charset or "utf-8", errors="replace")


def _rewrite_location(loc, port, prefix):
    """Keep redirects inside the prefix (the app thinks it lives at /)."""
    for origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        if loc.startswith(origin):
            loc = loc[len(origin):] or "/"
    if loc.startswith("/") and not loc.startswith("//") and not loc.startswith(prefix + "/") \
            and loc != prefix:
        return prefix + loc
    return loc


def _proxy_to(port, rest, label, prefix, rewrite=False):
    """Forward the current request to ``127.0.0.1:<port>/<rest>`` and relay the reply."""
    url = f"http://127.0.0.1:{port}/{rest}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    # Tell the app where it lives so prefix-aware frameworks can build correct links.
    headers["X-Forwarded-Prefix"] = prefix
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = request.scheme
    headers["X-Forwarded-For"] = request.remote_addr or ""
    if rewrite:
        headers["Accept-Encoding"] = "identity"        # we must read the body as text
    body = request.get_data()
    # Connection-refused means nothing was processed, so retrying is always
    # safe (a freshly launched app needs a moment to bind its port). Timeouts
    # and other errors are not retried: the request may have run.
    resp = None
    last_err = None
    for _ in range(8):
        try:
            resp = _upstream().request(
                request.method, url, params=list(request.args.items(multi=True)), data=body,
                headers=headers, cookies=_forward_cookies(), timeout=30,
                allow_redirects=False,
            )
            break
        except requests.ConnectionError as e:
            if isinstance(e, requests.ReadTimeout):
                last_err = e
                break
            last_err = e
            time.sleep(0.25)
        except requests.RequestException as e:
            last_err = e
            break
    if resp is None:
        app.logger.warning("proxy %s failed: %s", label, last_err)
        abort(502)
    excluded = {"content-length", "content-encoding", "transfer-encoding",
                "connection", "server", "keep-alive"}
    out_headers = []
    for k, v in resp.raw.headers.items():
        if k.lower() in excluded:
            continue
        if k.lower() == "location":
            v = _rewrite_location(v, port, prefix)
        out_headers.append((k, v))
    content = resp.content
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if rewrite and any(t in ctype for t in _REWRITE_TYPES):
        content = _rewrite_body(content, resp.encoding, prefix)
    return Response(content, status=resp.status_code, headers=out_headers)


def _do_proxy(folder, port, rest=""):
    """Hosted apps started with `sudo serve` / `sudo gunicorn`: /<folder>/<port>/…"""
    key = (folder, port)
    if key not in HOSTS:
        # An exposure whose path happens to start with digits (/myapp/8000/…).
        if exposure.get(folder):
            return _named_proxy(folder, request.path.split("/", 2)[2])
        abort(404)
    return _proxy_to(port, rest, f"{folder}:{port}", f"/{folder}/{port}")


def _named_proxy(name, rest=""):
    """`expose <port> <name>`: /<name>/… -> 127.0.0.1:<port>/…"""
    e = exposure.get(name)
    if not e:
        abort(404)
    return _proxy_to(e["port"], rest, name, f"/{name}", rewrite=e["rewrite"])


def _named_bare(name):
    """`/name` -> `/name/` so relative links in the app resolve inside the prefix."""
    if not exposure.get(name):
        abort(404)
    qs = request.query_string.decode("latin-1")
    return redirect(f"/{name}/" + (f"?{qs}" if qs else ""), code=308)


_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]
app.add_url_rule("/<folder>/<int:port>", "proxy_noslash",
                 lambda folder, port: _do_proxy(folder, port, ""), methods=_PROXY_METHODS)
app.add_url_rule("/<folder>/<int:port>/", "proxy_slash", _do_proxy, methods=_PROXY_METHODS)
app.add_url_rule("/<folder>/<int:port>/<path:rest>", "proxy_rest", _do_proxy,
                 methods=_PROXY_METHODS)
app.add_url_rule("/<name>", "proxy_named_bare", _named_bare, methods=["GET", "HEAD"])
app.add_url_rule("/<name>/", "proxy_named", lambda name: _named_proxy(name, ""),
                 methods=_PROXY_METHODS)
app.add_url_rule("/<name>/<path:rest>", "proxy_named_rest", _named_proxy,
                 methods=_PROXY_METHODS)


# ---------------------------------------------------------------------------
# Live job tail: incremental output feed for `jobtail` / `fg`.
# ---------------------------------------------------------------------------
@app.route("/api/jobtail")
def api_jobtail():
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    sid = session["username"]
    try:
        jid = int(request.args.get("job"))
    except (TypeError, ValueError):
        return {"error": "bad job id"}, 400
    try:
        off = int(request.args.get("off", 0) or 0)
    except (TypeError, ValueError):
        off = 0
    job = jobs.get_job(sid, jid)
    if not job:
        return {"error": "no such job"}, 404
    text, new_off = job.read_from(off)
    return {"text": text, "off": new_off, "alive": job.alive}


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
@app.route("/api/tabs")
def api_tabs():
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    username = session["username"]
    sess = state.get_session(username)
    tabs = []
    if sess:
        for tid, t in sess["tabs"].items():
            s = termsess.get(username, tid)
            tabs.append({
                "id": tid,
                "name": t.get("name", tid),
                "cwd": t.get("cwd"),
                "is_terminal": s is not None,
                "alive": bool(s and s.alive and not s.exited),
                "cmd": t.get("cmd", ""),
            })
    return {"tabs": tabs, "target": sandbox.target_label(username)}


@app.route("/api/targets")
def api_targets():
    """What the browser's target dropdown shows (host/VMs/installable systems)."""
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    return sandbox.targets(session["username"])


@app.route("/api/target", methods=["POST"])
def api_set_target():
    """Switch where plain commands run: ``host`` or ``vm:<name>``."""
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    username = session["username"]
    data = request.get_json(silent=True) or {}
    ok, msg = sandbox.set_target(username, str(data.get("target", "")))
    if not ok:
        return {"ok": False, "error": msg}, 400
    sandbox.reset_cwds(username)
    sess = state.get_session(username)
    cwds = {tid: t["cwd"] for tid, t in (sess or {}).get("tabs", {}).items()}
    return {"ok": True, "cwds": cwds, **sandbox.targets(username)}


@app.route("/api/close_tab", methods=["POST"])
def api_close_tab():
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    username = session["username"]
    data = request.get_json(silent=True) or {}
    tab_id = data.get("tab")
    if not tab_id:
        return {"ok": False}, 400
    state.remove_tab(username, tab_id)
    with termsess.SESSIONS_LOCK:
        s = termsess.SESSIONS.pop((username, tab_id), None)
    if s:
        s.kill()
    return {"ok": True}


# ---------------------------------------------------------------------------
# File APIs (editor + visual file navigator).
#
# Every path is resolved through ``sandbox.fs``: admins in host mode see the
# whole machine, everyone else is confined to their VM root (symlinks and ``..``
# cannot escape it). Paths returned to the client are the user's own view.
# ---------------------------------------------------------------------------
def _fs_view():
    """Return ``(view, None)`` or ``(None, error_response)``."""
    try:
        return sandbox.fs(session["username"]), None
    except sandbox.NeedVmChoice as need:
        msg = ("no system installed yet — install one from the target menu" if need.empty
               else "several systems installed — choose one from the target menu")
        return None, ({"ok": False, "error": msg, "choose": True}, 409)
    except sandbox.SandboxError as e:
        return None, ({"ok": False, "error": str(e)}, 409)


def _map(view, path, follow_last=True):
    """Map a client path to a host path (relative paths use the view's home)."""
    return view.host(path or view.default_cwd(), view.default_cwd(), follow_last)


@app.route("/api/file")
def api_file():
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    path = request.args.get("path", "")
    if not path:
        return {"error": "missing path"}, 400
    view, err = _fs_view()
    if err:
        return err
    try:
        p = _map(view, path)
        if p.is_dir():
            return {"error": "is a directory"}, 400
        exists = p.exists()
        content = ""
        if exists:
            if p.stat().st_size > MAX_EDIT_BYTES:
                return {"error": "file too large for the editor "
                                 f"(limit {MAX_EDIT_BYTES // (1024 * 1024)} MB)"}, 413
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        return {"path": view.virt(p), "content": content, "exists": exists}
    except (OSError, sandbox.SandboxError) as e:
        return {"error": str(e)}, 500


@app.route("/api/file", methods=["POST"])
def api_file_save():
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    content = data.get("content", "")
    if not path:
        return {"ok": False, "error": "missing path"}, 400
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be text"}, 400
    view, err = _fs_view()
    if err:
        return err
    try:
        p = _map(view, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": view.virt(p)}
    except (OSError, sandbox.SandboxError) as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/api/dir")
def api_dir():
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    view, err = _fs_view()
    if err:
        return err
    try:
        p = _map(view, request.args.get("path", ""))
        vpath = view.virt(p)
    except (OSError, sandbox.SandboxError) as e:
        return {"error": str(e)}, 400
    if not p.is_dir():
        return {"error": f"not a directory: {vpath}"}, 400
    entries = []
    truncated = False
    try:
        # scandir hands back cached type/stat info: one syscall per entry
        # instead of listdir + isdir + stat (3x), noticeable on big folders.
        with os.scandir(p) as it:
            for e in it:
                if len(entries) >= MAX_DIR_ENTRIES:
                    truncated = True
                    break
                try:
                    is_dir = e.is_dir()
                    st = e.stat()
                except OSError:
                    continue
                entries.append({
                    "name": e.name,
                    "path": view.child_virt(vpath, e.name),
                    "is_dir": is_dir,
                    "size": 0 if is_dir else st.st_size,
                    "mtime": int(st.st_mtime),
                })
    except OSError as e:
        return {"error": str(e)}, 500
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    parent = posixpath.dirname(vpath) if not view.unrestricted else os.path.dirname(vpath)
    if not parent or parent == vpath:
        parent = vpath
    return {"path": vpath, "parent": parent, "entries": entries, "truncated": truncated,
            "max_upload_mb": config.MAX_UPLOAD_MB}


@app.route("/api/fs", methods=["POST"])
def api_fs():
    if "username" not in session:
        return {"ok": False, "error": "unauthorized"}, 401
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip().lower()
    view, err = _fs_view()
    if err:
        return err

    def need(field):
        val = data.get(field)
        if not val:
            raise ValueError(f"missing {field}")
        return val

    try:
        if action == "mkdir":
            p = _map(view, need("path"))
            os.makedirs(p, exist_ok=True)
            return {"ok": True, "path": view.virt(p)}
        if action == "touch":
            p = _map(view, need("path"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
            return {"ok": True, "path": view.virt(p)}
        if action == "rm":
            # Don't follow a final symlink: remove the link, never its target.
            p = _map(view, need("path"), follow_last=False)
            if p.is_dir() and not p.is_symlink():
                os.rmdir(p)  # only empty dirs; use rmtree for trees
            else:
                os.remove(p)
            return {"ok": True, "path": view.virt(p)}
        if action == "rmtree":
            p = _map(view, need("path"), follow_last=False)
            if not p.is_dir() or p.is_symlink():
                return {"ok": False, "error": "not a directory"}, 400
            if p.parent == p or (view.root is not None and p == view.host("/")):
                return {"ok": False, "error": "refusing to delete a filesystem root"}, 400
            shutil.rmtree(p)
            return {"ok": True, "path": view.virt(p)}
        if action in ("rename", "mv", "move"):
            src_in = data.get("src") or data.get("path")
            dst_in = data.get("dst") or data.get("name")
            if not src_in or not dst_in:
                return {"ok": False, "error": "missing src/dst"}, 400
            src = _map(view, src_in, follow_last=False)
            dst = _map(view, dst_in, follow_last=False)
            if not os.path.lexists(src):
                return {"ok": False, "error": f"no such file: {view.virt(src)}"}, 404
            os.rename(src, dst)
            return {"ok": True, "path": view.virt(dst)}
        return {"ok": False, "error": f"unknown action: {action}"}, 400
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}, 404
    except (OSError, sandbox.SandboxError) as e:
        return {"ok": False, "error": str(e)}, 500


# ---------------------------------------------------------------------------
# File browser: download (a file, or a folder as a streamed .zip) and upload
# (files or whole folders). Both go through the same confinement as the rest of
# the file APIs: ``_map`` for the path, and symlinks are never followed out of it.
# ---------------------------------------------------------------------------
def _content_disposition(filename):
    """``attachment`` header that survives non-ASCII and quote characters."""
    from urllib.parse import quote
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", filename) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


class _ZipSink:
    """Write-only, unseekable buffer: ``zipfile`` fills it, the response drains it."""

    def __init__(self):
        self._chunks = []
        self._pos = 0

    def write(self, data):
        self._chunks.append(bytes(data))
        self._pos += len(data)
        return len(data)

    def tell(self):
        return self._pos

    def flush(self):
        pass

    def pending(self):
        return sum(len(c) for c in self._chunks)

    def drain(self):
        out = b"".join(self._chunks)
        self._chunks = []
        return out


def _zip_stream(root, top_name):
    """Yield a .zip of the directory ``root`` as it is built (constant memory).

    Walks with directory file descriptors and ``O_NOFOLLOW`` so a symlink inside
    the folder can never pull in a file from outside it (not even by swapping a
    link in mid-walk). Symlinks, sockets, FIFOs and devices are skipped.
    """
    sink = _ZipSink()
    zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, compresslevel=3)
    try:
        for dirpath, dirnames, filenames, dirfd in os.fwalk(root, follow_symlinks=False):
            rel_dir = os.path.relpath(dirpath, root)
            prefix = top_name if rel_dir == "." else top_name + "/" + rel_dir.replace(os.sep, "/")
            if not dirnames and not filenames:
                zi = zipfile.ZipInfo(prefix + "/", time.localtime(time.time())[:6])
                zi.external_attr = (0o40755 << 16) | 0x10
                zf.writestr(zi, b"")
                if sink.pending():
                    yield sink.drain()
            for name in filenames:
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
                except OSError:
                    continue
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    zi = zipfile.ZipInfo(prefix + "/" + name, time.localtime(st.st_mtime)[:6])
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zi.external_attr = (stat.S_IMODE(st.st_mode) | 0o100000) << 16
                    zi.file_size = st.st_size
                    with zf.open(zi, "w", force_zip64=st.st_size > 0x7FFFFFFF) as dest:
                        while True:
                            chunk = os.read(fd, 256 * 1024)
                            if not chunk:
                                break
                            dest.write(chunk)
                            if sink.pending() >= 64 * 1024:
                                yield sink.drain()
                finally:
                    os.close(fd)
                if sink.pending():
                    yield sink.drain()
        zf.close()
        yield sink.drain()
    finally:
        try:
            zf.close()
        except Exception:
            pass


@app.route("/api/download")
def api_download():
    """Download a file, or a folder as ``<name>.zip``."""
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    path = request.args.get("path", "")
    if not path:
        return {"error": "missing path"}, 400
    view, err = _fs_view()
    if err:
        return err
    try:
        p = _map(view, path)
        if p.is_dir():
            if p.parent == p or (view.root is not None and p == view.host("/")):
                return {"error": "download a folder inside the root, not the root itself"}, 400
            name = (p.name or "folder") + ".zip"
            resp = Response(_zip_stream(str(p), p.name or "folder"), mimetype="application/zip")
            resp.headers["Content-Disposition"] = _content_disposition(name)
            resp.headers["Cache-Control"] = "no-store"
            return resp
        if not p.is_file():
            return {"error": "not a regular file" if p.exists() else f"no such file: {view.virt(p)}"}, 404
        # octet-stream on purpose: the response compressor must never buffer a big download.
        resp = send_file(p, mimetype="application/octet-stream", conditional=True, max_age=0)
        resp.headers["Content-Disposition"] = _content_disposition(p.name)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except (OSError, sandbox.SandboxError) as e:
        return {"error": str(e)}, 500


def _upload_target(view, folder, rel):
    """Virtual path for ``rel`` (``a/b/c.txt``) under ``folder``; ``None`` if unsafe."""
    rel = rel or ""
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    target = folder
    for part in parts:
        target = view.child_virt(target, part)
    return target


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Receive one file (``file``) into ``dir``; ``rel`` keeps a folder upload's layout."""
    if "username" not in session:
        return {"ok": False, "error": "unauthorized"}, 401
    view, err = _fs_view()
    if err:
        return err
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    if (request.content_length or 0) > limit + 1024 * 1024:
        return {"ok": False, "error": f"file too large (limit {config.MAX_UPLOAD_MB} MB per file)"}, 413
    upload = request.files.get("file")
    if upload is None:
        return {"ok": False, "error": "missing file"}, 400
    folder = request.form.get("dir", "")
    rel = request.form.get("rel") or upload.filename or ""
    overwrite = request.form.get("overwrite") in ("1", "true", "yes")
    try:
        base = view.virt(_map(view, folder))
        if not _map(view, folder).is_dir():
            return {"ok": False, "error": f"not a directory: {base}"}, 400
        vpath = _upload_target(view, base, rel)
        if vpath is None:
            return {"ok": False, "error": f"unsafe file name: {rel!r}"}, 400
        dest = _map(view, vpath, follow_last=False)          # replace a symlink, never write through it
        if os.path.lexists(dest):
            if dest.is_dir() and not dest.is_symlink():
                return {"ok": False, "error": f"{view.virt(dest)} is a folder"}, 409
            if not overwrite:
                return {"ok": False, "exists": True, "path": view.virt(dest),
                        "error": f"{view.virt(dest)} already exists"}, 409
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.upload-{uuid.uuid4().hex[:8]}")
        try:
            upload.save(tmp)
            if tmp.stat().st_size > limit:
                return {"ok": False, "error": f"file too large (limit {config.MAX_UPLOAD_MB} MB per file)"}, 413
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"ok": True, "path": view.virt(dest), "size": dest.stat().st_size}
    except (OSError, sandbox.SandboxError) as e:
        return {"ok": False, "error": str(e)}, 500


# ---------------------------------------------------------------------------
# Interactive terminal: stream a real PTY over a websocket.
# ---------------------------------------------------------------------------
def _ws_notice(ws, text):
    try:
        ws.send(f"\r\n\x1b[31m[rconsole] {text}\x1b[0m\r\n")
    except Exception:
        pass


@sock.route("/ws")
def ws_terminal(ws):
    if "username" not in session:
        return
    username = session["username"]
    if session.get("must_change"):
        return _ws_notice(ws, "change the default password first: sudo cpass")
    sess = state.get_session(username)
    if not sess:
        return
    tab_id = request.args.get("tab", "main")
    tab = state.ensure_tab(sess, username, tab_id)
    cmd = request.args.get("cmd") or ""
    if len(cmd) > 4096 or len(tab_id) > 64:
        return
    if cmd:
        # The command comes straight from the query string, so it is untrusted:
        # authorize it BEFORE persisting it or spawning anything.
        forced, _ = sandbox.parse_target(cmd)
        try:
            sandbox.resolve_target(username, forced)
        except sandbox.NeedVmChoice as need:
            return _ws_notice(ws, "no system installed yet: run `vm install`" if need.empty
                              else "choose a system first (target menu, or `vm use <name>`)")
        except sandbox.SandboxError as e:
            return _ws_notice(ws, str(e))
        state.set_terminal(sess, username, tab_id, cmd, tab["cwd"])
    try:
        cols = int(request.args.get("cols", 80))
    except (TypeError, ValueError):
        cols = 80
    try:
        rows = int(request.args.get("rows", 24))
    except (TypeError, ValueError):
        rows = 24
    try:
        ptyhost.attach(ws, username, tab_id, cmd, tab["cwd"], cols, rows)
    except sandbox.NeedVmChoice:
        _ws_notice(ws, "choose a system first (target menu, or `vm use <name>`)")
    except sandbox.SandboxError as e:
        _ws_notice(ws, str(e))
    except OSError as e:
        _ws_notice(ws, f"could not start: {e}")


# ---------------------------------------------------------------------------
# Custom error pages. API routes (/api/*, JSON Accept) get a JSON error so the
# client keeps working; everything else gets a themed HTML page.
# ---------------------------------------------------------------------------
ERROR_INFO = {
    400: ("Bad Request", "The server couldn't understand that request."),
    401: ("Unauthorized", "You need to log in to access this."),
    403: ("Forbidden", "You don't have permission to do that."),
    404: ("Not Found", "This page wandered off into the void."),
    405: ("Method Not Allowed", "That request method isn't supported here."),
    408: ("Request Timeout", "The server timed out waiting for you."),
    413: ("Payload Too Large", "That upload is too big."),
    429: ("Too Many Requests", "Slow down — you're sending requests too fast."),
    500: ("Internal Server Error", "Something broke on our end."),
    502: ("Bad Gateway", "An upstream service misbehaved."),
    503: ("Service Unavailable", "The server is temporarily overloaded or down."),
    504: ("Gateway Timeout", "An upstream service timed out."),
}


def _is_api_request():
    if request.path.startswith("/api/"):
        return True
    return request.accept_mimetypes.best_match(
        ["application/json", "text/html"]
    ) == "application/json"


def _static_version():
    p = os.path.join(os.path.dirname(__file__), "static", "style.css")
    try:
        return int(os.path.getmtime(p))
    except OSError:
        return 1


@app.errorhandler(HTTPException)
def handle_http_error(e):
    code = e.code or 500
    title, default_msg = ERROR_INFO.get(code, ("Error", str(e)))
    message = e.description or default_msg
    if _is_api_request():
        return jsonify(error=message, code=code), code
    return render_template(
        "error.html", code=code, title=title, message=message,
        cache_v=_static_version(),
    ), code


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return handle_http_error(e)
    app.logger.exception("Unhandled exception: %s", e)
    if _is_api_request():
        return jsonify(error="internal server error", code=500), 500
    return render_template(
        "error.html",
        code=500,
        title="Internal Server Error",
        message="Something broke on our end. Check the server logs.",
        cache_v=_static_version(),
    ), 500


# Seed the default user at import time so login works regardless of how the app
# is launched (gunicorn/WSGI on Render never triggers the __main__ block).
try:
    auth.seed_defaults()
except Exception as e:  # e.g. a read-only data dir shouldn't crash the whole app
    app.logger.warning("Could not seed default user: %s", e)


def start_background_tasks():
    """Start the things that keep a free instance useful (all no-ops unless enabled):

    * keep-alive — pings our own public URL so the host never puts us to sleep;
    * backups    — periodic encrypted snapshots to a git repo / folder;
    * VM rebuild — after a restore, reinstall the VMs that were backed up;
    * supervisor — start and restart the user's services (bots, workers).
    """
    started = []
    try:
        if keepalive.start_background():
            started.append("keepalive")
        if backup.start_background():
            started.append("backup")
        if sandbox.restore_vms_background():
            started.append("vm-rebuild")
        if services.start_background():
            started.append("supervisor")
    except Exception as e:                       # never stop the site from starting
        app.logger.warning("background tasks: %s: %s", type(e).__name__, e)
    if started:
        app.logger.info("background tasks: %s", ", ".join(started))


start_background_tasks()
updater.schedule_confirm()          # a freshly updated process confirms itself once it has stayed up


def main():
    port = int(os.environ.get("PORT", 5000))
    # Threaded so a long-lived terminal websocket doesn't block other requests
    # (other tabs' commands, API calls) on the dev server.
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
