"""User authentication and persistence.

Security properties
-------------------
* Passwords are PBKDF2-HMAC-SHA256 with a per-user salt plus a server pepper.
  The pepper comes from ``RCONSOLE_PEPPER`` or a random value persisted in the
  data dir (never a hard-coded constant).
* Legacy hashes (unsalted SHA-256, or PBKDF2 with the old public pepper) are
  still accepted and transparently upgraded on the next successful login, so
  existing ``users.json`` files keep working.
* The user file is written atomically with ``0600`` permissions, and reads are
  cached by mtime so the per-request auth check does not hit the disk.
* A corrupt user file is quarantined instead of silently treated as empty (which
  would have re-created the default admin account).
* Failed logins are throttled per (ip, username) — see :class:`LoginThrottle`.

The default admin is ``ark``. Set ``RCONSOLE_ADMIN_PASSWORD`` to choose its
initial password; otherwise it is ``ark`` and the console forces a change.
"""
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

import config

DATA_DIR = config.DATA_DIR
USERS_FILE = DATA_DIR / "users.json"

DEFAULT_ADMIN = "ark"
DEFAULT_PASSWORD = "ark"

# The pepper that shipped in earlier versions. It is public (it was in the
# source), so it is only ever used to *verify and upgrade* old hashes.
_LEGACY_PEPPER = "rconsole-static-pepper-change-me"
PEPPER = config.persistent_secret("RCONSOLE_PEPPER", "pepper.key")

# Names start with an alphanumeric (so '.' and '..' are impossible) and may
# contain ._- afterwards; capped so they stay usable as filenames and labels.
USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")

_PBKDF2_ROUNDS = 210_000

# Serializes user-file read-modify-write cycles so concurrent requests (e.g.
# two `sudo muser` sessions) can't clobber each other's changes.
_USERS_LOCK = threading.RLock()
_cache = {"sig": None, "users": {}}


class UserStoreError(RuntimeError):
    """The user file exists but could not be read."""


# --------------------------------------------------------------------- hashing
def _derive(password, salt, pepper):
    return hashlib.pbkdf2_hmac(
        "sha256", (password + pepper).encode("utf-8"),
        salt.encode("utf-8"), _PBKDF2_ROUNDS,
    ).hex()


def _legacy_hash(password, pepper):
    return hashlib.sha256((password + pepper).encode("utf-8")).hexdigest()


def _hash(password):
    salt = secrets.token_hex(16)
    return f"pbkdf2v2${salt}${_derive(password, salt, PEPPER)}"


def _check(stored, password):
    """Return ``(ok, needs_upgrade)`` for a stored hash."""
    try:
        if stored.startswith("pbkdf2v2$"):
            _, salt, hexd = stored.split("$")
            return hmac.compare_digest(_derive(password, salt, PEPPER), hexd), False
        if stored.startswith("pbkdf2$"):
            _, salt, hexd = stored.split("$")
            for pepper in (PEPPER, _LEGACY_PEPPER):
                if hmac.compare_digest(_derive(password, salt, pepper), hexd):
                    return True, True
            return False, False
        for pepper in (PEPPER, _LEGACY_PEPPER):
            if hmac.compare_digest(stored, _legacy_hash(password, pepper)):
                return True, True
    except (ValueError, TypeError):
        pass
    return False, False


# ------------------------------------------------------------------- password
def validate_password(password):
    """Return an error string, or ``None`` if the password is acceptable."""
    if len(password or "") < config.MIN_PASSWORD_LEN:
        return f"Password must be at least {config.MIN_PASSWORD_LEN} characters"
    return None


def is_default_credential(username, password):
    """True if these are the well-known factory credentials."""
    return username == DEFAULT_ADMIN and password == DEFAULT_PASSWORD


def _valid_username(username):
    return bool(username and USERNAME_RE.fullmatch(username))


_USERNAME_ERR = ("Username must be 1-32 chars: letters, digits, . _ - "
                 "(starting with a letter or digit)")


