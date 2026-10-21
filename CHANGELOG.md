# Changelog

All notable changes. The format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

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
