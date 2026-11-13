"""Every environment option, editable from the dashboard.

Each :class:`Setting` describes one ``RCONSOLE_*`` variable: its type, whether a change takes effect at once
(``live``) or needs a restart, and how the value reaches the running server. A change is

1. validated,
2. saved in ``<data dir>/settings.json`` (see :mod:`settingsfile`; it wins over the real environment variable),
3. put in ``os.environ`` and in the matching :mod:`config` attribute, and
4. passed to the module that owns the feature (``hooks``), so e.g. the keep-alive starts or stops at once.

Anything that cannot change while the server runs (the port, the thread count, ...) is saved and marked
*restart needed*; the dashboard offers a restart button. Secrets (tokens, the passphrase) are write-only:
the dashboard can set or clear them but is never sent their value.

Not editable on purpose: ``RCONSOLE_DATA_DIR`` (the file lives there), ``RCONSOLE_SECRET``,
``RCONSOLE_PEPPER`` (changing them logs everybody out or invalidates every password) and
``RCONSOLE_ADMIN_PASSWORD`` (initial password only; use ``sudo cpass``). Docker/compose/VM-only variables are
shown read-only.
"""
import os
import re
import threading

import config
import settingsfile

_lock = threading.RLock()
_pending_restart = set()          # env names changed since start that only a restart applies
_hooks = {}                        # env name -> [callable(value)]


def _bool(raw, default):
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _int(raw, default, lo=None, hi=None):
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _csv_hosts(raw):
    return tuple(h.strip().lower().split("://", 1)[-1].rstrip("/") for h in (raw or "").split(",") if h.strip())


def _csv_urls(raw):
    return tuple(u.strip() for u in (raw or "").split(",") if u.strip())


class Setting:
    def __init__(self, env, group, label, kind, *, attr=None, conv=None, default="", live=True, choices=(),
                 lo=None, hi=None, secret=False, help="", module=None, placeholder="", unit=""):
        self.env, self.group, self.label, self.kind = env, group, label, kind
        self.attr = attr                  # config attribute that mirrors it (or None)
        self.module = module              # alternative: (module name, attribute) to set
        self.conv = conv                  # raw string (or None) -> typed value
        self.default = default            # what the dashboard shows as the default
        self.live = live
        self.choices = tuple(choices)
        self.lo, self.hi, self.secret = lo, hi, secret
        self.help, self.placeholder, self.unit = help, placeholder, unit

    def typed(self, raw):
        return self.conv(raw) if self.conv else raw


def _B(env, group, label, attr, default, **kw):
    return Setting(env, group, label, "bool", attr=attr, default="1" if default else "0",
                   conv=lambda raw, d=default: _bool(raw, d), **kw)


def _I(env, group, label, attr, default, lo, hi=None, **kw):
    return Setting(env, group, label, "int", attr=attr, default=str(default), lo=lo, hi=hi,
                   conv=lambda raw, d=default, lo=lo, hi=hi: _int(raw, d, lo, hi), **kw)


def _S(env, group, label, attr, default="", **kw):
    return Setting(env, group, label, "str", attr=attr, default=default,
                   conv=lambda raw, d=default: (raw or "").strip() or d, **kw)


def _C(env, group, label, attr, default, choices, **kw):
    return Setting(env, group, label, "choice", attr=attr, default=default, choices=choices,
                   conv=lambda raw, d=default, ch=tuple(choices): (str(raw or "").strip().lower()
                                                                  if str(raw or "").strip().lower() in ch else d), **kw)


def _K(env, group, label, attr, **kw):
    """A secret: write-only in the dashboard."""
    return Setting(env, group, label, "secret", attr=attr, secret=True, conv=lambda raw: raw or "", **kw)


_PLATFORM_DEFAULT = bool(config.PLATFORM)