# ------------------------------------------------------------------ persistence
def _sig():
    try:
        st = os.stat(USERS_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _read_cached():
    """Return the cached users dict (do NOT mutate). Raises UserStoreError."""
    sig = _sig()
    if sig is None:
        return {}
    if sig == _cache["sig"]:
        return _cache["users"]
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            users = json.load(f)
        if not isinstance(users, dict):
            raise ValueError("users.json must contain an object")
    except (OSError, ValueError) as e:
        # Quarantine instead of treating as empty: an empty store would make
        # seed_defaults() recreate the default admin with a known password.
        try:
            os.replace(USERS_FILE, USERS_FILE.with_suffix(f".corrupt.{int(time.time())}"))
        except OSError:
            pass
        raise UserStoreError(f"user store unreadable and was quarantined: {e}") from e
    _cache["sig"], _cache["users"] = sig, users
    return users


def _load():
    """Return a private, mutable copy of the users dict."""
    return copy.deepcopy(_read_cached())


def _save(users):
    config.ensure_private_dir(DATA_DIR)
    tmp = USERS_FILE.with_name(f".users.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, USERS_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _cache["sig"], _cache["users"] = _sig(), users


def _new_admin_record():
    pw = os.environ.get("RCONSOLE_ADMIN_PASSWORD") or DEFAULT_PASSWORD
    return {"password": _hash(pw), "is_admin": True}


def seed_defaults():
    """Create the default admin if it does not exist yet."""
    with _USERS_LOCK:
        users = _load()
        if DEFAULT_ADMIN not in users:
            users[DEFAULT_ADMIN] = _new_admin_record()
            _save(users)


def reset_to_defaults():
    """Factory reset: wipe all users and recreate the default admin."""
    with _USERS_LOCK:
        _save({DEFAULT_ADMIN: _new_admin_record()})


# ---------------------------------------------------------------------- queries
def verify(username, password):
    with _USERS_LOCK:
        users = _load()
        u = users.get(username)
        # Run a dummy derivation for unknown users so response time does not
        # reveal which usernames exist.
        if not u:
            _derive(password or "", "0" * 32, PEPPER)
            return False
        ok, upgrade = _check(u["password"], password or "")
        if not ok:
            return False
        if upgrade:
            u["password"] = _hash(password)
            _save(users)
        return True


def get_user(username):
    with _USERS_LOCK:
        u = _read_cached().get(username)
        return dict(u) if u else None


def is_admin(username):
    u = get_user(username)
    return bool(u and u.get("is_admin"))


def list_users():
    with _USERS_LOCK:
        return list(_read_cached().keys())


# -------------------------------------------------------------------- mutations
def create_user(username, password, is_admin=False):
    with _USERS_LOCK:
        users = _load()
        if username in users:
            return False, "User already exists"
        if not _valid_username(username):
            return False, _USERNAME_ERR
        err = validate_password(password)
        if err:
            return False, err
        users[username] = {"password": _hash(password), "is_admin": is_admin}
        _save(users)
        return True, None


def set_password(username, password):
    with _USERS_LOCK:
        users = _load()
        if username not in users:
            return False, "No such user"
        err = validate_password(password)
        if err:
            return False, err
        if is_default_credential(username, password):
            return False, "Choose a password other than the default"
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
            return False, _USERNAME_ERR
        users[new] = users.pop(old)
        _save(users)
        return True, None


def _only_admin(users, username):
    admins = [n for n, u in users.items() if u.get("is_admin")]
    return admins == [username]


def set_admin(username, is_admin):
    with _USERS_LOCK:
        users = _load()
        if username not in users:
            return False, "No such user"
        if not is_admin and _only_admin(users, username):
            return False, "Cannot revoke the last admin"
        users[username]["is_admin"] = bool(is_admin)
        _save(users)
        return True, None


def delete_user(username):
    with _USERS_LOCK:
        users = _load()
        if username not in users:
            return False, "No such user"
        if _only_admin(users, username):
            return False, "Cannot delete the last admin"
        users.pop(username)
        _save(users)
        return True, None


# -------------------------------------------------------------------- throttle
class LoginThrottle:
    """In-memory brute-force protection with exponential lockout.

    Keys are ``(ip, username)`` so one attacker cannot lock a legitimate user
    out from a different address, yet a single address cannot hammer one
    account. State is per-process (rconsole runs a single worker).
    """

    def __init__(self, max_attempts=None, base=None, cap=None, clock=time.monotonic):
        self.max_attempts = max_attempts or config.LOGIN_MAX_ATTEMPTS
        self.base = base or config.LOGIN_LOCKOUT_BASE
        self.cap = cap or config.LOGIN_LOCKOUT_MAX
        self._clock = clock
        self._lock = threading.Lock()
        self._fails = {}   # key -> [count, strikes, locked_until]

    def retry_after(self, key):
        """Seconds until ``key`` may try again (0 if allowed now)."""
        with self._lock:
            rec = self._fails.get(key)
            if not rec:
                return 0
            return max(0, int(rec[2] - self._clock() + 0.999))

    def failure(self, key):
        with self._lock:
            if len(self._fails) > 10_000:  # bound memory under a spray attack
                self._fails.clear()
            rec = self._fails.setdefault(key, [0, 0, 0.0])
            rec[0] += 1
            if rec[0] >= self.max_attempts:
                rec[1] += 1
                rec[0] = 0
                rec[2] = self._clock() + min(self.cap, self.base * 2 ** (rec[1] - 1))

    def success(self, key):
        with self._lock:
            self._fails.pop(key, None)


THROTTLE = LoginThrottle()
