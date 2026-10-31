# Changelog

All notable changes. The format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## 1.2.4 — 2026-10-30

### Added
- **`docker run` now works, without a daemon.** A `docker` command (a shim over udocker) is installed in the image and
  by `vm setup docker` / `vm setup udocker`. It translates `run` (`-e -v -w -u --entrypoint --name --rm --env-file`),
  `pull`, `images`, `ps`, `rm`, `rmi`, `inspect`, `search`, `login`; a run container is removed afterwards unless
  named. On the host shell it uses proot (P1) and runs anything, including static images such as hello-world. Inside a
  VM it uses fakechroot (F1), because proot cannot run inside proot: normal programs work, static binaries cannot,
  and it says so. `-p`, `--restart` and similar are ignored with a warning; `-d`, `exec`, `logs`, `build`, `compose`
  and the like explain that they need a daemon. `DOCKER_HOST` plus a real client forwards everything to that engine.
- `vm setup udocker`: the same as `vm setup docker` without the real Docker client.

### Changed
- `vm setup docker` now installs udocker and the `docker`/`drun` commands as well as the real client (which moved to
  `/usr/local/lib/docker-real` when fetched as a static binary, so it never shadows the shim).
- `drun` is now just `docker run`, always local.
- On Alpine, `vm setup docker`/`udocker` also installs GNU `findutils` (busybox `find` makes udocker print "while
  modifying attributes of image layer" for every layer) and the shim warns if it finds only busybox `find`.

## 1.2.3 — 2026-10-30

### Added
- **`drun`**: run Docker images without a Docker daemon, in the host shell (`drun hello-world`). A Render service
  and every rconsole VM is an unprivileged container, so a daemon can never run there; `drun` wraps udocker
  (proot mode) instead. It handles images that ship no `/etc` or `/root` (hello-world), reuses one container per
  image, and refuses inside a VM (proot cannot nest). Suits command-line images, not `-d`, ports, compose or
  desktops. Installed in the Docker image (`udocker==1.3.17`).

### Changed
- The `vm setup docker` closing note now points at `drun` as the no-engine option.

## 1.2.2 — 2026-10-30

(There is no 1.2.1: its tag was withdrawn before release and cannot be reused; everything from it is here.)

### Added
- **File browser: download.** Every row has a ⬇ button. A file downloads as it is; a **folder downloads as a
  `.zip`** that streams while it is built (constant memory). Symlinks inside a folder are skipped and opened
  with `O_NOFOLLOW`, so a link can never pull in a file from outside the folder or the VM.
- **File browser: upload.** **⬆ Upload files** and **⬆ Upload folder** (keeps the folder's structure). One request
  per file with live progress, three at a time; existing files are replaced only after you confirm once. Names are
  validated (no `..`, absolute or empty parts), writes are atomic, and a symlink at the target is replaced rather
  than written through. Limit per file: `RCONSOLE_MAX_UPLOAD_MB` (default 64).
- **`vm setup docker`** — Docker CLI, compose and buildx, **client only** on every system (never the engine:
  no `dockerd`, containerd, runc or iptables, which cannot run under proot and would waste ~300 MB). Where a
  release has no `docker-cli` package, Docker's static client and the compose plugin are fetched instead. The
  script says why there is no daemon and shows how to use `DOCKER_HOST` with a remote engine.
- **`vm setup kali`** — Kali VMs only (refused anywhere else): the command-line Kali toolset
  (`kali-linux-headless`, no desktop) plus the basics it needs, with a smaller fallback (`kali-tools-top10`).

### Removed
- **`sudo serve`, `sudo gunicorn` and `sudo sprocess`.** `service add … --expose PORT:NAME` (keeps it running) and
  `expose` (publishes something you started) cover them; `service logs <name>` replaces `sprocess`. The unused
  `_static_host.py` and `_wsgi_host.py` helpers went with them. `kprocess` stays.

### Changed
- `vm setup` lists `docker` and `kali`; `vm setup all` still means tools, build, python, node, rust and db; it does not include `docker` or `kali`.

## 1.2.0 — 2026-10-21

rconsole now has a clear purpose: **free hosting for bots, websites and background workers, for one person or a
small team** — on free tiers such as Render's, from a phone. This release adds what that needs. See
[FREE_HOSTING.md](FREE_HOSTING.md).

### Added
- **`service`** — a process supervisor for bots, workers and servers: named commands that **start at boot,
  restart after crashes** (exponential backoff 2 s → 1 min), run on the host or inside a VM, keep logs, and can
  be exposed on the web (`--expose PORT:NAME`). Definitions persist and are backed up.
- **Encrypted backups and automatic restore** (`backup`): snapshots of users, secrets, settings, the workspace,
  services, exposures and every VM's `/root` go — encrypted (Fernet, key from `RCONSOLE_BACKUP_PASSPHRASE`) — to a
  private git repo or a folder, every 15 min when something changed and once at shutdown. A **fresh instance
  restores itself at boot** (before secrets are generated), **rebuilds your VMs** and **re-applies your `vm setup`
  toolchains**, and services start again. The git branch is replaced on each push, so it never grows.
- **Built-in keep-alive** (`keepalive`): pings the site's own public URL so free instances never go to sleep; also
  pings extra URLs. On by default on Render.
- **`reboot`** — restart rconsole in place (final backup, stop everything, reload) without redeploying; `--hard`
  to exit the process.
- **Installable app**: web manifest and icon, so the console can be added to a phone's home screen.
- `RCONSOLE_MAX_USERS` (default 10) keeps it a small-team tool; `RCONSOLE_DEFAULT_SYSTEM` picks the default VM
  system (e.g. `alpine` on 512 MB instances); `RCONSOLE_PUBLIC_URL`.
- `FREE_HOSTING.md` and a free-tier `render.yaml`.

### Changed
- Host-mode commands now start in a persistent **workspace** (`<data>/workspace`), which is part of every backup,
  instead of the application directory.
- `render.yaml` targets Render's **free** plan; the disk-backed variant is `render.paid.yaml`.
- Dependabot updates are grouped into one pull request per ecosystem.

### Fixed
- **Slow shutdown/reload**: an open browser connection held gunicorn's graceful shutdown for the full 30 s timeout
  (and the final backup only ran after that wait). Upstream keep-alive is now off, and shutdown work starts the
  moment SIGTERM arrives. Restarts now take well under a second.
- **Orphaned processes**: stopping or reloading the server left background jobs, services and hosted apps running,
  and an old worker's supervisor could restart them while it was exiting ("address already in use" for the new
  one). Shutdown now pauses the supervisor first, stops everything it started, then takes the final backup.

### Security
- Backups are encrypted by default and refuse to run without a passphrase; tokens, passphrases and URL
  credentials are scrubbed from every message; restore only writes inside the expected folders and never follows
  symlinks or paths outside them.

## 1.1.0 — 2026-10-20

The first release of rconsole as a complete, self-hostable Linux console: private Linux VMs for every user, a
hardened security model, a phone-friendly UI, one-command publishing of your own servers, full documentation and
ready-made deployment files.

### Added
- **Several virtual machines per user** (`vm`): Alpine, Debian (default), Ubuntu and Kali — any number at once, each
  with its own root filesystem and `/root`. Runs under proot, so it needs no host privileges and works on
  locked-down hosts such as Render. `vm list`, `vm install <name>…|all`, `vm shell`, `vm run`, `vm use`, `vm mode`,
  `vm reset`, `vm remove`, `vm doctor`, and `@vm:<name>` one-off prefixes.
- **System picker and target dropdown**: commands that need a system show an inline dropdown (including installing
  another); the tab bar has a permanent dropdown, and the prompt shows where you are.
- **`vm setup <profile>`**: one command to install Python, pip, a compiler and the headers that psycopg2,
  mysqlclient, Pillow, lxml and cryptography need (`python`), plus `build`, `node`, `rust`, `db`, `tools`, `all`.
- **`expose <port> <name>`** (and `unexpose`): publish any server you started — on the host or in a VM — at
  `https://<domain>/<name>/`. Private by default, `--public` for admins, `--rewrite` for apps unaware of the prefix.
- **`neofetch` / `fetch` / `rfetch`** that follow your target (a VM's own system, or rconsole's) and fit phone screens.
- **Phone layout**: the theme button and system dropdown move into a ⚙ settings menu on small screens.
- **✕ close button** on every interactive window.
- `version` command and `/healthz` reports the version.
- **`COMMANDS.md`**: every command documented (a test keeps it complete); `DEPLOY.md` for any platform; MIT license.
- Deployment files: `Dockerfile`, `docker-compose.yml` + Caddy, nginx, Fly.io, Kubernetes, systemd, Render blueprint,
  GitHub Actions CI and a release workflow that publishes archives and a container image.
- Native commands and a native AI agent (`sudo agent`) now work inside the VM you are in.

### Changed
- Debian is the default system for `vm install`.
- Plain commands in a VM always have `~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, `~/go/bin` on `PATH`, and read
  `~/.profile` exports, so tools installed with `curl … | sh` (uv, rustup, bun, …) work outside the interactive shell.
- `pip install` works in a VM without `--break-system-packages`; installs never prompt (`DEBIAN_FRONTEND=noninteractive`).
- Configuration is entirely environment-driven (`config.py`); secrets are generated once and persisted.

### Fixed
- **Inline system picker did nothing in a real browser**: a screen-wide click handler re-focused the command box,
  closing the native dropdown the moment it opened.
- Interactive shells had no controlling terminal: `bash: cannot set terminal process group … no job control`, and
  Ctrl+C did nothing in bash-based VMs (Ubuntu, Debian, Kali).
- Ubuntu 26.04: `coreutils: unknown program 'prooted-…'` — old proot builds cannot run its Rust coreutils. rconsole
  now uses a pinned proot 5.5.0 and the install self-test explains the problem instead of leaving a broken VM.
- tzdata's "Please select the geographic area" prompt; apt's "does not have a stable CLI interface" warning.
- Kali: login script failing on `/dev/net/tun` (each VM gets a private writable `/dev/net`) and its banner.
- Pipes and redirects were quoted away (`echo hi | tr …` printed literally); native `echo`/`wc`/… shadowed pipelines.
- A corrupt user file silently recreated the default `ark`/`ark` admin; it is now quarantined.
- Hosted apps lost multiple `Set-Cookie` headers (Django's `csrftoken` + `sessionid`).

### Security
- One authorization choke point (`sandbox.py`) for every command and file access, including the terminal websocket.
  Non-admins can never reach the host; file APIs are confined to the user's VM (no `..`/symlink escapes).
- PBKDF2 hashes with a persisted random pepper (old hashes upgrade on login); login throttling; the factory
  password must be changed; minimum password length.
- Cross-site request protection (Origin / `Sec-Fetch-Site`), secure cookie flags, a distinct session cookie name,
  security headers, server secrets stripped from user processes and hosted apps.
- The VM is a compatibility layer, **not** a hard security boundary — see the README's security model.

## 0.1.0
Initial version: browser console, real PTY terminals, hosted apps, users and the AI agent.