SETTINGS = [
    # ---- server
    _I("PORT", "server", "Port", None, 5000, 1, 65535, live=False,
       help="Port to listen on. Most platforms set it for you."),
    _I("RCONSOLE_THREADS", "server", "Threads", None, 64, 4, 1024, live=False,
       help="gunicorn threads: roughly the number of open terminals."),
    _C("RCONSOLE_LOG_LEVEL", "server", "Log level", None, "info",
       ("debug", "info", "warning", "error", "critical"), live=False),
    _B("RCONSOLE_TRUST_PROXY", "server", "Trust proxy headers", "TRUST_PROXY", _PLATFORM_DEFAULT, live=False,
       help="Trust X-Forwarded-* from one proxy hop."),
    _B("RCONSOLE_COOKIE_SECURE", "server", "Secure session cookie", "COOKIE_SECURE", _PLATFORM_DEFAULT,
       help="HTTPS-only session cookie. Turn it on behind your own HTTPS proxy."),
    Setting("RCONSOLE_ALLOWED_ORIGINS", "server", "Allowed origins", "str", attr="ALLOWED_ORIGINS",
            conv=_csv_hosts, help="Extra hostnames the console is served from, comma separated.",
            placeholder="console.example.com"),
    Setting("RCONSOLE_PUBLIC_URL", "server", "Public URL", "str", attr="PUBLIC_URL",
            conv=lambda raw: config.detect_public_url(),
            help="The site's public URL (the keep-alive pings it).", placeholder="https://my-app.onrender.com"),
    _S("RCONSOLE_HOST_HOME", "server", "Host shell home", "HOST_HOME", "/home/rconsole",
       help="Where the host shell starts."),
    # ---- login
    _I("RCONSOLE_MIN_PASSWORD", "login", "Minimum password length", "MIN_PASSWORD_LEN", 8, 4, 128),
    _I("RCONSOLE_MAX_USERS", "login", "Account limit", "MAX_USERS", 10, 1, 1000),
    _I("RCONSOLE_METRICS_INTERVAL", "login", "Dashboard sample interval (s)", "METRICS_INTERVAL", 5, 2, 300),
    _I("RCONSOLE_LOGIN_ATTEMPTS", "login", "Failed logins before lockout", "LOGIN_MAX_ATTEMPTS", 5, 1, 100),
    _I("RCONSOLE_LOGIN_LOCKOUT", "login", "Base lockout (s)", "LOGIN_LOCKOUT_BASE", 30, 1, 3600),
    # ---- systems
    _B("RCONSOLE_VM", "systems", "Linux systems (vm)", "VM_ENABLED", True),
    _C("RCONSOLE_DEFAULT_MODE", "systems", "Default mode for admins", "DEFAULT_ADMIN_MODE", "host", ("host", "vm")),
    Setting("RCONSOLE_DEFAULT_SYSTEM", "systems", "Default system", "str", module=("distros", "DEFAULT_DISTRO"),
            default="debian", conv=lambda raw: (raw or "").strip().lower() or "debian",
            help="System `vm install` uses when you name none."),
    _B("RCONSOLE_ALLOW_USER_HOST", "systems", "Let normal users run on the host", "ALLOW_USER_HOST", False,
       help="Remote code execution for every account. Leave it off."),
    Setting("RCONSOLE_USER_VM_PROC", "systems", "Full /proc in VMs without a PID namespace", "bool",
            default="0", conv=lambda raw: _bool(raw, False)),
    _C("RCONSOLE_VM_PIDNS", "systems", "PID namespace per VM", "VM_PIDNS", "auto", ("auto", "on", "off")),
    Setting("RCONSOLE_VM_ROOTFS_URL", "systems", "Root filesystem URL override", "str",
            help="Used for every system you install."),
    Setting("RCONSOLE_VM_ROOTFS_SHA256", "systems", "Root filesystem SHA-256", "str"),
    Setting("RCONSOLE_VM_PROOT", "systems", "proot binary", "str", help="Path to proot 5.5.0 or newer."),
    # ---- services
    _B("RCONSOLE_SUPERVISOR", "services", "Service supervisor", "SUPERVISOR", True, live=False),
    # ---- files
    _I("RCONSOLE_MAX_UPLOAD_MB", "files", "Largest upload (MB)", "MAX_UPLOAD_MB", 1024, 1, 100000),
    _I("RCONSOLE_MAX_EXTRACT_MB", "files", "Largest extraction (MB)", "MAX_EXTRACT_MB", 2048, 16, 1000000),
    # ---- keepalive
    _B("RCONSOLE_KEEPALIVE", "keepalive", "Keep-alive self-ping", "KEEPALIVE", config.PLATFORM == "render"),
    _I("RCONSOLE_KEEPALIVE_INTERVAL", "keepalive", "Ping interval (s)", "KEEPALIVE_INTERVAL", 600, 60, 86400),
    Setting("RCONSOLE_KEEPALIVE_EXTRA", "keepalive", "Extra URLs to ping", "str", attr="KEEPALIVE_EXTRA",
            conv=_csv_urls, help="Comma separated."),
    # ---- backup
    _S("RCONSOLE_BACKUP_URL", "backup", "Backup repo URL", "BACKUP_URL",
       help="A PRIVATE git repository. Repo storage uses the same repository.",
       placeholder="https://github.com/you/rconsole-data.git"),
    _K("RCONSOLE_BACKUP_TOKEN", "backup", "Repo token", "BACKUP_TOKEN",
       help="Token with Contents: read and write on that repository."),
    _S("RCONSOLE_BACKUP_USER", "backup", "Git user name", "BACKUP_USER", "x-access-token"),
    _S("RCONSOLE_BACKUP_BRANCH", "backup", "Backup branch", "BACKUP_BRANCH", "rconsole-backup"),
    _S("RCONSOLE_BACKUP_DIR", "backup", "Backup folder", "BACKUP_DIR", help="Back up to a folder instead of git."),
    _K("RCONSOLE_BACKUP_PASSPHRASE", "backup", "Encryption passphrase", "BACKUP_PASSPHRASE",
       help="Encrypts backups and repo storage. Keep a copy elsewhere: without it nothing can be read."),
    _B("RCONSOLE_BACKUP_ENCRYPT", "backup", "Encrypt backups", "BACKUP_ENCRYPT", True),
    _I("RCONSOLE_BACKUP_INTERVAL", "backup", "Backup interval (s)", "BACKUP_INTERVAL", 900, 60, 604800),
    _I("RCONSOLE_BACKUP_MAX_MB", "backup", "Largest snapshot (MB)", "BACKUP_MAX_MB", 40, 1, 100000),
    _B("RCONSOLE_RESTORE_SETUP", "backup", "Re-run vm setup after restore", "RESTORE_SETUP", True),
    _B("RCONSOLE_RESTORE_DEPS", "backup", "Reinstall dependencies after restore", "RESTORE_DEPS", True),
    # ---- repo storage
    Setting("RCONSOLE_STORAGE", "storage", "Where files live", "choice", attr="STORAGE", default="host",
            choices=("host", "github"),
            conv=lambda raw: (str(raw or "").strip().lower() if str(raw or "").strip().lower() in ("host", "github")
                              else config.default_storage()),
            help="host: on this machine's disk. github: in the repo above. Left alone it is github as soon as "
                 "the repo, token and passphrase are set (and host if the repo is not set up)."),
    _B("RCONSOLE_STORAGE_ENCRYPT", "storage", "Encrypt files in the repo", "STORAGE_ENCRYPT", True,
       help="Needs the passphrase. Off stores file names and contents as plain text in the repo."),
    _S("RCONSOLE_STORAGE_BRANCH", "storage", "Storage branch", "STORAGE_BRANCH", "rconsole-storage"),
    _I("RCONSOLE_STORAGE_CHUNK_MB", "storage", "Split files into pieces of (MB)", "STORAGE_CHUNK_MB", 48, 1, 95,
       help="GitHub refuses files over 100 MB, so bigger files are split."),
    _I("RCONSOLE_STORAGE_CACHE_MB", "storage", "Local cache size (MB)", "STORAGE_CACHE_MB", 1024, 16, 10_000_000,
       help="Above this, the least recently used big files become placeholders again."),
    _I("RCONSOLE_STORAGE_STUB_MB", "storage", "Files from this size can be placeholders (MB)", "STORAGE_STUB_MB",
       4, 1, 100000),
    _I("RCONSOLE_STORAGE_INTERVAL", "storage", "Sync every (s)", "STORAGE_INTERVAL", 20, 5, 3600),
    # ---- update
    _S("RCONSOLE_UPDATE_REPO", "update", "Update source repo", "UPDATE_REPO", "Salehin-07/rconsole-ark"),
    _K("RCONSOLE_UPDATE_TOKEN", "update", "Update token", "UPDATE_TOKEN"),
    Setting("RCONSOLE_UPDATE_API", "update", "GitHub API base for updates", "str", attr="UPDATE_API",
            default="https://api.github.com",
            conv=lambda raw: (raw or "").strip().rstrip("/") or "https://api.github.com"),
]
BY_ENV = {s.env: s for s in SETTINGS}
_UNITS = {"RCONSOLE_THREADS": "threads", "RCONSOLE_MIN_PASSWORD": "characters", "RCONSOLE_MAX_USERS": "accounts",
          "RCONSOLE_METRICS_INTERVAL": "seconds", "RCONSOLE_LOGIN_ATTEMPTS": "attempts",
          "RCONSOLE_LOGIN_LOCKOUT": "seconds", "RCONSOLE_MAX_UPLOAD_MB": "MB", "RCONSOLE_MAX_EXTRACT_MB": "MB",
          "RCONSOLE_KEEPALIVE_INTERVAL": "seconds", "RCONSOLE_BACKUP_INTERVAL": "seconds",
          "RCONSOLE_BACKUP_MAX_MB": "MB", "RCONSOLE_STORAGE_CHUNK_MB": "MB", "RCONSOLE_STORAGE_CACHE_MB": "MB",
          "RCONSOLE_STORAGE_STUB_MB": "MB", "RCONSOLE_STORAGE_INTERVAL": "seconds", "PORT": ""}
