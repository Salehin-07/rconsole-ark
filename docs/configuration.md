# Configuration

Rconsole is configured only through environment variables. They are read by `config.py` (and a few by `serve.py`,
`gunicorn.conf.py`, the Docker entrypoint and the Compose files). **This page is generated from one list**
(`scripts/envvars.py`), and a test fails if the code reads a variable that is not on it, so the names and defaults
here are the real ones.

## How to set them

| Where you run | How |
|---|---|
| Render | Dashboard → your service → *Environment*, or the `envVars` of the blueprint ([`render.yaml`](../render.yaml)) |
| Docker Compose | `deploy/.env` next to `docker-compose.yml` (start from [`deploy/.env.example`](../deploy/.env.example)) |
| systemd | `/etc/rconsole.env` loaded by the unit (start from [`deploy/rconsole.env.example`](../deploy/rconsole.env.example)) |
| `docker run` | `-e NAME=value` |
| Locally | `export NAME=value` before `uv run python serve.py` |

Changed a variable on a running instance? Restart it: `sudo reboot --yes` restarts in place, so the new value is read.

> **Release asset name.** On GitHub Releases the Compose example is attached as `env.example` (GitHub renames
> dot-files on upload). In the repository it is `deploy/.env.example`. Copy either to `.env`.

## Platform detection

Rconsole recognises these hosts from *their* variables and then turns on proxy trust and secure cookies, accepts the
platform hostname, and (where noted) finds the volume and public URL.

| Platform | Detected by |
|---|---|
| Render | `RENDER` (hostname from `RENDER_EXTERNAL_HOSTNAME`, URL from `RENDER_EXTERNAL_URL`) |
| Railway | `RAILWAY_PROJECT_ID` or `RAILWAY_ENVIRONMENT` (hostname `RAILWAY_PUBLIC_DOMAIN`; volume `RAILWAY_VOLUME_MOUNT_PATH`) |
| Fly.io | `FLY_APP_NAME` (hostname `<app>.fly.dev`) |
| Koyeb | `KOYEB_APP_NAME` (hostname `KOYEB_PUBLIC_DOMAIN`) |
| Heroku | `DYNO` (hostname `HEROKU_APP_DEFAULT_DOMAIN_NAME`) |
| Google Cloud Run | `K_SERVICE` |
| Azure | `WEBSITE_SITE_NAME` or `CONTAINER_APP_NAME` |

Only Render turns the keep-alive on by default.

## Secrets that never reach your commands

These variables are removed from the environment of every command, terminal, job, service and hosted app that runs on
the host shell. Linux systems (VMs) never inherit the server's environment at all.

`RCONSOLE_SECRET`, `RCONSOLE_PEPPER`, `RCONSOLE_ADMIN_PASSWORD`, `RCONSOLE_UPDATE_TOKEN`, `RCONSOLE_BACKUP_TOKEN`,
`RCONSOLE_BACKUP_PASSPHRASE`.

Other secrets are different: values you save with `sudo env` or `service add --env` are stored in the data directory
(`0600`, plain JSON), and are encrypted only inside backups. See the [security model](security.md).

## Reference

<!-- BEGIN GENERATED: env-reference -->

### Server

| Variable | Default | What it does |
|---|---|---|
| `PORT` | `5000` | Port to listen on. Most platforms set it for you. |
| `RCONSOLE_DATA_DIR` | `./data` (`/data` in the Docker image; the attached volume on Railway) | Where users, state, secrets, VMs, logs, the workspace and update files live. **Point it at a persistent disk**, or configure backups. |
| `RCONSOLE_THREADS` | `64` | gunicorn threads. Each open terminal holds one for as long as the tab is connected, so this is roughly the number of terminals. |
| `RCONSOLE_LOG_LEVEL` | `info` | gunicorn log level. |
| `RCONSOLE_SERVER` *(`serve.py`)* | auto | `serve.py` only: force `gunicorn` or `builtin`. Default: gunicorn where it works, otherwise the built-in threaded server (Windows). |
| `RCONSOLE_HOST` *(`serve.py`)* | `0.0.0.0` | `serve.py` built-in server only: bind address. gunicorn always binds `0.0.0.0`. |
| `RCONSOLE_TRUST_PROXY` | on when a managed platform is detected, else off | Trust `X-Forwarded-*` headers (one proxy hop). Detected platforms: Render, Railway, Fly.io, Koyeb, Heroku, Google Cloud Run, Azure. |
| `RCONSOLE_COOKIE_SECURE` | on when a managed platform is detected, else off | Mark the session cookie `Secure` (HTTPS only). Turn it on yourself behind your own HTTPS proxy. |
| `RCONSOLE_ALLOWED_ORIGINS` | - | Extra hostnames the console is served from, comma separated. Normally auto-detected from `Host`, `X-Forwarded-Host` and the platform hostname; set it only if you see `cross-origin request blocked`. |
| `RCONSOLE_PUBLIC_URL` | auto-detected on Render, Railway, Fly.io, Koyeb, Heroku | The site's public URL. The keep-alive pings it. |
| `RCONSOLE_RUN_AS_ROOT` *(Docker image)* | `0` | Docker image only: keep root inside the container so an admin's host shell is real root (single-owner setups). Default: drop to an unprivileged user. |

