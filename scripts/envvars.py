"""The one authoritative list of every environment variable Rconsole reads.

The configuration tables in README.md, FEATURES.md and the documentation website are *generated* from
``VARS`` by ``scripts/build_docs.py``. ``tests/test_docs.py`` fails when

* the code reads a variable that is not listed here (or this list names one the code never reads),
* a generated table in the docs is out of date,
* any document mentions a variable that does not exist.

So to add, rename or remove a setting: change the code, change this file, run
``python scripts/build_docs.py --write`` and commit the result.
"""

# area, name, default, description, scope
#   scope: all     - read by the server everywhere
#          docker  - read by the Docker image only (Dockerfile / docker-entrypoint.sh)
#          compose - read by deploy/docker-compose*.yml and the Caddyfile only (not by the server)
#          serve   - read by serve.py only
#          vm      - read inside Rconsole VMs by the bundled docker / drun wrappers
AREAS = [
    ("server", "Server"),
    ("login", "Accounts and login"),
    ("systems", "Linux systems (VMs)"),
    ("services", "Services and hosted apps"),
    ("files", "Files"),
    ("keepalive", "Staying awake"),
    ("backup", "Backups and restore"),
    ("storage", "Files in your GitHub repo"),
    ("update", "Updating"),
    ("deploy", "Docker Compose files"),
    ("docker", "Docker compatibility layer (inside VMs)"),
]

