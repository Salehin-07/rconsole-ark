"""Central, environment-driven configuration for rconsole.

Every tunable lives here so deployments (Render, Docker, a VPS) are configured
purely through environment variables and nothing is hard-coded elsewhere.

Environment variables
---------------------
``RCONSOLE_DATA_DIR``          Where users, state, secrets and VMs live
                               (default ``./data``, or the attached volume on
                               Railway). Point it at a persistent disk (e.g.
                               ``/data``) to keep them across deploys.
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
``RCONSOLE_TRUST_PROXY``       Honour ``X-Forwarded-*`` headers (default on when a
                               managed platform is detected, see
                               :func:`detect_platform`).
``RCONSOLE_PUBLIC_URL``        The site's public URL (auto-detected on most platforms);
                               used by the keep-alive self-ping.
``RCONSOLE_KEEPALIVE``         Ping our own public URL so free instances never sleep
                               (default on for Render). ``..._INTERVAL`` seconds (600),
                               ``..._EXTRA`` comma list of other URLs to ping too.
``RCONSOLE_BACKUP_URL``        Private git repo to back the data up to (GitHub, GitLab,
                               ...), with ``RCONSOLE_BACKUP_TOKEN`` and a required
                               ``RCONSOLE_BACKUP_PASSPHRASE`` (backups are encrypted).
                               ``RCONSOLE_BACKUP_DIR`` backs up to a folder instead.
``RCONSOLE_MAX_USERS``         Account cap (default 10): this is for one person or a
                               small team.
``RCONSOLE_SUPERVISOR``        Run the service supervisor (default on).
``RCONSOLE_DEFAULT_SYSTEM``    System ``vm install`` uses by default (default ``debian``).
``RCONSOLE_ALLOWED_ORIGINS``   Extra hostnames the console is served from (comma
                               separated). Normally auto-detected; see README.
``RCONSOLE_COOKIE_SECURE``     Mark the session cookie ``Secure`` (default on
                               when a managed platform is detected).
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


def detect_platform(environ=None):
    """Recognise a managed host from its environment variables.

    Returns ``(name, public_hostnames, volume_path)``; ``name`` is ``None`` on a
    plain server. See also :func:`detect_public_url`. Used to turn on proxy trust / secure cookies by default,
    accept the platform's public hostname, and find an attached volume.
    """
    e = os.environ if environ is None else environ
    name, hosts, volume = None, [], None
    if e.get("RENDER"):
        name = "render"
        hosts.append(e.get("RENDER_EXTERNAL_HOSTNAME"))
    if e.get("RAILWAY_PROJECT_ID") or e.get("RAILWAY_ENVIRONMENT"):
        name = "railway"
        hosts.append(e.get("RAILWAY_PUBLIC_DOMAIN"))
        volume = e.get("RAILWAY_VOLUME_MOUNT_PATH")
    if e.get("FLY_APP_NAME"):
        name = "fly"
        hosts.append(e["FLY_APP_NAME"] + ".fly.dev")
    if e.get("KOYEB_APP_NAME"):
        name = "koyeb"
        hosts.append(e.get("KOYEB_PUBLIC_DOMAIN"))
    if e.get("DYNO"):
        name = "heroku"
        hosts.append(e.get("HEROKU_APP_DEFAULT_DOMAIN_NAME"))
    if e.get("K_SERVICE"):
        name = "cloudrun"
    if e.get("WEBSITE_SITE_NAME") or e.get("CONTAINER_APP_NAME"):
        name = "azure"
    return name, tuple(h.lower() for h in hosts if h), volume


def detect_public_url(environ=None):
    """The site's public URL as the platform reports it (or ``""``)."""
    e = os.environ if environ is None else environ
    explicit = (e.get("RCONSOLE_PUBLIC_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if e.get("RENDER_EXTERNAL_URL"):
        return e["RENDER_EXTERNAL_URL"].rstrip("/")
    for var, fmt in (("RAILWAY_PUBLIC_DOMAIN", "https://{}"), ("KOYEB_PUBLIC_DOMAIN", "https://{}"),
                     ("HEROKU_APP_DEFAULT_DOMAIN_NAME", "https://{}")):
        if e.get(var):
            return fmt.format(e[var])
    if e.get("FLY_APP_NAME"):
        return f"https://{e['FLY_APP_NAME']}.fly.dev"
    return ""


PLATFORM, PLATFORM_HOSTS, _PLATFORM_VOLUME = detect_platform()
PUBLIC_URL = detect_public_url()
ON_RENDER = PLATFORM == "render"          # kept for backwards compatibility

DATA_DIR = Path(os.environ.get("RCONSOLE_DATA_DIR") or _PLATFORM_VOLUME
                or (HERE / "data")).resolve()
STATE_DIR = DATA_DIR / "state"
VM_DIR = DATA_DIR / "vm"
LOG_DIR = DATA_DIR / "logs"
# Where host-mode commands start (and what backups include): anything you keep
# here — bots, sites, scripts — is part of the data that survives a restore.
WORKSPACE_DIR = DATA_DIR / "workspace"

MIN_PASSWORD_LEN = max(4, env_int("RCONSOLE_MIN_PASSWORD", 8))
# rconsole is built for one person or a small team on a small (often free)
# instance, not for the public: cap the number of accounts.
MAX_USERS = max(1, env_int("RCONSOLE_MAX_USERS", 10))

# --- staying awake on free tiers (see keepalive.py) ---------------------------------
# Free instances (Render, ...) sleep after ~15 minutes without inbound requests.
# Pinging our own *public* URL counts as inbound traffic, so we do it ourselves.
KEEPALIVE = env_bool("RCONSOLE_KEEPALIVE", PLATFORM == "render")
KEEPALIVE_INTERVAL = max(60, env_int("RCONSOLE_KEEPALIVE_INTERVAL", 600))
KEEPALIVE_EXTRA = tuple(u.strip() for u in
                        (os.environ.get("RCONSOLE_KEEPALIVE_EXTRA") or "").split(",") if u.strip())

# --- backups: make free, disk-less hosting survive redeploys (see backup.py) ----------
BACKUP_URL = (os.environ.get("RCONSOLE_BACKUP_URL") or "").strip()        # a private git repo
BACKUP_DIR = (os.environ.get("RCONSOLE_BACKUP_DIR") or "").strip()        # or any folder/mount
BACKUP_TOKEN = os.environ.get("RCONSOLE_BACKUP_TOKEN") or ""
BACKUP_USER = os.environ.get("RCONSOLE_BACKUP_USER") or "x-access-token"
BACKUP_PASSPHRASE = os.environ.get("RCONSOLE_BACKUP_PASSPHRASE") or ""
BACKUP_ENCRYPT = env_bool("RCONSOLE_BACKUP_ENCRYPT", True)
BACKUP_BRANCH = os.environ.get("RCONSOLE_BACKUP_BRANCH") or "rconsole-backup"
BACKUP_INTERVAL = max(60, env_int("RCONSOLE_BACKUP_INTERVAL", 900))
BACKUP_MAX_MB = max(1, env_int("RCONSOLE_BACKUP_MAX_MB", 40))
RESTORE_SETUP = env_bool("RCONSOLE_RESTORE_SETUP", True)   # re-run `vm setup` profiles after a restore

# --- the service supervisor (see services.py) ---------------------------------------
SUPERVISOR = env_bool("RCONSOLE_SUPERVISOR", True)

VM_ENABLED = env_bool("RCONSOLE_VM", True)
DEFAULT_ADMIN_MODE = (os.environ.get("RCONSOLE_DEFAULT_MODE") or "host").strip().lower()
if DEFAULT_ADMIN_MODE not in ("host", "vm"):
    DEFAULT_ADMIN_MODE = "host"
ALLOW_USER_HOST = env_bool("RCONSOLE_ALLOW_USER_HOST", False)

TRUST_PROXY = env_bool("RCONSOLE_TRUST_PROXY", PLATFORM is not None)
# Extra hostnames/origins the console may be reached at (comma separated), e.g.
# "console.example.com,https://other.example.com". Only needed if a proxy
# rewrites the Host header AND sends no X-Forwarded-Host.
ALLOWED_ORIGINS = tuple(
    h.strip().lower().split("://", 1)[-1].rstrip("/")
    for h in (os.environ.get("RCONSOLE_ALLOWED_ORIGINS") or "").split(",") if h.strip()
)
COOKIE_SECURE = env_bool("RCONSOLE_COOKIE_SECURE", PLATFORM is not None)

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