### Accounts and login

| Variable | Default | What it does |
|---|---|---|
| `RCONSOLE_SECRET` | random, persisted in the data dir | Session signing key. Never passed to user commands or hosted apps. |
| `RCONSOLE_PEPPER` | random, persisted in the data dir | Password-hash pepper. Never passed to user commands or hosted apps. |
| `RCONSOLE_ADMIN_PASSWORD` | `ark` (a password change is forced) | Initial password of the `ark` admin. Set it before the first start. Never passed to user commands. |
| `RCONSOLE_MIN_PASSWORD` | `8` | Minimum password length (never below 4). |
| `RCONSOLE_MAX_USERS` | `10` | Account cap. Rconsole is for one person or a small team. |
| `RCONSOLE_METRICS_INTERVAL` | `5` | Seconds between the admin dashboard's CPU, memory, disk and network samples (2-300). One hour of history is kept. |
| `RCONSOLE_LOGIN_ATTEMPTS` | `5` | Failed logins before a lockout starts. |
| `RCONSOLE_LOGIN_LOCKOUT` | `30` | Base lockout in seconds; it doubles on each repeat, up to 15 minutes. |

### Linux systems (VMs)

| Variable | Default | What it does |
|---|---|---|
| `RCONSOLE_VM` | `1` | Enable the Linux systems feature (`vm`). Linux hosts only. (Inside a Linux system Rconsole sets `RCONSOLE_VM` to that system's *name*, e.g. `debian`: that is output for your scripts, not a setting.) |
| `RCONSOLE_DEFAULT_MODE` | `host` | Where an admin's plain commands run: `host` or `vm`. Non-admins always use `vm`. |
| `RCONSOLE_DEFAULT_SYSTEM` | `debian` | System that `vm install` uses when you name none (`alpine` suits 512 MB instances). |
| `RCONSOLE_ALLOW_USER_HOST` | `0` | Let **non-admins** run commands on the real host. This is remote code execution: leave it off. |
| `RCONSOLE_USER_VM_PROC` | `0` | Bind the host's full `/proc` into VMs that have no PID namespace of their own (`ps`/`top` then show host processes: see the security model). VMs with their own PID namespace always see only their own processes. |
| `RCONSOLE_VM_PIDNS` | `auto` | One PID namespace per VM. `auto`/`on`: use it when the host allows user namespaces and say so in `vm doctor` when it does not; `off`: never (VMs share the server's PID space). |
| `RCONSOLE_HOST_HOME` | `/home/rconsole` | Where the host shell starts. The Docker image links it to the persistent workspace; if it is missing or not writable, `<data>/workspace` is used. |
| `RCONSOLE_VM_ROOTFS_URL` | - | Use your own root filesystem tarball for **every** system you install (mirrors, air-gapped hosts, tests). Default: the catalog's checksum-verified image for the system you pick. |
| `RCONSOLE_VM_ROOTFS_SHA256` | - | Expected SHA-256 of the override tarball. |
| `RCONSOLE_VM_PROOT` | auto-detect / download | Path to a `proot` binary (5.5.0 or newer). |

### Services and hosted apps

| Variable | Default | What it does |
|---|---|---|
| `RCONSOLE_SUPERVISOR` | `1` | Run the `service` supervisor. |

### Files

| Variable | Default | What it does |
|---|---|---|
| `RCONSOLE_MAX_UPLOAD_MB` | `64` | Largest single file the file browser accepts. |
| `RCONSOLE_MAX_EXTRACT_MB` | `2048` | Most an archive may expand to when extracted in the file browser (zip-bomb guard; minimum 16). |

### Staying awake

| Variable | Default | What it does |
|---|---|---|
| `RCONSOLE_KEEPALIVE` | on on Render, off elsewhere | Ping our own public URL so a free instance is not suspended for inactivity. It cannot prevent provider restarts, quota limits or maintenance. |
| `RCONSOLE_KEEPALIVE_INTERVAL` | `600` | Seconds between pings (minimum 60). |
| `RCONSOLE_KEEPALIVE_EXTRA` | - | Comma-separated extra URLs to ping as well. |

### Backups and restore

| Variable | Default | What it does |
|---|---|---|
| `RCONSOLE_BACKUP_URL` | - | A **private** git repository that receives the snapshots. |
| `RCONSOLE_BACKUP_TOKEN` | - | Token with write access to that repository only. Not passed to user commands. |
| `RCONSOLE_BACKUP_USER` | `x-access-token` | Git user name paired with the token. |
| `RCONSOLE_BACKUP_BRANCH` | `rconsole-backup` | Branch that holds the single snapshot. |
| `RCONSOLE_BACKUP_DIR` | - | Back up to this folder or mount instead of a git repository. |
| `RCONSOLE_BACKUP_PASSPHRASE` | - | **Required for encrypted backups.** Keep a copy somewhere else: without it a backup cannot be read. Not passed to user commands. |
| `RCONSOLE_BACKUP_ENCRYPT` | `1` | Encrypt snapshots. Turning it off stores them in plain text. |
| `RCONSOLE_BACKUP_INTERVAL` | `900` | Seconds between backups when something changed (minimum 60). |
| `RCONSOLE_BACKUP_MAX_MB` | `40` | Largest snapshot accepted. Files over 20 MB are always left out. |
| `RCONSOLE_RESTORE_SETUP` | `1` | Re-run each VM's `vm setup` profiles after a restore. |
| `RCONSOLE_RESTORE_DEPS` | `1` | Reinstall each VM's recorded packages, virtualenvs and Node projects after a restore. |

### Files in your GitHub repo

| Variable | Default | What it does |
|---|---|---|
| `RCONSOLE_STORAGE` | `host` | `host` keeps files on this disk; `github` keeps your files (workspace and each system's `/home`, `/srv`, `/data`) in the backup repo and uses the disk only as a cache. Falls back to `host` when the repo is not set up. Also switchable in the dashboard. |
| `RCONSOLE_STORAGE_ENCRYPT` | `1` | Encrypt file names and contents in the repo (AES-256-GCM, key from `RCONSOLE_BACKUP_PASSPHRASE`). `0` stores them as plain text. |
| `RCONSOLE_STORAGE_BRANCH` | `rconsole-storage` | Branch of the backup repo that holds the files (must differ from the backup branch). |
| `RCONSOLE_STORAGE_CHUNK_MB` | `50` | Files are split into pieces of this size before upload (1 to 95; GitHub rejects files over 100 MB). |
| `RCONSOLE_STORAGE_CACHE_MB` | `1024` | How much of the stored files may sit on the disk; above it, the least recently used big files become placeholders again. |
| `RCONSOLE_STORAGE_STUB_MB` | `4` | Only files at least this big can become placeholders; smaller files always stay on the disk. |
| `RCONSOLE_STORAGE_INTERVAL` | `20` | Seconds between looking for changed files to upload (minimum 5). |

### Updating

| Variable | Default | What it does |
|---|---|---|
| `RCONSOLE_UPDATE_REPO` | `Salehin-07/rconsole-ark` | GitHub `owner/repo` that `rconsole update` reads releases from. Point it at your fork. |
| `RCONSOLE_UPDATE_TOKEN` | - | Optional GitHub token for `rconsole update` (private forks, rate limits). Not passed to user commands. |
| `RCONSOLE_UPDATE_API` | `https://api.github.com` | GitHub API base. Only for tests and GitHub Enterprise mirrors. |

### Docker Compose files

| Variable | Default | What it does |
|---|---|---|
| `RCONSOLE_DOMAIN` *(compose files)* | - (required) | `deploy/.env` only: the public hostname Caddy serves and obtains a certificate for. Also becomes `RCONSOLE_ALLOWED_ORIGINS`. The server itself never reads it. |
| `RCONSOLE_VERSION` *(compose files)* | `latest` | `deploy/docker-compose.image.yml` only: the image tag to pull from `ghcr.io/salehin-07/rconsole-ark`. |

### Docker compatibility layer (inside VMs)

| Variable | Default | What it does |
|---|---|---|
| `DRUN_MODE` *(inside VMs)* | `P1` on the host shell, `F1` inside a VM | `drun` / `docker run` wrapper: udocker execution mode (`P1` = proot, `F1` = fakechroot). |
| `DRUN_PULL_OPTS` *(inside VMs)* | - | Extra options for the image pull. |
| `DOCKER_SHIM_LOCAL` *(inside VMs)* | `0` (`drun` sets `1`) | Force the local udocker path even if `DOCKER_HOST` is set. |
| `DOCKER_HOST` *(inside VMs)* | - | With a real Docker client installed (`vm setup docker`), forwards everything to that daemon. |

<!-- END GENERATED: env-reference -->