VARS = [
    # ---- server --------------------------------------------------------------------------------
    ("server", "PORT", "`5000`",
     "Port to listen on. Most platforms set it for you.", "all"),
    ("server", "RCONSOLE_DATA_DIR", "`./data` (`/data` in the Docker image; the attached volume on Railway)",
     "Where users, state, secrets, VMs, logs, the workspace and update files live. "
     "**Point it at a persistent disk**, or configure backups.", "all"),
    ("server", "RCONSOLE_THREADS", "`64`",
     "gunicorn threads. Each open terminal holds one for as long as the tab is connected, "
     "so this is roughly the number of terminals.", "all"),
    ("server", "RCONSOLE_LOG_LEVEL", "`info`", "gunicorn log level.", "all"),
    ("server", "RCONSOLE_SERVER", "auto",
     "`serve.py` only: force `gunicorn` or `builtin`. Default: gunicorn where it works, "
     "otherwise the built-in threaded server (Windows).", "serve"),
    ("server", "RCONSOLE_HOST", "`0.0.0.0`",
     "`serve.py` built-in server only: bind address. gunicorn always binds `0.0.0.0`.", "serve"),
    ("server", "RCONSOLE_TRUST_PROXY", "on when a managed platform is detected, else off",
     "Trust `X-Forwarded-*` headers (one proxy hop). Detected platforms: Render, Railway, Fly.io, Koyeb, "
     "Heroku, Google Cloud Run, Azure.", "all"),
    ("server", "RCONSOLE_COOKIE_SECURE", "on when a managed platform is detected, else off",
     "Mark the session cookie `Secure` (HTTPS only). Turn it on yourself behind your own HTTPS proxy.", "all"),
    ("server", "RCONSOLE_ALLOWED_ORIGINS", "-",
     "Extra hostnames the console is served from, comma separated. Normally auto-detected from `Host`, "
     "`X-Forwarded-Host` and the platform hostname; set it only if you see `cross-origin request blocked`.", "all"),
    ("server", "RCONSOLE_PUBLIC_URL", "auto-detected on Render, Railway, Fly.io, Koyeb, Heroku",
     "The site's public URL. The keep-alive pings it.", "all"),
    ("server", "RCONSOLE_RUN_AS_ROOT", "`0`",
     "Docker image only: keep root inside the container so an admin's host shell is real root "
     "(single-owner setups). Default: drop to an unprivileged user.", "docker"),
    # ---- login ---------------------------------------------------------------------------------
    ("login", "RCONSOLE_SECRET", "random, persisted in the data dir",
     "Session signing key. Never passed to user commands or hosted apps.", "all"),
    ("login", "RCONSOLE_PEPPER", "random, persisted in the data dir",
     "Password-hash pepper. Never passed to user commands or hosted apps.", "all"),
    ("login", "RCONSOLE_ADMIN_PASSWORD", "`ark` (a password change is forced)",
     "Initial password of the `ark` admin. Set it before the first start. Never passed to user commands.", "all"),
    ("login", "RCONSOLE_MIN_PASSWORD", "`8`", "Minimum password length (never below 4).", "all"),
    ("login", "RCONSOLE_MAX_USERS", "`10`",
     "Account cap. Rconsole is for one person or a small team.", "all"),
    ("login", "RCONSOLE_METRICS_INTERVAL", "`5`",
     "Seconds between the admin dashboard's CPU, memory, disk and network samples (2-300). One hour of history is kept.", "all"),
    ("login", "RCONSOLE_LOGIN_ATTEMPTS", "`5`", "Failed logins before a lockout starts.", "all"),
    ("login", "RCONSOLE_LOGIN_LOCKOUT", "`30`",
     "Base lockout in seconds; it doubles on each repeat, up to 15 minutes.", "all"),
    # ---- systems -------------------------------------------------------------------------------
    ("systems", "RCONSOLE_VM", "`1`",
     "Enable the Linux systems feature (`vm`). Linux hosts only. (Inside a Linux system Rconsole sets "
     "`RCONSOLE_VM` to that system's *name*, e.g. `debian`: that is output for your scripts, not a setting.)", "all"),
    ("systems", "RCONSOLE_DEFAULT_MODE", "`host`",
     "Where an admin's plain commands run: `host` or `vm`. Non-admins always use `vm`.", "all"),
    ("systems", "RCONSOLE_DEFAULT_SYSTEM", "`debian`",
     "System that `vm install` uses when you name none (`alpine` suits 512 MB instances).", "all"),
    ("systems", "RCONSOLE_ALLOW_USER_HOST", "`0`",
     "Let **non-admins** run commands on the real host. This is remote code execution: leave it off.", "all"),
    ("systems", "RCONSOLE_USER_VM_PROC", "`0`",
     "Bind the host's full `/proc` into VMs that have no PID namespace of their own (`ps`/`top` then show "
     "host processes: see the security model). VMs with their own PID namespace always see only their own "
     "processes.", "all"),
    ("systems", "RCONSOLE_VM_PIDNS", "`auto`",
     "One PID namespace per VM. `auto`/`on`: use it when the host allows user namespaces and say so in "
     "`vm doctor` when it does not; `off`: never (VMs share the server's PID space).", "all"),
    ("systems", "RCONSOLE_HOST_HOME", "`/home/rconsole`",
     "Where the host shell starts. The Docker image links it to the persistent workspace; if it is missing "
     "or not writable, `<data>/workspace` is used.", "all"),
    ("systems", "RCONSOLE_VM_ROOTFS_URL", "-",
     "Use your own root filesystem tarball for **every** system you install (mirrors, air-gapped hosts, tests). "
     "Default: the catalog's checksum-verified image for the system you pick.", "all"),
    ("systems", "RCONSOLE_VM_ROOTFS_SHA256", "-", "Expected SHA-256 of the override tarball.", "all"),
    ("systems", "RCONSOLE_VM_PROOT", "auto-detect / download",
     "Path to a `proot` binary (5.5.0 or newer).", "all"),
    # ---- services ------------------------------------------------------------------------------
    ("services", "RCONSOLE_SUPERVISOR", "`1`", "Run the `service` supervisor.", "all"),
    # ---- files ---------------------------------------------------------------------------------
    ("files", "RCONSOLE_MAX_UPLOAD_MB", "`1024`", "Largest single file the file browser accepts (big files are sent in parts, so this is not limited by a proxy's request size).", "all"),
    ("files", "RCONSOLE_MAX_EXTRACT_MB", "`2048`",
     "Most an archive may expand to when extracted in the file browser (zip-bomb guard; minimum 16).", "all"),
    # ---- keepalive -----------------------------------------------------------------------------
    ("keepalive", "RCONSOLE_KEEPALIVE", "on on Render, off elsewhere",
     "Ping our own public URL so a free instance is not suspended for inactivity. It cannot prevent "
     "provider restarts, quota limits or maintenance.", "all"),
    ("keepalive", "RCONSOLE_KEEPALIVE_INTERVAL", "`600`", "Seconds between pings (minimum 60).", "all"),
    ("keepalive", "RCONSOLE_KEEPALIVE_EXTRA", "-", "Comma-separated extra URLs to ping as well.", "all"),
    # ---- backup --------------------------------------------------------------------------------
    ("backup", "RCONSOLE_BACKUP_URL", "-",
     "A **private** git repository that receives the snapshots.", "all"),
    ("backup", "RCONSOLE_BACKUP_TOKEN", "-",
     "Token with write access to that repository only. Not passed to user commands.", "all"),
    ("backup", "RCONSOLE_BACKUP_USER", "`x-access-token`", "Git user name paired with the token.", "all"),
    ("backup", "RCONSOLE_BACKUP_BRANCH", "`rconsole-backup`", "Branch that holds the single snapshot.", "all"),
    ("backup", "RCONSOLE_BACKUP_DIR", "-", "Back up to this folder or mount instead of a git repository.", "all"),
    ("backup", "RCONSOLE_BACKUP_PASSPHRASE", "-",
     "**Required for encrypted backups.** Keep a copy somewhere else: without it a backup cannot be read. "
     "Not passed to user commands.", "all"),
    ("backup", "RCONSOLE_BACKUP_ENCRYPT", "`1`",
     "Encrypt snapshots. Turning it off stores them in plain text.", "all"),
    ("backup", "RCONSOLE_BACKUP_INTERVAL", "`900`",
     "Seconds between backups when something changed (minimum 60).", "all"),
    ("backup", "RCONSOLE_BACKUP_MAX_MB", "`40`",
     "Largest snapshot accepted. Files over 20 MB are always left out.", "all"),
    ("backup", "RCONSOLE_RESTORE_SETUP", "`1`", "Re-run each VM's `vm setup` profiles after a restore.", "all"),
    ("backup", "RCONSOLE_RESTORE_DEPS", "`1`",
     "Reinstall each VM's recorded packages, virtualenvs and Node projects after a restore.", "all"),
    # ---- update --------------------------------------------------------------------------------
    ("storage", "RCONSOLE_STORAGE", "auto", "`host` keeps files on this disk; `github` keeps your files (workspace and each system's `/home`, `/srv`, `/data`) in the backup repo and uses the disk only as a cache. Left unset it is `github` as soon as the repo, token and passphrase are configured, otherwise `host`; it also falls back to `host` when the repo is not usable. Also switchable in the dashboard.", "all"),
    ("storage", "RCONSOLE_STORAGE_ENCRYPT", "`1`", "Encrypt file names and contents in the repo (AES-256-GCM, key from `RCONSOLE_BACKUP_PASSPHRASE`). `0` stores them as plain text.", "all"),
    ("storage", "RCONSOLE_STORAGE_BRANCH", "`rconsole-storage`", "Branch of the backup repo that holds the files (must differ from the backup branch).", "all"),
    ("storage", "RCONSOLE_STORAGE_CHUNK_MB", "`48`", "Files are split into pieces of this size before upload (1 to 95; GitHub rejects files over 100 MB).", "all"),
    ("storage", "RCONSOLE_STORAGE_CACHE_MB", "`1024`", "How much of the stored files may sit on the disk; above it, the least recently used big files become placeholders again.", "all"),
    ("storage", "RCONSOLE_STORAGE_STUB_MB", "`4`", "Only files at least this big can become placeholders; smaller files always stay on the disk.", "all"),
    ("storage", "RCONSOLE_STORAGE_INTERVAL", "`20`", "Seconds between looking for changed files to upload (minimum 5).", "all"),
    ("update", "RCONSOLE_UPDATE_REPO", "`Salehin-07/rconsole-ark`",
     "GitHub `owner/repo` that `rconsole update` reads releases from. Point it at your fork.", "all"),
    ("update", "RCONSOLE_UPDATE_TOKEN", "-",
     "Optional GitHub token for `rconsole update` (private forks, rate limits). Not passed to user commands.", "all"),
    ("update", "RCONSOLE_UPDATE_API", "`https://api.github.com`",
     "GitHub API base. Only for tests and GitHub Enterprise mirrors.", "all"),
    # ---- deploy (compose files) ----------------------------------------------------------------
    ("deploy", "RCONSOLE_DOMAIN", "- (required)",
     "`deploy/.env` only: the public hostname Caddy serves and obtains a certificate for. "
     "Also becomes `RCONSOLE_ALLOWED_ORIGINS`. The server itself never reads it.", "compose"),
    ("deploy", "RCONSOLE_VERSION", "`latest`",
     "`deploy/docker-compose.image.yml` only: the image tag to pull from `ghcr.io/salehin-07/rconsole-ark`.",
     "compose"),
    # ---- docker shim (inside VMs) --------------------------------------------------------------
    ("docker", "DRUN_MODE", "`P1` on the host shell, `F1` inside a VM",
     "`drun` / `docker run` wrapper: udocker execution mode (`P1` = proot, `F1` = fakechroot).", "vm"),
    ("docker", "DRUN_PULL_OPTS", "-", "Extra options for the image pull.", "vm"),
    ("docker", "DOCKER_SHIM_LOCAL", "`0` (`drun` sets `1`)",
     "Force the local udocker path even if `DOCKER_HOST` is set.", "vm"),
    ("docker", "DOCKER_HOST", "-",
     "With a real Docker client installed (`vm setup docker`), forwards everything to that daemon.", "vm"),
]

