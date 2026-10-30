# rconsole — free hosting for your bots, websites and workers

> 🆓 **Start here → [FREE_HOSTING.md](FREE_HOSTING.md)** (host on Render's free tier from your phone) ·
> 📦 **Other hosts** → [DEPLOY.md](DEPLOY.md) ·
> 📖 **Every command** → [COMMANDS.md](COMMANDS.md) ·
> 🧾 **What's new** → [CHANGELOG.md](CHANGELOG.md) ·
> ⬇️ **Self-host from a release** → [Releases](../../releases)

**Version 1.2.3** · MIT licensed

## Why this exists

Plenty of providers give you a **free tier for a small website** — but *bot hosting* and *background workers*
are never free. If you have no budget, that leaves you with nothing: you cannot run a Telegram bot, a scraper, a
queue worker or an API with a background job anywhere, because every free plan sleeps, wipes its disk, and gives
you no shell.

rconsole is built for exactly that person. Put it on **one free web service** (Render's free tier is the
reference) and you get a small always-on Linux server that you control **from your phone, from anywhere**:

* **A real terminal and your own Linux systems** (Debian, Ubuntu, Kali, Alpine) where you are `root` and can
  install anything — no host privileges needed.
* **Run anything, keep it running**: `service add` starts bots and workers at boot and restarts them when they
  crash. Publish websites (static *or* dynamic) with `service add … --expose` / `expose`.
* **It doesn't fall asleep**: a built-in keep-alive pings its own public URL every 10 minutes — no uptime monitor
  needed.
* **A wiped disk isn't the end**: encrypted backups go to a free private git repo; a fresh instance restores
  itself, rebuilds your VMs and restarts your services. `sudo reboot` restarts in place when you just need a
  restart, with no redeploy.
* **Made for a thumb**: a responsive UI you can install as an app.

It is designed for **one person or a small team** (default cap: 10 accounts) — not for the public — so it keeps
things simple and small. Be honest about the limits: free instances have ~512 MB RAM, restart when the provider
decides, and the VM is a compatibility layer rather than a hard sandbox. FREE_HOSTING.md spells out what to expect.

## What it is

rconsole is a Flask web application that exposes a **Linux console** in the browser. It blends two modes:

1. **Line-mode console** — a stateful shell (`cd`, `help`, services, hosted-app management, user
   administration, background jobs, an AI agent) implemented natively in Python.
2. **Real interactive terminal** — full-screen, real programs (`nano`, `vim`, `python`, `bash`, `less`, `top`,
   `git`, `ssh`, …) streamed over a WebSocket-backed PTY.

Commands run on one of two **targets**:

| Target | What it is | Who |
|--------|------------|-----|
| `host` | The machine rconsole runs on, with the server's privileges | admins |
| `vm`   | **Your own Linux system** (Debian by default) where you are `root` and can `apt install` anything — works even on locked-down hosts such as Render | everyone |

---

## Features

- Browser terminal powered by **xterm.js** + a real PTY (ConPTY on Windows,
  `pty`/`select` on Linux/macOS).
- **Several virtual machines per user** (`vm`): **Alpine, Debian, Ubuntu, Kali** —
  one, some or all, each with its own root filesystem and persistent `/root`. Be
  root inside, install any package, no host privileges needed; switch with a
  dropdown (see [The VM](#the-vm-vm)).
- **`service`**: bots and workers that **start at boot and restart when they crash** (`service add tgbot --vm
  debian python bot.py`), with logs, env vars and optional web exposure.
- **Free-tier survival**: built-in **keep-alive** (never sleeps), **encrypted backups + automatic restore**
  (VMs and toolchains rebuilt, services restarted), `sudo reboot` without a redeploy.
- **Installable on a phone** (home-screen app) with a layout built for small screens.
- **`expose <port> <name>`**: run any server you like (`python main.py &`, `npm run dev`, …) on the host or in a
  VM and publish it at `https://<domain>/<name>/` — private by default, `--public` for admins.
- **`neofetch` / `fetch`** that follow your target (the VM's own system in `vm`
  mode, rconsole's in `host` mode) and adapt to phone-sized screens.
- Tabs, each owning its own terminal session that keeps running in the
  background and reappears when you switch back.
- Native, stateful console commands plus a real shell passthrough (pipes,
  redirects and `&&` work as in any shell).
- Visual file navigator (📁 button): browse, open, create/rename/delete — in the
  host filesystem (admins) or confined to your VM.
- Built-in code editor with syntax highlighting for ~65 languages.
- Hosted web apps behind a path-based proxy: start them yourself and `expose` them, or let
  the `service` supervisor run them (autostart, crash restart, logs).
- Visual file browser (📁) with **download** (files, or whole folders as a `.zip`) and
  **upload** (files or entire folders).
- Accounts with admin/non-admin roles, brute-force protection, forced
  replacement of the factory password, session timeout, and factory reset.
- Native **AI agent** (`sudo agent`) with file and shell tools.

---

## Requirements

- Python **>= 3.14** (see `.python-version`). The test-suite also passes on 3.12.
- [`uv`](https://docs.astral.sh/uv/) for dependency management (or `pip install -r requirements.txt`).
- **VM**: Linux. `proot` is fetched automatically on x86-64 (checksum-verified);
  on other CPUs install it (`apt install proot` / `apk add proot`).
- **Windows**: `pywinpty` (ConPTY) is installed automatically; the VM is unavailable there.

---

## Quick start

```bash
uv sync                      # create the environment
uv run python main.py        # http://127.0.0.1:5000
```

Log in as **`ark` / `ark`**. rconsole immediately asks you to replace that
password (`sudo cpass`) before anything else works. To skip the default
entirely, set `RCONSOLE_ADMIN_PASSWORD` before the first start.

Production (single worker + threads — see [Deployment](#deployment)):

```bash
uv run gunicorn -c gunicorn.conf.py app:app
```

---

## Configuration

Everything is configured through environment variables (see `config.py`).

| Variable | Purpose | Default |
|----------|---------|---------|
| `PORT` | Listen port | `5000` |
| `RCONSOLE_DATA_DIR` | Users, state, secrets, VMs, logs. **Point at a persistent disk** (`/data` on Render) | `./data` |
| `RCONSOLE_SECRET` | Session signing key | random, **persisted** in the data dir |
| `RCONSOLE_PEPPER` | Password-hash pepper | random, **persisted** in the data dir |
| `RCONSOLE_ADMIN_PASSWORD` | Initial password for `ark` | `ark` (forces a change) |
| `RCONSOLE_MIN_PASSWORD` | Minimum password length | `8` |
| `RCONSOLE_VM` | Enable the VM feature | `1` |
| `RCONSOLE_DEFAULT_MODE` | Where admins' plain commands run: `host` or `vm` | `host` |
| `RCONSOLE_RUN_AS_ROOT` | Docker only: keep root in the container so admins' host shell is real root (single-owner deployments; see [DEPLOY.md](DEPLOY.md#13-root-vs-unprivileged)) | `0` |
| `RCONSOLE_ALLOW_USER_HOST` | Let **non-admins** use the real host shell (full RCE — leave off) | `0` |
| `RCONSOLE_USER_VM_PROC` | Expose host `/proc` to non-admin VMs (`ps`/`top` work, but see Security) | `0` |
| `RCONSOLE_TRUST_PROXY` | Trust `X-Forwarded-*` (one proxy hop) | on when `RENDER` is set |
| `RCONSOLE_PUBLIC_URL` | The site's public URL (auto-detected on Render, Fly, Railway, Koyeb) — what the keep-alive pings | auto |
| `RCONSOLE_KEEPALIVE` / `..._INTERVAL` / `..._EXTRA` | Ping our own URL so a free instance never sleeps (default **on for Render**); seconds between pings (600); extra URLs to ping | auto / `600` / – |
| `RCONSOLE_BACKUP_URL` + `RCONSOLE_BACKUP_TOKEN` | A **private git repo** (and a token with write access to it only) that receives encrypted backups; or `RCONSOLE_BACKUP_DIR` for a folder | – |
| `RCONSOLE_BACKUP_PASSPHRASE` | **Required for backups** — encrypts them (keep a copy elsewhere!) | – |
| `RCONSOLE_BACKUP_INTERVAL` / `RCONSOLE_BACKUP_MAX_MB` | Seconds between backups when something changed (900) · size limit (40) | `900` / `40` |
| `RCONSOLE_MAX_USERS` | Account cap — this is a one-person / small-team tool | `10` |
| `RCONSOLE_MAX_UPLOAD_MB` | Largest single file the file browser's upload accepts | `64` |
| `RCONSOLE_DEFAULT_SYSTEM` | System `vm install` uses by default (`alpine` suits 512 MB instances) | `debian` |
| `RCONSOLE_SUPERVISOR` | Run the service supervisor | `1` |
| `RCONSOLE_ALLOWED_ORIGINS` | Extra hostnames the console is served from (comma separated). Auto-detected via `Host`, `X-Forwarded-Host` and Render's hostname; set it only if you still see `cross-origin request blocked` | – |
| `RCONSOLE_COOKIE_SECURE` | `Secure` session cookie (HTTPS only) | on when `RENDER` is set |
| `RCONSOLE_LOGIN_ATTEMPTS` / `RCONSOLE_LOGIN_LOCKOUT` | Failed logins before lockout / base lockout seconds (doubles per repeat, max 15 min) | `5` / `30` |
| `RCONSOLE_THREADS` | gunicorn threads (≈ open terminals) | `64` |
| `RCONSOLE_VM_ROOTFS_URL` / `RCONSOLE_VM_ROOTFS_SHA256` | Use your own root filesystem tarball (e.g. Debian) | Alpine latest-stable |
| `RCONSOLE_VM_PROOT` | Path to a `proot` binary | auto-detect / download |

Secrets that rconsole generates are stored `0600` in the data dir, so sessions
and password hashes survive restarts and are shared by every process. They are
**never** passed to user commands or hosted apps.

---

## The VM (`vm`)

Every user can have private Linux systems — **as many as you like**: just one,
two, or all four at once. Each is a separate machine with its **own root
filesystem and its own `/root`**; nothing is shared. Inside one you are `root`
and can install anything. It needs **no host privileges** — that is the point: on
Render (or any container that gives you no `sudo`) the VM still gives you a fully
permissive machine.

```text
vm                    your systems and the active one     vm use <name>         make one the default
vm list               systems you can install             vm mode [host|vm [n]] where plain commands run
vm install <name>...  e.g. debian ubuntu:24.04 | all      vm reset [name] --yes recreate system files (keeps /root)
vm shell [name]       interactive shell in a VM           vm remove <name|all> --yes   delete it and its /root
vm run [--vm n] <cmd> run one command in a VM             vm doctor [name]      diagnose a VM
vm setup <profile>    install Python / build tools / Node / … in a VM (see below)
```

### Pick your systems

`vm list` shows everything available; `vm install <name>[:<release>]` installs it.
Install several in one go: `vm install debian ubuntu kali`, or `vm install all`.

| Name | System | Packages | Download | Notes |
|------|--------|----------|----------|-------|
| `debian` (**default**) | Debian 13 "trixie" | `apt install` | ~90 MB | The universal choice. `debian:bookworm`, `debian:sid`, … |
| `ubuntu` | Ubuntu (newest LTS, 26.04) | `apt install` | ~35 MB | `ubuntu:24.04`, `ubuntu:22.04`, … |
| `kali` (also `কালী`) | Kali Linux (minimal) | `apt install` | ~140 MB | Security-testing distro — authorized use only. Add tools with `apt install kali-linux-headless` etc. |
| `alpine` | Alpine Linux | `apk add` | ~3 MB | Tiny and the fastest to set up. |

Plain `vm install` (no name) installs **Debian**.

Every download is verified against the checksum the distribution itself publishes.

### Install Python and build tools in one command

`vm setup <profile>` installs a ready-made toolchain **inside a VM** as a background
job with live progress (it keeps going if you close the tab; `jobs`, `fg %n`):

| Profile | Installs |
|---------|----------|
| `python` | Python 3, pip, venv, a compiler **and the development headers** that packages like **psycopg2**, mysqlclient, Pillow, lxml and cryptography need to build (PostgreSQL, MySQL/MariaDB, SQLite, libffi, OpenSSL, XML, JPEG/PNG/WebP, LDAP, YAML, …) |
| `build` | gcc, g++, make, cmake, autotools, pkg-config |
| `node` | Node.js and npm |
| `rust` | Rust and cargo (for wheels that compile from source) |
| `db` | `psql`, `mysql`, `redis-cli`, `sqlite3` |
| `tools` | nano, vim, wget, curl, git, ps, htop, zip, jq, ssh, … |
| `docker` | Docker CLI, compose, buildx. **Client only**: a VM (proot) can't run the Docker daemon, so point it at a remote engine with `DOCKER_HOST`. For simple images with no engine at all, use `drun <image>` in the host shell |
| `kali` | **Kali VMs only**: the command-line Kali toolset (`kali-linux-headless`) and the basics it needs. Several GB, long install |
| `all` | `tools`, `build`, `python`, `node`, `rust`, `db` (not `docker`/`kali`) |

```text
$ vm setup python            # (add a name if you have several: vm setup python kali)
$ pip install psycopg2 mysqlclient Pillow lxml      # builds from source, no flags needed
$ vm setup docker            # Docker client + compose
$ vm setup kali              # in a Kali VM (vm install kali first): the Kali tools
```

The right package names are used for each system (`libpq-dev` on Debian-family,
`postgresql-dev` on Alpine), and if one package is missing on a given release only
that one is skipped. Inside a VM `pip install` works without
`--break-system-packages`, and installs never stop to ask questions (no more
"Please select the geographic area" from tzdata) — `DEBIAN_FRONTEND=noninteractive`
is always set.

### Working with several systems

- **One system installed:** everything just works; you never type its name.
- **Several installed:** pick where things run — whichever is easiest:
  - the **dropdown in the tab bar** (🖥 Host · 🐧 Debian · 🐧 Kali · *Install another…*)
    — on a **phone** it lives in the **⚙ settings menu** together with the theme
    button (same controls, just tidier; wider screens keep them in the bar);
  - `vm use debian` / `vm mode vm debian`;
  - one-offs: `vm shell kali`, `vm run --vm ubuntu apt update`, or prefix any
    command with `@vm:kali` (e.g. `@vm:kali nmap -V`).
- If a command needs a system and you have not picked one, a **dropdown appears
  right in the output** listing your systems *and* the ones you can still install;
  choose one and the command runs. Your choice is remembered.
- The **prompt shows where you are**: `ark@rconsole:/srv$` on the host,
  `ark@debian:/root$` in a VM.
- **Admins** can use both `host` and any VM (`vm mode host`). **Normal users** go
  straight to their VMs and never see the host.

```text
$ vm install debian kali          # two systems, installed one after the other
$ vm                              # shows both and which is active
$ vm shell kali                   # a root shell in Kali
root@kali:~# apt update && apt install -y nmap
$ vm use debian                   # plain commands now run in Debian
ark@debian:/root$ python3 --version
```

### How it works

Each system is a root filesystem run under [proot](https://proot-me.github.io/)
(user-space `chroot` + fake root, no kernel privileges). The file navigator,
editor, `fm`, `cd` and the agent are all confined to the chosen system's
filesystem — `..` and symlinks cannot escape it. rconsole uses **proot 5.5.0**: on
x86-64 it downloads that exact, checksum-verified build the first time it is
needed, because older proots (such as the one Ubuntu/Debian package) cannot run
Ubuntu 26.04's Rust-based coreutils (`coreutils: unknown program 'prooted-…'`).
Installs run a self-test and fail with a clear message rather than leave a broken VM.

Data lives in `<data>/vm/<user>/<system>/{rootfs,home}`. `vm reset <name> --yes`
recreates a system's files but keeps its `/root`; `vm remove` deletes both.
Existing single-VM installs from earlier versions are moved into this layout
automatically.

Things to know:

- **It is not a hard security boundary.** proot is a compatibility layer:
  guest processes share the server's uid and PID namespace. Only create
  accounts for people you trust. (Non-admin guests only see the harmless
  `/proc/{uptime,meminfo,cpuinfo,loadavg,version}` files, not host processes.)
- **Long commands:** a plain command or `vm run` is cut off after 60–90 s (hosting
  platforms drop HTTP requests around 100 s). For `apt upgrade`, builds and the
  like use `vm shell` (no limit, Ctrl+C works) or append `&` to run a background job.
- No `systemd`, no mounting, no raw sockets/kernel features; some syscalls are
  emulated and a few programs misbehave. If something needs a real kernel, use a
  VPS or QEMU — see [DEPLOY.md](DEPLOY.md).
- **proot needs `ptrace`.** If your host blocks it, `vm install` reports why and
  removes the half-built VM instead of leaving it broken. **`vm doctor`** runs a
  checklist (shell, programs, root, write access, DNS, internet, package manager)
  and shows exactly which step fails.
- Other CPU architectures: install `proot` 5.5+ yourself (the Dockerfile adds the
  distro one, which may be older). Debian, Ubuntu and Kali publish arm64 images too.
- Hosted apps in a VM: run them inside the VM (`vm setup python`, then
  `service add app --vm debian --cwd /root/app --expose 8000:app gunicorn -b 127.0.0.1:8000 app:app`).

### `neofetch` / `fetch`

`neofetch` and `fetch` follow your current target, and adapt to your screen so
they look right on a phone as well as a desktop:

- **In a VM** they show that VM's own system — distro, package count, shell — by
  running a small `rfetch` shim that rconsole installs inside every VM. The same
  `neofetch` works when you type it inside `vm shell`, whichever system you chose.
- **On the host** they show rconsole's own summary of the server.
- `rfetch` always shows rconsole's. Flags: `--no-logo`, `--plain` (VM shim).

Layout is chosen from your screen width: logo beside the details on wide screens,
a short logo above on medium ones, and details only (long values shortened with
`~`) on narrow phones. If you install the real `neofetch`/`fastfetch` in a VM it
lives at `/usr/bin/…`; call it by that path to use it instead of the shim.

### Choosing the target

| | host | vm |
|---|---|---|
| Admin default | ✅ (`RCONSOLE_DEFAULT_MODE=host`) | `vm use <name>` |
| Non-admin | ❌ always refused | ✅ the only option |
| Prefix a single command | `@host cmd` (admins) | `@vm cmd` (active) or `@vm:<name> cmd` |

All of this is enforced in one place (`sandbox.py`), so it cannot be bypassed by
calling the terminal websocket or the file APIs directly.

---

## Using the console

### Line-mode commands

| Command | Description |
|---------|-------------|
| `help` | List commands. |
| `clear` / `cls` | Clear the screen. |
| `cd <dir>` | Change directory (stateful; `~` supported; confined to the VM in `vm` mode). |
| `history`, `rwhoami`, `alias`, `unalias`, `theme` | Console helpers. |
| `edit <file>` / `fm [path]` | Built-in editor / file manager (mobile friendly). |
| `service add\|list\|start\|stop\|restart\|logs\|rm` | Supervised bots/workers — see [COMMANDS.md](COMMANDS.md#service). Everyone. |
| `keepalive` · `sudo backup` · `sudo reboot --yes` | Stay awake, back up/restore, restart in place — see [FREE_HOSTING.md](FREE_HOSTING.md). |
| `expose <port> <name>` · `expose list` · `expose rm <name>` | Publish a server you started at `/<name>/` — see [Hosting web apps](#hosting-web-apps). Everyone. |
| `version` | Print the rconsole version. |
| `vm …` | Your systems (`vm list`, `vm install`, `vm shell`, `vm use`, `vm doctor`, …) — see above. Available to everyone. |
| `neofetch` / `fetch` / `rfetch` | System summary: your VM's (in `vm` mode) or rconsole's (`host` mode); `rfetch` is always rconsole's. Fits your screen width. |
| `<cmd> &`, `jobs`, `jobout %n`, `jobtail %n`, `fg %n`, `kill %n` | Background jobs. |
| `logout` | Log out (anyone). |
| `sudo cpass` / `cuser` | Change a password / rename a user (state and VM move with the name). |
| `sudo muser` / `ruser` | Create / delete a user (their VM and state are removed too). |
| `sudo suser` / `auser <u>` / `nauser <u>` | List users / grant / revoke admin. |
| `sudo rboot` | Factory reset: kill apps, wipe state and VMs, reset users. |
| `sudo kprocess [pid]` | Kill a process (or everything this session hosts). Refuses to kill the server itself. |
| `sudo env …`, `sudo agent`, `sudo opencode` | Per-user env vars, AI agent, opencode launcher. |

Commands prefixed with `sudo` require an **admin** account; a non-admin who types
`sudo anything` is refused.

### Real commands

Anything that isn't a built-in runs in your current target through a real shell,
so pipes, redirects, `&&`, globbing and quoting behave as in any shell:

```text
$ ls -la | grep py > files.txt && wc -l files.txt
```

Interactive programs (`nano vim less top python bash ssh git log …`) open in a
real PTY window. Close it with the **✕ button in the top-right corner** (works on
phones, where there is no Esc key) or press **Esc**: either one ends the session and
returns you to the console. Other commands have a 60-second timeout —
append ` &` to run longer ones as a job.

---

## Hosting web apps

Two ways, both behind rconsole's login (unless you use `expose --public`):

**`expose` — you start the program, rconsole publishes it.** Works for anything that serves HTTP, on the host
or inside any VM:

```text
$ python main.py &              # or: flask run -p 8000, npm run dev, uvicorn app:app --port 8000, …
$ expose 8000 myapp             # -> https://<your-domain>/myapp/
$ expose list                   # [up]/[down] status
$ expose rm myapp
```
Apps that don't know they live under a path prefix can use `expose 8000 myapp --rewrite` (fixes root-relative
links in HTML/CSS). The proxy sends `X-Forwarded-Prefix`, so prefix-aware frameworks work natively. Details and
limits (no WebSockets) are in [COMMANDS.md](COMMANDS.md#expose).

**`service` — rconsole starts and keeps the app running** (this replaces `sudo serve` / `sudo gunicorn`,
removed in 1.2.2):

```text
$ service add site --vm debian --cwd /root/site --expose 8000:site python3 -m http.server 8000
$ service add app  --vm debian --cwd /root/app  --expose 8000:app  gunicorn -b 127.0.0.1:8000 app:app
$ service logs app
```

- Apps bind `127.0.0.1:<port>` and are reachable only through the proxy at
  `https://<your-domain>/<name>/…`, and only by logged-in users.
- The proxy never forwards rconsole's own session cookie to your app, and
  passes multiple `Set-Cookie` headers through intact (Django's `csrftoken` +
  `sessionid` work).

---

## AI agent (`sudo agent`) — in-console, no PTY

rconsole ships a **native, line-mode AI coding agent** so you get tool-using
AI assistance without fighting a browser-hostile PTY TUI. It calls an
OpenAI-compatible (or Anthropic) API directly and can run tools **inside
your current target (host or VM — see `vm mode`)** — `bash`, `read_file`, `write_file`,
`list_dir` — all rendered through the normal line console.

### Providers (first match wins)

| Provider       | Key(s)                                  | Base URL (default)                    |
|----------------|-----------------------------------------|---------------------------------------|
| **OpenCode Zen** | `OPENCODE_API_KEY` (alias `ZEN_API_KEY`) | `https://opencode.ai/zen/v1`        |
| Anthropic      | `ANTHROPIC_API_KEY`                     | `https://api.anthropic.com`           |
| OpenAI         | `OPENAI_API_KEY`                        | `https://api.openai.com/v1`           |

Keys are read from your **`sudo env` store** (persisted per user, injected into
every session) or the server process environment. Setting them needs no
redeploy:

```bash
sudo env set OPENCODE_API_KEY=oc-...        # from https://opencode.ai/auth
sudo env set OPENCODE_MODEL=hy3-free        # any Zen /v1/chat/completions model
sudo agent
```

OpenRouter / local OpenAI-compatible servers work by setting `OPENAI_BASE_URL`
(and `OPENAI_MODEL`). **OpenCode Zen note:** the GPT-5.x family on Zen requires
the separate `/v1/responses` API (not yet wrapped here); use a chat/completions
model such as `hy3-free`, `deepseek-v4-flash`, `glm-5.2`, `kimi-k3`, or
`nemotron-3.5-lightning-free`.

### Usage

```bash
sudo agent                       # interactive session (chat persists across commands)
sudo agent "write a flask app and run it"   # one-shot task, then stays open
sudo agent models                # list models from the current provider
sudo agent help                  # show agent slash commands
```

Inside a session, slash commands are available:

```
/help        show help            /clear      forget the conversation
/model <n>   switch model         /provider   show provider/model/base
/keys        list keys (masked)   /models     list provider models
/compact     summarise context    /system     print system prompt
/exit        leave the agent
```

### Robustness

- **Automatic retry/backoff** on `429`/`5xx` and connection errors (pooled
  `requests.Session`).
- **Persistent conversation** across `sudo agent` invocations (stored per tab;
  cleared on `/clear` or `/exit`).
- **Context compaction** — older turns are summarised automatically when the
  transcript grows large, to cap token usage and cost.
- **Token usage report** per turn and per session.
- **Safety guard** — on the *host*, the `bash` tool refuses obviously destructive
  commands (`rm -rf /`, `mkfs`, `dd` on disks, `shutdown`, …); run those directly
  in the console if you truly intend them. Inside your **VM** the guard is off:
  it is your sandbox, and `vm reset` rebuilds it.

---

## Architecture

```
Browser (xterm.js)
   │  HTTP(S)            REST /api/run  (line-mode commands)
   ▼
Flask app (app.py)   ── before_request: Origin/CSRF check → auth guard
   ├── /api/run  → interp.py ──┬─ commands.py (built-ins: cmd_*.py, editor, agent, fun)
   │                           ├─ jobs.py      (background `cmd &`)
   │                           └─ shell passthrough
   ├── /ws       → ptyhost.py → termsess.py (persistent PTY)
   ├── /api/file|dir|fs  (editor + file navigator)
   └── /<folder>/<port>/…  → proxy → hosted app process
                │
   every launch and every file access goes through
                ▼
        sandbox.py  ── authorizes (admin? mode?) and picks the target
            ├── host: the server's shell/filesystem (admins)
            └── vm:   proot + per-user Alpine rootfs, path-confined FsView
```

Key points:

- **`sandbox.py` is the single choke point.** `sandbox.spec()` turns a command
  into argv/cwd/env for the right target *and* enforces permissions;
  `sandbox.fs()` returns the path namespace for file access. The interpreter,
  jobs, PTY sessions, hosted apps, the editor, the file APIs and the AI agent all
  use them, so there is exactly one place to audit.
- Server state lives in `state.py`; users in `data/users.json` (atomic, `0600`).
- Everything is in-process memory, so run **one worker with threads**.

---

## Deployment

See **[DEPLOY.md](DEPLOY.md)** for the full guide — a checklist of what any host
must provide, a comparison table, and step-by-step instructions for **Render,
Railway, Fly.io, Koyeb/Northflank, any VPS (Docker Compose + Caddy), plain Linux
with systemd, AWS/Google Cloud/Azure, Kubernetes, and a home server or Raspberry
Pi**, plus backups, updates and troubleshooting.

The short version:

```bash
# Any VPS with Docker — automatic HTTPS:
git clone https://github.com/<you>/rconsole-ark.git && cd rconsole-ark/deploy
cp .env.example .env && nano .env          # RCONSOLE_DOMAIN + RCONSOLE_ADMIN_PASSWORD
docker compose up -d --build
```

```bash
# Or just the container:
docker build -t rconsole . && docker run -p 5000:5000 -v rconsole-data:/data \
  -e RCONSOLE_ADMIN_PASSWORD='choose-a-strong-one' rconsole
```

Rules that apply everywhere: **one instance only**, a **persistent volume** at
`RCONSOLE_DATA_DIR`, **websockets with long idle timeouts**, and **HTTPS** in front.
Config files live in [`deploy/`](deploy/) (`docker-compose.yml`, `Caddyfile`,
`nginx.conf`, `fly.toml`, `k8s.yaml`, systemd unit) plus [`render.yaml`](render.yaml).

---

## Security model

What rconsole enforces, and what it does not.

**Authentication**
- PBKDF2-HMAC-SHA256 (210k rounds), per-user salt + a random pepper that is
  persisted (never a constant in the source). Old hashes are upgraded on login.
- Failed logins are throttled per (IP, username) and per IP with exponential
  lockout; the response never reveals whether a username exists.
- The factory `ark`/`ark` login works only to set a new password.
- Minimum password length 8; usernames can't be `.`/`..` or contain path characters.
- Users file: atomic writes, `0600`, mtime-cached, and **quarantined** (not
  silently reset) if it is ever corrupt.

**Web**
- Cross-site POSTs and websocket handshakes are rejected (Origin /
  `Sec-Fetch-Site` check) on top of `SameSite=Lax`, `HttpOnly`, and `Secure`
  cookies; `X-Frame-Options`, `nosniff` and HSTS (over HTTPS) are set.
- The session cookie is named `rconsole_session` so hosted Flask apps' own
  `session` cookie can't collide with or clobber it.

**Authorization**
- Host access is admin-only and enforced in `sandbox.spec()`, which runs for the
  line console, jobs, hosted apps **and the terminal websocket** (whose command
  arrives from the browser and is untrusted). File APIs are confined by
  `sandbox.fs()`; a non-admin can reach only their own VM filesystem.
- Server secrets are stripped from the environment of every user process.

**Not covered** — be honest with yourself about these:
- The VM is not a hardened sandbox (shared uid/PID namespace). Treat every
  account as trusted, or run one container per tenant.
- Admins are root-equivalent on the server's container by design.
- Hosted apps run as the server's user and are not isolated from each other.
- API keys saved with `sudo env` are stored in `data/state/*.json` (`0600`,
  plaintext). Use disk encryption if that matters.
- No TLS in-process: terminate HTTPS at the platform/proxy.

---

## Testing

```bash
uv run python -m unittest discover -s tests          # ~100 tests
RCONSOLE_VM_PROOT=/path/to/proot RCONSOLE_TEST_BUSYBOX=/path/to/busybox \
  uv run python -m unittest discover -s tests -v     # include real-VM end-to-end tests
uv run ruff check .
```

The suite covers hashing/migration, throttling, path-confinement (symlink and
`..` escapes), authorization of every entry point, CSRF, the file APIs, the
proxy, and — when proot and a static busybox are present — a real VM built from
a locally served root filesystem (download → checksum → extract → run).

---

## Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| "Password change required" / commands refused after login | You used the default password. Run `sudo cpass`. |
| `host access is restricted to admins` | Non-admins use the VM: `vm install`, then `vm shell`. |
| `vm install` fails: "proot cannot run" | Either the platform blocks `ptrace` (rconsole already retries without seccomp acceleration — use a VPS then), or the proot is too old (`unknown program 'prooted-…'`): rconsole fetches 5.5.0 on x86-64; elsewhere install a newer one. |
| `bash: cannot set terminal process group … no job control` | Fixed in current versions (the terminal now gets a controlling terminal). Update. |
| Kali prints "mkdir: cannot create directory '/dev/net'" or a "minimal installation" banner | Fixed: every VM gets a private writable `/dev/net` and the banner is hidden by `~/.hushlogin`. Update rconsole; new VMs get it at install, existing ones on the next launch (the banner file: `touch ~/.hushlogin`). |
| `apt install` stops at "Please select the geographic area" | Fixed: VMs never ask questions (`DEBIAN_FRONTEND=noninteractive`). Update rconsole. |
| `pip install psycopg2` fails to build ("pg_config executable not found") | Run `vm setup python` first (installs the compiler and `libpq-dev`/`postgresql-dev`). |
| A VM command hangs or "timed out" | `apt`/builds can exceed the 60–90 s limit for plain commands: use `vm shell` or add `&`. `vm doctor` shows if DNS/network is the cause. |
| "Choose a system" dropdown keeps appearing | You have several VMs and no default: pick one (it is remembered), or `vm use <name>`. |
| `vm install` fails to download | Server has no outbound HTTPS, or the mirror is blocked. Set `RCONSOLE_VM_ROOTFS_URL` to a reachable tarball. |
| `apt: not found` / `apk: not found` in the VM | Different systems, different tools: Alpine uses `apk add`, Debian/Ubuntu/Kali use `apt install`. `vm status` shows which one you have. |
| `vm install debian` says "already installed" | Switch with `vm reset --yes` (keeps `/root`), then `vm install debian`. |
| Logged out after every deploy | No persistent disk / `RCONSOLE_DATA_DIR`; the generated secrets were lost. Mount a disk or set `RCONSOLE_SECRET`. |
| `{"error":"cross-origin request blocked"}` after login | Your proxy rewrites `Host` without `X-Forwarded-Host`. The server log names the blocked host — add it to `RCONSOLE_ALLOWED_ORIGINS`. |
| "Too many failed attempts" | Login lockout; wait the stated seconds. |
| Hosted app exits immediately | The error shows the app's log; in VM mode install `python3`/`gunicorn` in the VM (`vm setup python`). `service logs <name>` shows its output. |
| Instance falls asleep / bots stop after a while | `keepalive` — is there a public URL and is it enabled? See [FREE_HOSTING.md](FREE_HOSTING.md#stay-awake). |
| Everything is gone after a redeploy | Backups weren't configured (`sudo backup` shows what's missing) — see [FREE_HOSTING.md](FREE_HOSTING.md). |
| `uv: command not found` (or `cargo`, `bun`, …) right after installing it in a VM | Fixed: plain commands now have `~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, `~/go/bin` on `PATH` and read `~/.profile`. Update rconsole. (Interactive `vm shell` always worked because the installer's `source` line ran there.) |
| `expose` link gives 502 | Nothing is listening on that port yet (`expose list` shows `[down]`) — start your app. |
| Hosted app not reachable | Is it running (`service list`) and exposed (`expose list`)? Open `/<name>/`. |
| Changes not taking effect | No auto-reload; restart the server. Hard-refresh for static assets. |

---

## File reference

See [`structure.md`](structure.md) for every module and what it does.

---

## License

[MIT](LICENSE) © 2026 Salehin-07 (MD ABU SALEHIN).
