# rconsole features

Every feature of rconsole 1.3.0, by area. This is the **map**; the details live in
[COMMANDS.md](COMMANDS.md) (each command, option and example), [README.md](README.md) (setup, configuration,
security model), [DEPLOY.md](DEPLOY.md) and [FREE_HOSTING.md](FREE_HOSTING.md). A test fails when a command exists
that is not mentioned here or in COMMANDS.md.

**Contents** ·
[1. What it is](#1-what-it-is) ·
[2. The console](#2-the-console) ·
[3. Files and editing](#3-files-and-editing) ·
[4. Your Linux systems](#4-your-linux-systems-vm) ·
[5. Hosting](#5-hosting-apps-bots-and-workers) ·
[6. Free-tier survival](#6-free-tier-survival) ·
[7. Updating](#7-updating-rconsole-update) ·
[8. Background jobs](#8-background-jobs) ·
[9. Users and security](#9-users-and-security) ·
[10. Environment and AI](#10-environment-and-ai) ·
[11. Fun commands](#11-fun-commands) ·
[12. Running real programs](#12-running-real-programs) ·
[13. Deployment and releases](#13-deployment-and-releases) ·
[14. Settings](#14-settings) ·
[15. Limits](#15-limits)

---

## 1. What it is

A Flask web app that gives you a **Linux console in the browser** — built so bots, websites and workers can be hosted
for free on platforms such as Render. Two modes in one page:

* a native **line-mode console** (stateful `cd`, services, users, jobs, AI agent, …) written in Python, and
* a **real interactive terminal** (nano, vim, python, bash, top, ssh, …) over a WebSocket-backed PTY.

Commands run on a **target**: `host` (the server itself; admins only) or a `vm` (your own Linux system where you are
root; everyone). One worker process with threads; all state in one data directory.

---

## 2. The console

| Feature | What you get |
|---|---|
| **Line mode** | Type a command, get its output. `cd` is stateful per tab; pipes, redirects, `&&`, `;` work (a real shell runs anything that is not a built-in). |
| **Tabs** | Each tab has its own directory, history and terminal. Terminals keep running in the background and re-attach when you come back; closing a tab kills its terminal. |
| **Real terminal** | Interactive programs open in an xterm.js window with a real PTY (ConPTY on Windows): job control, Ctrl+C, resize, colours, a ✕ button (or Esc) to close. See [section 12](#12-running-real-programs). |
| **Sessions that survive** | Tabs, history, aliases and running terminals are kept across reloads and logouts. |
| **`help`** | Lists the commands; `help` also prints the version. |
| **`clear` / `cls`** | Clears the screen. |
| **`history`** | Command history of the current tab. |
| **`rwhoami`** | Prints your username. |
| **`version`** | Prints the rconsole version (also at `/healthz`). See also `rconsole version` ([section 7](#7-updating-rconsole-update)). |
| **`cd`** | Change directory (`~` supported; confined to the VM in VM mode). |
| **`alias` / `unalias`** | Persistent per-user aliases that replace the first word of a command. |
| **`theme`** | 17 colour themes (`default light amber solarized dracula github ubuntu retro nord gruvbox matrix tokyonight catppuccin monokai everforest ayu papercolor`), saved per account, switchable live from the command or the theme button. |
| **`logout`** | Signs you out; everything is kept. |
| **Mobile first** | Installable as a home-screen app (web manifest + icon); on narrow screens the tab bar shrinks to tabs, **+**, **📁**, **⚙**; a footer key bar provides Tab / Esc / Ctrl / arrows for touch keyboards; `neofetch`, `vm list` and friends reformat to your width. |
| **Update screen** | While `rconsole update` runs, every open tab shows a locked progress screen and reloads by itself ([section 7](#7-updating-rconsole-update)). |

---

## 3. Files and editing

| Feature | What you get |
|---|---|
| **`edit <file>`** | Built-in code editor with syntax highlighting for ~65 languages (300+ file extensions and special file names), mobile friendly. Works on the host (admins) or inside your VM. Files over 5 MB are refused. |
| **`fm [path]`** | In-console file manager: `cd`, `up`, `open`, `cat`, `rm`, `mkdir`, `touch`, `mv`, `?`, `q`; the directory carries over when you quit. |
| **📁 file browser** | Visual navigator: browse, open in the editor, create, rename, delete. |
| **Download** | A file as it is; a **folder as a streamed `.zip`** (symlinks are skipped so a link can never pull in outside files). |
| **Upload** | One or many files, or a whole folder with its structure kept; existing files are replaced only after one confirmation; each file is its own request so one failure does not stop the rest. |
| **Confinement** | Everything goes through the same path namespace as commands: admins see the host, everyone else only their own VM. Symlink and `..` escapes are resolved inside the VM root. |

---

## 4. Your Linux systems (`vm`)

Several systems per user at once — **Alpine, Debian, Ubuntu, Kali** — each with its own root filesystem and its own
persistent `/root`. You are `root` inside and can install anything; no host privileges. Works on locked-down hosts
(it uses `proot`, fetched and checksum-verified automatically on x86-64). Debian is the default
(`RCONSOLE_DEFAULT_SYSTEM`).

| Feature | What you get |
|---|---|
| **`vm` / `vm status [name]`** | Your systems, which is active, disk used, install progress. |
| **`vm list`** | Every installable system (`debian:bookworm`, `ubuntu:24.04`, `alpine:3.21`, `kali`, aliases `deb ubu alp কালী`). |
| **`vm install [name…]`** | Installs one or more (`all` too) in the background; root filesystems come from the distribution's own mirrors and are verified against the **published SHA-256**; safe extraction; the same system can't be installed twice. |
| **`vm shell [name]`** | Interactive root shell in a VM. |
| **`vm run [--vm name] <cmd>`** | One command, printed output (90 s limit). `@vm:<name> cmd` does the same inline. |
| **`vm use <n>` / `vm mode [host\|vm [name]]`** | Which VM plain commands use / where they run (`host` is admins only). Several VMs and no default pops up a picker. |
| **`vm reset [name] --yes`** | Recreates a VM's system files and **keeps its `/root`**. |
| **`vm remove <name\|all> --yes`** | Deletes a VM and its `/root`. |
| **`vm setup <profile> [name]`** | Ready-made toolchains installed as a background job with live progress: `python` (with headers for psycopg2, mysqlclient, Pillow, lxml, cryptography…), `build`, `node`, `rust`, `db`, `tools`, `docker`, `udocker`, `kali` (Kali toolset), `all`. Profiles are remembered and re-applied after a restore. |
| **`vm doctor [name]`** | Step-by-step health check: shell, programs, root, write, DNS, internet, packages. |
| **`vm help`** | Short usage. |
| **No questions asked** | Installs are non-interactive (`DEBIAN_FRONTEND=noninteractive`); `pip install` works without `--break-system-packages`; Kali gets a writable `/dev/net` and no banner; user tool directories (`~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, `~/go/bin`) are on `PATH`. |
| **`docker`** | `docker run`/`pull`/`images`/`ps`/`rm`/`rmi`/… **without a daemon** (a shim over udocker, installed in the image and by `vm setup docker`). `drun` is `docker run`. `DOCKER_HOST` forwards to a real engine. Daemon-only features (`-d`, `exec`, `logs`, `build`, `compose`) explain why they are impossible. |
| **`neofetch` / `fetch` / `rfetch`** | Screen-fitting system summary: your target's (a VM's own distro and packages) or rconsole's; `rfetch` is always rconsole's. |
| **Not a hard sandbox** | The VM is a convenience boundary, not a security one (see the [security model](README.md#security-model)). |

---

## 5. Hosting apps, bots and workers

| Feature | What you get |
|---|---|
| **`expose <port> <name>`** | Publishes a server you started (host or any VM) at `https://<domain>/<name>/`. Private by default (rconsole login), `--public` for admins (webhooks, demos), `--rewrite` fixes root-relative links in HTML/CSS. Up to 20 per user; names unique across users. |
| **`expose list` / `expose rm` / `unexpose`** | List with `[up]`/`[down]`, remove one, several, or `all`. |
| **Reverse proxy** | Pooled; sends `X-Forwarded-Prefix/Host/Proto`, rewrites redirects into the prefix, passes multiple `Set-Cookie` through, never forwards rconsole's own session cookie; requests are buffered with a 30 s timeout (no WebSockets through it). |
| **`service`** | Named commands rconsole **keeps running**: start at boot, restart on crash with 2 s → 60 s back-off (reset after a minute of stability), live in a VM or on the host, survive redeploys (included in backups). `service add\|list\|start\|stop\|restart\|logs\|rm\|help`. |
| **`service add` options** | `--vm`, `--host` (admins), `--cwd`, `--env KEY=VALUE` (repeatable; good for bot tokens), `--expose PORT:NAME` (also publishes it), `--no-autostart`. Up to 10 per user. |
| **`kprocess`** | 🔒 Kill a process by PID, or everything the session hosts. Refuses to kill rconsole itself. |
| **Everything together** | `service add app --vm debian --cwd /root/app --expose 8000:app gunicorn -b 127.0.0.1:8000 app:app` starts it, restarts it, logs it and publishes it. |

---

## 6. Free-tier survival

Made for hosts that **sleep when idle and wipe the disk on every deploy** — see [FREE_HOSTING.md](FREE_HOSTING.md).

| Feature | What you get |
|---|---|
| **`keepalive`** | Pings the site's own public URL (`/healthz`) every 10 minutes so a free instance never sleeps. On by default on Render; `keepalive now`, `sudo keepalive off\|on`; also pings extra URLs (`RCONSOLE_KEEPALIVE_EXTRA`). |
| **`backup`** | 🔒 Encrypted snapshots to a **private git repo** (history never accumulates: each push replaces the branch) or a folder. Saves users and secrets, per-user state, the workspace, exposures, services, each VM's `/root`, the VM list and `vm setup` profiles, and the installed release of `rconsole update`. `sudo backup`, `now`, `restore --yes`, `help`. Runs every 15 minutes if something changed and once at shutdown. |
| **Automatic restore** | On a blank instance the latest backup is restored **before anything else starts**; VMs are reinstalled and profiles re-applied in the background; services start again. |
| **`reboot`** | 🔒 Restarts in place without a redeploy (`sudo reboot --yes`: final backup → stop everything → reload), or `--hard` to exit the process. Services start again; the page reconnects. |
| **`/healthz`** | Public health probe: `{"ok": true, "version": …}`. Answers even while an update is running. |
| **Sleep-proof deploys** | Render blueprints for the free plan (`render.yaml`) and for a persistent disk (`render.paid.yaml`). |

---

## 7. Updating (`rconsole update`)

New in 1.3.0. Details: [COMMANDS.md → Updating rconsole](COMMANDS.md#updating-rconsole).

| Feature | What you get |
|---|---|
| **`rconsole update`** | Asks GitHub for the latest release; if it is newer, shows what is new, asks, then updates the **whole program** to it. Admins only (no `sudo` prefix). |
| **`rconsole update --check`** | Reports only (everyone). `--yes` skips the question; `--force` also allows a git checkout or reinstalling the same / a rolled-back version. |
| **Console lock** | From the first step until the restart nothing else can be done: every request except `/healthz`, static files and the status endpoint gets `503`; no command, terminal, file operation, login or hosted app works, in any tab. Open tabs show a progress screen. |
| **Verified** | SHA-256 of the archive checked against the release's `SHA256SUMS` (no checksum, no update); strict unpack (no links, `..`, absolute paths, size limits). |
| **Tested before applied** | Correct version, every file compiles, and the new app is **imported in a separate process** with a throw-away data directory — before anything is stopped or replaced. |
| **Data stays intact** | Only program files are replaced, one by one, atomically, after a rollback copy of each is saved. `data/`, `.venv`, `.git`, `.env` are never touched; only files an earlier release installed are ever removed. Services, jobs and terminals are stopped and a final backup taken first; then an in-place restart. |
| **Dependencies** | Changed Python packages go to `<data>/update/site-<version>`, never into the system. |
| **`--rollback`** | Back to the previous version (also `python updater.py rollback`, which works with a dead server). |
| **Boot safety** | A new version must stay up ~25 s to count as healthy; one that keeps failing to start is rolled back at the next container start and is not offered again without `--force`. |
| **Survives re-creation** | The verified release is kept in `<data>/update/releases` and in the encrypted backups, and is re-applied at boot when the image was rebuilt from older code. |
| **`rconsole version`** | Version, install directory, last update result, rollback available. |
| **Settings** | `RCONSOLE_UPDATE_REPO` (your fork), `RCONSOLE_UPDATE_TOKEN` (private repo / rate limit; never passed to user commands). |

---

## 8. Background jobs

Add `&` to any command (`sleep 60 &`). Jobs are per user, survive closing the tab, and are numbered `%n`.

| Command | Does |
|---|---|
| `jobs` | Lists jobs (number, pid, running/done, command). |
| `jobout %n [lines]` | Last lines of a job's output (default 50). |
| `jobtail %n` | Live-follows output; any key stops. |
| `fg %n` | Follows output and stops when the job ends. |
| `kill %n` | Stops a job (`kill SIGKILL %n`); anything else goes to the real `kill`. |

Output is a rolling buffer; kills take the whole process tree.

---

## 9. Users and security

| Feature | What you get |
|---|---|
| **Accounts** | Admin and normal users; up to `RCONSOLE_MAX_USERS` (10). `suser`, `auser`, `nauser`, `muser`, `ruser`, `cpass`, `cuser` — all 🔒. The last admin can't be demoted or deleted; deleting a user removes their state, VMs, jobs, terminals and exposures; renaming moves everything with the name. |
| **First login** | The factory `ark`/`ark` login only works to set a new password (`sudo cpass`); until then only `cpass`, `logout`, `help`, `clear`, `history` and `rwhoami` run. `RCONSOLE_ADMIN_PASSWORD` sets the first password. |
| **Passwords** | PBKDF2-HMAC-SHA256, 210 000 rounds, per-user salt plus a persisted random pepper; old hashes upgrade on login; minimum length `RCONSOLE_MIN_PASSWORD` (8). |
| **Brute-force protection** | Throttled per (IP, user) and per IP with exponential lockout (`RCONSOLE_LOGIN_ATTEMPTS`/`..._LOCKOUT`); the response never reveals whether a user exists. |
| **Sessions** | Signed cookie `rconsole_session` (HttpOnly, SameSite=Lax, Secure over HTTPS), 30-day inactivity timeout, a deleted user is logged out. |
| **Web hardening** | Origin / `Sec-Fetch-Site` check on every POST and WebSocket handshake; `X-Frame-Options`, `nosniff`, HSTS over HTTPS; trusted-proxy handling (`RCONSOLE_TRUST_PROXY`). |
| **Authorization choke point** | `sandbox.py` decides host vs VM for the line console, jobs, hosted apps, the terminal WebSocket and the file APIs; host access is admin-only. |
| **Secrets** | Generated secrets are stored `0600`, shared by all processes, and stripped from every user process and hosted app. |
| **Users file** | Atomic `0600` writes; a corrupt file is quarantined, never silently reset. |
| **`rboot`** | 🔒 Factory reset: stops everything, wipes every user's state, VMs, logs and exposures, resets users to the default admin. No undo. |

---

## 10. Environment and AI

| Feature | What you get |
|---|---|
| **`env`** | 🔒 Per-user environment variables exported into every command and VM (values masked when listed): `sudo env`, `set K=V`, `unset K`, `import` (copy known provider keys from the server). |
| **`agent`** | 🔒 Native AI coding agent: runs shell commands and reads/writes/lists files in your current target; persistent per-tab conversation, context compaction, retries; OpenAI-compatible or Anthropic providers (first key found wins). Slash commands `/help /clear /model /provider /keys /models /compact /system /exit`; obviously destructive host commands are refused. |
| **`opencode`** | 🔒 Launches the OpenCode terminal AI; installs it as a background job on first use; `sudo opencode run "prompt"` for one prompt. |

---

## 11. Fun commands

Built in, nothing to install: `hello` / `hi`, `cowsay`, `fortune`, `lolcat`, `figlet` / `banner`, `sl`, `matrix`,
`colors`, `date`, `uptime`, `cal [month [year]]`, `echo [-n] [-e]`, `rev`, `wc`, plus `neofetch` / `fetch` /
`rfetch` ([section 4](#4-your-linux-systems-vm)). `echo`, `rev` and `wc` hand over to the real programs when the line has pipes.

---

## 12. Running real programs

Any word that is not a built-in is run by a real shell in your current target (60 s limit for captured output; use
`vm shell`, `&` or an interactive program for longer work), with your `sudo env` variables, user tool directories on
`PATH` and non-interactive package managers.

**Interactive programs** open in a real terminal window: `nano` `vi` `vim` `nvim` `emacs` `less` `more` `most` `man`
`top` `htop` `btm` `glances` `watch` `tmux` `screen` `mc` `ranger` `nnn` `fzf` `lynx` `w3m` `mutt` `irssi` `telnet`
`ssh` `bash` `sh` `zsh` `fish` `pwsh` `powershell` `cmd` `cmd.exe` `python` `python3` `py` `ipython` `pip` `irb`
`node` `npm` `npx` `php` `lua` `opencode`.

---

## 13. Deployment and releases

| Feature | What you get |
|---|---|
| **Docker image** | Runs as an unprivileged user by default (`RCONSOLE_RUN_AS_ROOT=1` for single-owner setups), data in `/data`, gunicorn with one worker and `RCONSOLE_THREADS` threads, health check on `/healthz`; the entrypoint also re-applies a saved update (`updater.py sync`). |
| **Render** | `render.yaml` (free plan, keep-alive + backups) and `render.paid.yaml` (persistent disk); detection of the public URL and trusted proxy. |
| **VPS** | `docker compose` with Caddy (automatic HTTPS) or nginx (`deploy/nginx.conf`), systemd notes — see [DEPLOY.md](DEPLOY.md). |
| **Local** | `uv run python main.py` (Python ≥ 3.14; the tests also pass on 3.12); Windows supported (ConPTY, no VM). |
| **CI and releases** | GitHub Actions runs lint and the test-suite; pushing a `vX.Y.Z` tag publishes a release with `rconsole-X.Y.Z.tar.gz`, a `.zip` and `SHA256SUMS` from the changelog — the archive `rconsole update` installs. Dependabot keeps dependencies current. |
| **Tests** | ~340 tests, including HTTP-level, authorization, path confinement, backups, the updater and documentation drift guards; real-VM end-to-end tests run when `proot` and a static `busybox` exist. |

---

## 14. Settings

All through environment variables (`config.py`); the full table with defaults is in
[README → Configuration](README.md#configuration).

| Area | Variables |
|---|---|
| Server | `PORT`, `RCONSOLE_DATA_DIR`, `RCONSOLE_THREADS`, `RCONSOLE_TRUST_PROXY`, `RCONSOLE_ALLOWED_ORIGINS`, `RCONSOLE_COOKIE_SECURE`, `RCONSOLE_PUBLIC_URL` |
| Secrets and login | `RCONSOLE_SECRET`, `RCONSOLE_PEPPER`, `RCONSOLE_ADMIN_PASSWORD`, `RCONSOLE_MIN_PASSWORD`, `RCONSOLE_LOGIN_ATTEMPTS`, `RCONSOLE_LOGIN_LOCKOUT`, `RCONSOLE_MAX_USERS` |
| Systems | `RCONSOLE_VM`, `RCONSOLE_DEFAULT_MODE`, `RCONSOLE_DEFAULT_SYSTEM`, `RCONSOLE_ALLOW_USER_HOST`, `RCONSOLE_USER_VM_PROC`, `RCONSOLE_RUN_AS_ROOT`, `RCONSOLE_VM_ROOTFS_URL`, `RCONSOLE_VM_ROOTFS_SHA256`, `RCONSOLE_VM_PROOT` |
| Services | `RCONSOLE_SUPERVISOR` |
| Files | `RCONSOLE_MAX_UPLOAD_MB` |
| Staying alive | `RCONSOLE_KEEPALIVE`, `RCONSOLE_KEEPALIVE_INTERVAL`, `RCONSOLE_KEEPALIVE_EXTRA` |
| Backups | `RCONSOLE_BACKUP_URL`, `RCONSOLE_BACKUP_TOKEN`, `RCONSOLE_BACKUP_DIR`, `RCONSOLE_BACKUP_PASSPHRASE`, `RCONSOLE_BACKUP_INTERVAL`, `RCONSOLE_BACKUP_MAX_MB`, `RCONSOLE_BACKUP_BRANCH` (branch that receives snapshots, default `rconsole-backup`), `RCONSOLE_BACKUP_USER` (git user for the token, default `x-access-token`), `RCONSOLE_BACKUP_ENCRYPT` (default on; turning it off stores plain snapshots), `RCONSOLE_RESTORE_SETUP` (re-run `vm setup` profiles after a restore, default on) |
| Updating | `RCONSOLE_UPDATE_REPO`, `RCONSOLE_UPDATE_TOKEN`, `RCONSOLE_UPDATE_API` (GitHub API base; tests / Enterprise mirrors only) |
| Docker shim | `DRUN_MODE`, `DRUN_PULL_OPTS`, `DOCKER_SHIM_LOCAL`, `DOCKER_HOST` |

---

## 15. Limits

| Thing | Limit |
|---|---|
| Accounts | 10 (`RCONSOLE_MAX_USERS`) |
| Services per user | 10 |
| Exposures per user | 20 (ports 1024–65535) |
| Captured command output | 60 s; `vm run` 90 s |
| Proxy request | 30 s, buffered, no WebSockets |
| Upload per file | 64 MB (`RCONSOLE_MAX_UPLOAD_MB`) |
| Editor file | 5 MB |
| Backup | 40 MB total, 20 MB per file (`RCONSOLE_BACKUP_MAX_MB`) |
| Terminals | about one per thread (`RCONSOLE_THREADS`, 64) |
| One worker | State lives in process memory: do not run several workers. |