for _e, _u in _UNITS.items():
    BY_ENV[_e].unit = _u

GROUP_TITLES = [
    ("storage", "Files in your GitHub repo", "Piece size, cache and sync timing for repo storage."),
    ("backup", "Backups and the repo", "The private repository, its token and passphrase, and backup timing."),
    ("server", "Server", "Port, threads, HTTPS and proxy behaviour."),
    ("login", "Accounts and login", "Password rules, account limit and lockout."),
    ("systems", "Linux systems", "How virtual Linux systems are created and run."),
    ("services", "Services", "The supervisor that keeps your bots and sites running."),
    ("files", "Files", "Upload and extraction limits in the file manager."),
    ("keepalive", "Staying awake", "Pings that stop a free host from sleeping."),
    ("update", "Updating", "Where updates come from."),
]

# Variables the dashboard shows but cannot change, with the reason.
READ_ONLY = [
    ("RCONSOLE_DATA_DIR", "the settings file lives there"),
    ("RCONSOLE_SECRET", "changing it signs everybody out"),
    ("RCONSOLE_PEPPER", "changing it invalidates every password"),
    ("RCONSOLE_ADMIN_PASSWORD", "initial password only; use `sudo cpass`"),
    ("RCONSOLE_SERVER", "read by serve.py when it starts"),
    ("RCONSOLE_HOST", "read by serve.py when it starts"),
    ("RCONSOLE_RUN_AS_ROOT", "read by the Docker entrypoint"),
    ("RCONSOLE_DOMAIN", "read by the compose files"),
    ("RCONSOLE_VERSION", "read by the compose files"),
]