# Names that look like settings but are not (internal markers, heredoc delimiters, test hooks).
INTERNAL = {
    "RCONSOLE_PROFILE_LOADED",   # guard variable of the VM profile loader
    "RCONSOLE_DRUN",             # heredoc delimiter in devsetup.py
    "RCONSOLE_DOCKER_SHIM",      # heredoc delimiter in devsetup.py
    "RCONSOLE_BACKUP",           # prefix: updater.py drops every RCONSOLE_BACKUP_* variable from its child processes
    "RCONSOLE_UPDATE",           # prefix: same for RCONSOLE_UPDATE_*
    "RCONSOLE_TEST_BUSYBOX",     # tests only: path of a static busybox for the real-VM tests
}


def names():
    return [v[1] for v in VARS]


def by_area():
    out = []
    for key, title in AREAS:
        rows = [v for v in VARS if v[0] == key]
        if rows:
            out.append((key, title, rows))
    return out


def table(rows):
    lines = ["| Variable | Default | What it does |", "|---|---|---|"]
    for _area, name, default, desc, scope in rows:
        tag = {"docker": " *(Docker image)*", "compose": " *(compose files)*",
               "serve": " *(`serve.py`)*", "vm": " *(inside VMs)*"}.get(scope, "")
        lines.append(f"| `{name}`{tag} | {default} | {desc} |")
    return "\n".join(lines)


def full_reference():
    """Markdown of the complete reference, one table per area."""
    parts = []
    for _key, title, rows in by_area():
        parts.append(f"### {title}\n\n{table(rows)}\n")
    return "\n".join(parts).rstrip() + "\n"
