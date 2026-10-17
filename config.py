"""Central, environment-driven configuration for rconsole.

Every tunable lives here so deployments (Render, Docker, a VPS) are configured
purely through environment variables and nothing is hard-coded elsewhere.

Environment variables
---------------------
``RCONSOLE_DATA_DIR``          Where users, state, secrets and VMs live
                               (default ``./data``). Point it at a persistent
                               disk (e.g. ``/data`` on Render) to keep them.
``RCONSOLE_SECRET``            Flask session signing key. If unset, a random
                               key is generated once and persisted in the data
                               dir so sessions survive restarts.
``RCONSOLE_PEPPER``            Password-hash pepper (same persistence rule).
``RCONSOLE_ADMIN_PASSWORD``    Initial password for the ``ark`` admin account.
                               If unset the legacy ``ark``/``ark`` default is
                               used, but the console forces a password change.
``RCONSOLE_MIN_PASSWORD``      Minimum password length (default 8).
``RCONSOLE_VM``                ``1``/``0`` — enable the built-in VM (default 1).
``RCONSOLE_DEFAULT_MODE``      ``host`` or ``vm`` for admins (default ``host``;
                               non-admins always default to ``vm``).
``RCONSOLE_ALLOW_USER_HOST``   ``1`` lets *non-admin* users run commands on the
                               real host. Off by default — it is full RCE.
``RCONSOLE_TRUST_PROXY``       Honour ``X-Forwarded-*`` headers (default on when
                               running on Render).
``RCONSOLE_COOKIE_SECURE``     Mark the session cookie ``Secure`` (default on
                               when running on Render).
``RCONSOLE_VM_ROOTFS_URL``     Override the root filesystem tarball URL.
``RCONSOLE_VM_ROOTFS_SHA256``  Expected sha256 for the override tarball.
``RCONSOLE_VM_PROOT``          Path to a proot binary (default: auto-detect).
"""
import os
import secrets
from pathlib import Path

HERE = Path(__file__).resolve().parent


def env_bool(name, default=False):
    """Read a boolean environment variable (1/true/yes/on)."""
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


ON_RENDER = bool(os.environ.get("RENDER"))

DATA_DIR = Path(os.environ.get("RCONSOLE_DATA_DIR") or (HERE / "data")).resolve()
STATE_DIR = DATA_DIR / "state"
VM_DIR = DATA_DIR / "vm"
LOG_DIR = DATA_DIR / "logs"

MIN_PASSWORD_LEN = max(4, env_int("RCONSOLE_MIN_PASSWORD", 8))

VM_ENABLED = env_bool("RCONSOLE_VM", True)
DEFAULT_ADMIN_MODE = (os.environ.get("RCONSOLE_DEFAULT_MODE") or "host").strip().lower()
if DEFAULT_ADMIN_MODE not in ("host", "vm"):
    DEFAULT_ADMIN_MODE = "host"
ALLOW_USER_HOST = env_bool("RCONSOLE_ALLOW_USER_HOST", False)

TRUST_PROXY = env_bool("RCONSOLE_TRUST_PROXY", ON_RENDER)
COOKIE_SECURE = env_bool("RCONSOLE_COOKIE_SECURE", ON_RENDER)

# Login throttling: after MAX_ATTEMPTS failures a key is locked out, with the
# lockout doubling on each repeat up to LOCKOUT_MAX seconds.
LOGIN_MAX_ATTEMPTS = env_int("RCONSOLE_LOGIN_ATTEMPTS", 5)
LOGIN_LOCKOUT_BASE = env_int("RCONSOLE_LOGIN_LOCKOUT", 30)
LOGIN_LOCKOUT_MAX = 15 * 60

# Environment variables that must never leak into user commands or hosted apps.
SECRET_ENV_VARS = (
    "RCONSOLE_SECRET", "RCONSOLE_PEPPER", "RCONSOLE_ADMIN_PASSWORD",
)


def ensure_private_dir(path):
    """Create ``path`` (and parents) with owner-only permissions."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def persistent_secret(env_name, filename, nbytes=32):
    """Return a secret from ``env_name`` or a random one persisted on disk.

    Persisting the generated value keeps sessions and password hashes valid
    across restarts and across multiple worker processes.
    """
    val = os.environ.get(env_name)
    if val:
        return val
    ensure_private_dir(DATA_DIR)
    path = DATA_DIR / filename
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    new = secrets.token_hex(nbytes)
    try:
        # O_EXCL: if two workers race, exactly one wins and the other re-reads.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        return new
    except FileExistsError:
        try:
            return path.read_text(encoding="utf-8").strip() or new
        except OSError:
            return new
    except OSError:
        # Read-only data dir: fall back to a per-process secret.
        return new