# ---------------------------------------------------------------- state
def register_hook(env, fn):
    """Call ``fn(typed_value)`` whenever ``env`` changes at runtime (also on reset)."""
    with _lock:
        _hooks.setdefault(env, []).append(fn)


def saved():
    return settingsfile.load(config.DATA_DIR)


def source(s, saved_values):
    if s.env in saved_values:
        return "dashboard"
    orig = settingsfile.ORIGINAL.get(s.env, os.environ.get(s.env))
    return "environment" if orig else "default"


def current_raw(s):
    return os.environ.get(s.env)


def _display(s, raw):
    """The value in force, as the dashboard should show it: the real number or text, not a placeholder."""
    if s.kind == "secret":
        return ""
    typed = s.typed(raw) if s.conv else raw
    if s.kind == "bool":
        return "1" if typed else "0"
    if isinstance(typed, (tuple, list)):
        return ", ".join(str(x) for x in typed)
    return "" if typed is None else str(typed)


def snapshot():
    """Everything the dashboard needs: grouped settings with their current values (never secrets)."""
    sv = saved()
    groups = []
    for key, title, blurb in GROUP_TITLES:
        rows = []
        for s in (x for x in SETTINGS if x.group == key):
            raw = current_raw(s)
            orig = settingsfile.ORIGINAL.get(s.env, os.environ.get(s.env)) if s.env in sv else None
            row = {"env": s.env, "label": s.label, "kind": s.kind, "live": s.live, "help": s.help, "unit": s.unit,
                   "default": s.default,
                   "fallback": "" if s.secret else (orig if orig else _display(s, None)), "choices": list(s.choices), "min": s.lo, "max": s.hi,
                   "source": source(s, sv), "placeholder": s.placeholder,
                   "pending_restart": s.env in _pending_restart}
            if s.secret:
                row["set"] = bool(raw)
            else:
                row["value"] = _display(s, raw)
            rows.append(row)
        groups.append({"key": key, "title": title, "blurb": blurb, "settings": rows})
    ro = [{"env": e, "why": w, "value": ("" if e in ("RCONSOLE_SECRET", "RCONSOLE_PEPPER",
                                                      "RCONSOLE_ADMIN_PASSWORD") else os.environ.get(e, ""))}
          for e, w in READ_ONLY]
    return {"groups": groups, "read_only": ro, "restart_needed": sorted(_pending_restart)}


