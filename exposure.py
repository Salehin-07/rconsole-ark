"""Registry of *exposures*: a name -> local port mapping served at ``/<name>/``.

``expose 8000 myapp`` makes whatever listens on ``127.0.0.1:8000`` reachable at
``https://<your-domain>/myapp/`` through rconsole's reverse proxy. The program
can run anywhere on the server — on the host or inside any of your VMs (they
share the server's network), started with ``python main.py &``, ``npm run dev``,
``flask run -p 8000`` and so on.

Rules
-----
* names are lowercase ``a-z 0-9 -`` (1-32 chars), unique across all users, never
  all digits (that would clash with ``/<folder>/<port>/`` hosted apps) and never
  one of the console's own paths (``login``, ``api``, ...);
* ports 1024-65535, and never the console's own port;
* a name belongs to the user who created it; the owner or any admin can remove it;
* private by default (the visitor must be logged in); ``--public`` (admins only)
  serves it to anyone — useful for webhooks and demos.

The registry is a small JSON file in the data dir (atomic ``0600`` writes), read
through an mtime cache so the per-request lookup does not touch the disk.
"""
import copy
import json
import os
import re
import secrets
import threading
import time

import config

FILE = config.DATA_DIR / "exposed.json"
NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
RESERVED = frozenset({
    "login", "logout", "api", "static", "ws", "healthz", "console", "admin",
    "favicon", "robots", "sitemap", "assets", "index", "expose", "serve",
})
PORT_MIN, PORT_MAX = 1024, 65535
MAX_PER_USER = 20

_lock = threading.RLock()
_cache = {"sig": None, "data": {}}


def server_port():
    """The port the console itself listens on (never exposable: it would loop)."""
    try:
        return int(os.environ.get("PORT") or 5000)
    except ValueError:
        return 5000


def _sig():
    try:
        st = os.stat(FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _read():
    sig = _sig()
    if sig is None:
        return {}
    if sig == _cache["sig"]:
        return _cache["data"]
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return {}                       # a corrupt registry just means "nothing exposed"
    _cache["sig"], _cache["data"] = sig, data
    return data


def _save(data):
    config.ensure_private_dir(config.DATA_DIR)
    tmp = FILE.with_name(f".exposed.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _cache["sig"], _cache["data"] = _sig(), data


# ---------------------------------------------------------------- queries
def get(name):
    """The exposure called ``name`` (a copy), or ``None``."""
    with _lock:
        e = _read().get((name or "").lower())
        return dict(e, name=name.lower()) if e else None


def list_all():
    """Every exposure, sorted by name."""
    with _lock:
        return [dict(v, name=k) for k, v in sorted(_read().items())]


def list_for(owner):
    return [e for e in list_all() if e["owner"] == owner]


# -------------------------------------------------------------- validation
def validate_name(name):
    """Return an error message for a bad name, else ``None``."""
    name = (name or "").lower()
    if not NAME_RE.match(name):
        return "name must be 1-32 characters: letters, digits and '-' (not at the ends)"
    if name.isdigit():
        return "name can't be only digits (that is how hosted apps like /site/8000/ look)"
    if name in RESERVED:
        return f"'{name}' is reserved for the console itself"
    return None


def validate_port(port):
    if not isinstance(port, int) or isinstance(port, bool):
        return "port must be a number"
    if not (PORT_MIN <= port <= PORT_MAX):
        return f"port must be between {PORT_MIN} and {PORT_MAX}"
    if port == server_port():
        return f"port {port} is the console itself — pick the port your app uses"
    return None


# --------------------------------------------------------------- mutations
def add(name, port, owner, public=False, rewrite=False):
    """Create or update an exposure. Returns ``(ok, message)``."""
    name = (name or "").lower()
    err = validate_name(name) or validate_port(port)
    if err:
        return False, err
    with _lock:
        data = copy.deepcopy(_read())
        existing = data.get(name)
        if existing and existing["owner"] != owner:
            return False, f"'{name}' is already used by someone else"
        if not existing and sum(1 for v in data.values() if v["owner"] == owner) >= MAX_PER_USER:
            return False, f"you already have {MAX_PER_USER} exposures; remove one first"
        data[name] = {"port": port, "owner": owner, "public": bool(public),
                      "rewrite": bool(rewrite),
                      "created": existing["created"] if existing else int(time.time())}
        _save(data)
        return True, ("updated" if existing else "created")


def remove(name, requester, is_admin=False):
    """Remove an exposure. Returns ``(ok, message)``."""
    name = (name or "").lower()
    with _lock:
        data = copy.deepcopy(_read())
        e = data.get(name)
        if not e:
            return False, f"no exposure called '{name}'"
        if e["owner"] != requester and not is_admin:
            return False, f"'{name}' belongs to {e['owner']}"
        del data[name]
        _save(data)
        return True, "removed"


def remove_owner(owner):
    """Drop every exposure of a user (account deleted)."""
    with _lock:
        data = copy.deepcopy(_read())
        keep = {k: v for k, v in data.items() if v["owner"] != owner}
        if len(keep) != len(data):
            _save(keep)
        return len(data) - len(keep)


def rename_owner(old, new):
    """Move a user's exposures to their new username."""
    with _lock:
        data = copy.deepcopy(_read())
        changed = False
        for v in data.values():
            if v["owner"] == old:
                v["owner"] = new
                changed = True
        if changed:
            _save(data)


def clear():
    """Remove all exposures (factory reset)."""
    with _lock:
        _save({})
