"""User authentication and persistence (default user: ark / ark).

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user salt plus the server
pepper. Legacy unsalted ``sha256(password + PEPPER)`` hashes are still accepted
and are transparently upgraded to the new format on the next successful login,
so existing ``data/users.json`` files keep working.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
USERS_FILE = DATA_DIR / "users.json"

# A static pepper so hashes are not plain SHA256 of the password.
PEPPER = os.environ.get("RCONSOLE_PEPPER", "rconsole-static-pepper-change-me")

# Usernames may contain letters, digits, and ._- (aligned with the state-file
# sanitizer in state.py); capped so they stay usable as filenames and labels.
USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{1,32}\Z")

_PBKDF2_ROUNDS = 210_000

# Serializes user-file read-modify-write cycles so concurrent requests (e.g.
# two `sudo muser` sessions) can't clobber each other's changes.
_USERS_LOCK = threading.Lock()


def _legacy_hash(password):
    return hashlib.sha256((password + PEPPER).encode("utf-8")).hexdigest()


def _hash(password):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", (password + PEPPER).encode("utf-8"),
        salt.encode("utf-8"), _PBKDF2_ROUNDS,
    )
    return f"pbkdf2${salt}${dk.hex()}"


def _check(stored, password):
    if stored.startswith("pbkdf2$"):
        try:
            _, salt, hexd = stored.split("$")
            dk = hashlib.pbkdf2_hmac(
                "sha256", (password + PEPPER).encode("utf-8"),
                salt.encode("utf-8"), _PBKDF2_ROUNDS,
            )
        except Exception:
            return False
        return hmac.compare_digest(dk.hex(), hexd)
    return hmac.compare_digest(stored, _legacy_hash(password))


def _valid_username(username):
    return bool(username and USERNAME_RE.fullmatch(username))


def _load():
    if not USERS_FILE.exists():
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(users):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)


def seed_defaults():
    with _USERS_LOCK:
        users = _load()
        if "ark" not in users:
            users["ark"] = {"password": _hash("ark"), "is_admin": True}
            _save(users)


def reset_to_defaults():
    """Factory reset: wipe all users and recreate the default ark account."""
    with _USERS_LOCK:
        users = {"ark": {"password": _hash("ark"), "is_admin": True}}
        _save(users)


def verify(username, password):
    with _USERS_LOCK:
        users = _load()
        u = users.get(username)
        if not u:
            return False
        if not _check(u["password"], password):
            return False
        # Transparently upgrade legacy hashes on successful login.
        if not u["password"].startswith("pbkdf2$"):
            u["password"] = _hash(password)
            _save(users)
        return True


def get_user(username):
    with _USERS_LOCK:
        return _load().get(username)


def list_users():
    with _USERS_LOCK:
        return list(_load().keys())


def create_user(username, password, is_admin=False):
    with _USERS_LOCK:
        users = _load()
        if username in users:
            return False, "User already exists"
        if not _valid_username(username):
            return False, "Username must be 1-32 chars: letters, digits, . _ -"
        if len(password) < 4:
            return False, "Password must be at least 4 characters"
        users[username] = {"password": _hash(password), "is_admin": is_admin}
        _save(users)
        return True, None


def set_password(username, password):
    with _USERS_LOCK:
        users = _load()
        if username not in users:
            return False, "No such user"
        if len(password) < 4:
            return False, "Password must be at least 4 characters"
        users[username]["password"] = _hash(password)
        _save(users)
        return True, None


def set_username(old, new):
    with _USERS_LOCK:
        users = _load()
        if old not in users:
            return False, "No such user"
        if new in users:
            return False, "Username already taken"
        if not _valid_username(new):
            return False, "Username must be 1-32 chars: letters, digits, . _ -"
        users[new] = users.pop(old)
        _save(users)
        return True, None


def set_admin(username, is_admin):
    with _USERS_LOCK:
        users = _load()
        if username not in users:
            return False, "No such user"
        users[username]["is_admin"] = bool(is_admin)
        _save(users)
        return True, None


def delete_user(username):
    with _USERS_LOCK:
        users = _load()
        if username not in users:
            return False, "No such user"
        users.pop(username)
        _save(users)
        return True, None