# ---------------------------------------------------------------- validation
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


def validate(s, value):
    """Return ``(clean_string, error)`` for a value typed into the dashboard."""
    v = "" if value is None else str(value).strip()
    if s.kind == "secret":
        return v, None
    if s.kind == "bool":
        if isinstance(value, bool):
            return ("1" if value else "0"), None
        if v.lower() in ("1", "true", "yes", "on"):
            return "1", None
        if v.lower() in ("0", "false", "no", "off"):
            return "0", None
        return None, f"{s.label}: use on or off"
    if s.kind == "int":
        try:
            n = int(v)
        except ValueError:
            return None, f"{s.label}: not a number"
        if (s.lo is not None and n < s.lo) or (s.hi is not None and n > s.hi):
            return None, f"{s.label}: must be between {s.lo} and {s.hi}"
        return str(n), None
    if s.kind == "choice":
        if v.lower() not in s.choices:
            return None, f"{s.label}: choose one of {', '.join(s.choices)}"
        return v.lower(), None
    if len(v) > 2000 or "\n" in v or "\r" in v or "\x00" in v:
        return None, f"{s.label}: invalid value"
    if s.env in ("RCONSOLE_BACKUP_URL", "RCONSOLE_PUBLIC_URL", "RCONSOLE_UPDATE_API") and v:
        if not re.match(r"^(https?://|git@|ssh://|file://|/)", v):
            return None, f"{s.label}: must be a URL"
    if s.env in ("RCONSOLE_BACKUP_BRANCH", "RCONSOLE_STORAGE_BRANCH") and v and not _BRANCH_RE.match(v):
        return None, f"{s.label}: not a valid branch name"
    if s.env == "RCONSOLE_STORAGE_BRANCH" and v and v == (os.environ.get("RCONSOLE_BACKUP_BRANCH") or "rconsole-backup"):
        return None, "the storage branch must differ from the backup branch"
    if s.env == "RCONSOLE_DEFAULT_SYSTEM" and v:
        import distros
        if v.lower() not in distros.CATALOG:
            return None, f"{s.label}: unknown system (choose one of {', '.join(sorted(distros.CATALOG))})"
    return v, None


