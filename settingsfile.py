"""The settings file: dashboard changes to environment options, saved in ``<data dir>/settings.json``.

This module is stdlib-only and imports nothing from Rconsole, so :mod:`config` (and ``gunicorn.conf.py``)
can load it before anything else exists. A value saved here **wins over the real environment variable** of the
same name; removing it (``Reset`` in the dashboard) falls back to the environment variable again.

Only variables listed in :mod:`settings` are ever written. The file is private (mode 600) because it can hold
the backup token and passphrase when you type them into the dashboard.
"""
import json
import os
import secrets
from pathlib import Path

FILENAME = "settings.json"
# Never taken from the file, whatever it says: the data dir is where the file lives, and the other three
# would lock everybody out (or invalidate every password) if changed behind your back.
LOCKED = frozenset({"RCONSOLE_DATA_DIR", "RCONSOLE_SECRET", "RCONSOLE_PEPPER", "RCONSOLE_ADMIN_PASSWORD"})
# What the environment said before any override was injected (so the dashboard can show it and `Reset` can
# restore it). Filled by :func:`inject`.
ORIGINAL = {}


def data_dir():
    """The data directory, decided exactly like :mod:`config` does (the environment only)."""
    explicit = os.environ.get("RCONSOLE_DATA_DIR")
    if explicit:
        return Path(explicit).resolve()
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") if (
        os.environ.get("RAILWAY_PROJECT_ID") or os.environ.get("RAILWAY_ENVIRONMENT")) else None
    return Path(vol or (Path(__file__).resolve().parent / "data")).resolve()


def path(base=None):
    return Path(base or data_dir()) / FILENAME


def load(base=None):
    """The saved overrides as ``{ENV_NAME: "value"}`` (``{}`` when there is no usable file)."""
    try:
        raw = json.loads(path(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    vals = raw.get("values") if isinstance(raw, dict) else None
    if not isinstance(vals, dict):
        return {}
    return {str(k): str(v) for k, v in vals.items() if isinstance(k, str) and isinstance(v, (str, int, float))
            and k.startswith(("RCONSOLE_", "PORT")) and k not in LOCKED}


def save(values, base=None):
    """Write ``values`` atomically with owner-only permissions. Raises ``OSError`` when the disk is unusable."""
    p = path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "values": dict(sorted(values.items()))}, f, indent=1)
    os.replace(tmp, p)


def inject(base=None, environ=None):
    """Put the saved overrides into ``os.environ`` (remembering what was there). Returns the overrides."""
    env = os.environ if environ is None else environ
    vals = load(base)
    for k, v in vals.items():
        ORIGINAL.setdefault(k, env.get(k))
        env[k] = v
    return vals