# ---------------------------------------------------------------- applying
def _apply_one(s):
    raw = os.environ.get(s.env)
    typed = s.typed(raw)
    if s.attr:
        setattr(config, s.attr, typed)
    if s.module:
        import importlib
        try:
            setattr(importlib.import_module(s.module[0]), s.module[1], typed)
        except Exception:
            pass
    for fn in list(_hooks.get(s.env, ())):
        try:
            fn(typed)
        except Exception:
            pass
    return typed


def apply_saved():
    """Re-read the settings file and apply it (used after a restore brought the file back)."""
    vals = settingsfile.inject(config.DATA_DIR)
    for env in vals:
        s = BY_ENV.get(env)
        if s and s.live:
            _apply_one(s)
    return len(vals)


def set_values(changes, actor=""):
    """Apply ``{env: value}`` (``None`` resets that option to the environment/default).

    Returns ``(ok, message, restart_needed)``. Nothing is changed if any value is invalid."""
    if not isinstance(changes, dict) or not changes:
        return False, "nothing to change", []
    clean, errors = {}, []
    for env, val in changes.items():
        s = BY_ENV.get(env)
        if s is None:
            errors.append(f"{env}: not a setting you can change here")
            continue
        if val is None:
            clean[env] = None
            continue
        v, err = validate(s, val)
        if err:
            errors.append(err)
        else:
            clean[env] = v
    if errors:
        return False, "; ".join(errors[:4]), []
    with _lock:
        sv = settingsfile.load(config.DATA_DIR)
        for env, v in clean.items():
            if v is None:
                sv.pop(env, None)
            else:
                sv[env] = v
        try:
            settingsfile.save(sv, config.DATA_DIR)
        except OSError as e:
            return False, f"cannot save the settings file: {e.strerror or e}", []
        restart = []
        for env, v in clean.items():
            s = BY_ENV[env]
            if v is None:
                orig = settingsfile.ORIGINAL.pop(env, None)
                if orig is None:
                    os.environ.pop(env, None)
                else:
                    os.environ[env] = orig
            else:
                settingsfile.ORIGINAL.setdefault(env, os.environ.get(env))
                os.environ[env] = v
            if s.live:
                _apply_one(s)
                _pending_restart.discard(env)
            else:
                _pending_restart.add(env)
                restart.append(env)
    # the storage mode follows the repo being set up unless it was chosen explicitly
    if any(e in clean for e in ("RCONSOLE_BACKUP_URL", "RCONSOLE_BACKUP_TOKEN", "RCONSOLE_BACKUP_PASSPHRASE",
                                "RCONSOLE_STORAGE_ENCRYPT")):
        _apply_one(BY_ENV["RCONSOLE_STORAGE"])
    names = ", ".join(s_label(e) for e in clean)
    msg = f"saved: {names}" if not restart else f"saved: {names}. Restart the server to apply: " + \
        ", ".join(s_label(e) for e in restart)
    return True, msg, restart


def s_label(env):
    s = BY_ENV.get(env)
    return s.label if s else env


def pending_restart():
    return sorted(_pending_restart)
