# Rconsole features

Every feature of Rconsole 1.3.11, by area. This is the **map**; the details live in
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
| **Tabs** | Each Rconsole tab has its own directory, history and terminal. A terminal keeps running while you switch tabs, refresh the browser or close the browser tab, and re-attaches when you return; closing the *Rconsole* tab (✕) kills its terminal. |
| **Real terminal** | Interactive programs open in an xterm.js window with a real PTY (ConPTY on Windows): job control, Ctrl+C, resize, colours, a ✕ button (or Esc) to close; the console carries on in the directory you were in. See [section 12](#12-running-real-programs). |
| **What survives what** | Browser reload or logout: tabs, history, aliases **and** the running terminal programs. Server restart, `sudo reboot`, `rconsole update` or a redeploy: tabs, history, aliases and each tab's folder come back, but the programs that ran in terminals, jobs and hand-started apps are **stopped** (a reopened tab gets a fresh terminal). See [Updating](docs/upgrading.md). |
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
| **Editor: smart editing** | Auto-indent (tabs or spaces, detected per file), auto-closing brackets/quotes with step-over and pair-delete, selection wrapping, HTML/XML auto-close tags, Markdown list continuation, Tab/Shift+Tab block indent, toggle comment (~60 languages), move/copy/delete/join lines, smart Home, current-line highlight. Full list: [COMMANDS.md → edit](COMMANDS.md#edit). |
| **Editor: find & replace** | Ctrl/Cmd+F / H / F3, match case, whole word, regex with `$1`, match counter, Replace All, go to line (Ctrl/Cmd+G). |
| **Editor: font size & status bar** | **A− / A+** (9–32 px, remembered; 16 px minimum on iOS), line/column, selection size, line count, indentation style (tap to change). Warns before closing the page with unsaved changes. |
| **Editor: touch symbol bar** | Tab, outdent, undo/redo, Find, comment toggle and the symbols that are awkward on a phone keyboard (brackets, quotes, `;` `:` `=` `/` `\` and more) above the keyboard. |
| **`fm [path]`** | In-console file manager: `cd`, `up`, `open`, `cat`, `rm`, `mkdir`, `touch`, `mv`, `?`, `q`; the directory carries over when you quit. |
| **📁 file browser** | Visual navigator: browse, open in the editor, create, rename, delete; download and upload (see COMMANDS.md). |
| **File manager: selecting** | Click the blank space of a row to select it (the name still opens it); Ctrl/Cmd+click toggles, Shift+click selects a range, **drag a rectangle** to select many (Ctrl/Shift adds), double-click opens. On phones **long-press** a row, then drag to sweep over more rows; taps then toggle. **Select all** (a checkbox at the start of the selection bar, shown once something is selected; Ctrl/Cmd+A), Esc clears, Delete key deletes. |
| **File manager: compress** | Selected items become one archive in the same folder: **zip, tar, tar.gz, tar.bz2, tar.xz**, tar.zst (Python 3.14+), **7z** (needs `bsdtar`; admins can also use 7-Zip), RAR (admins with the `rar` tool installed), and one-file **gz, bz2, xz, zst**. Symlinks are stored as links, never followed; an existing archive is never overwritten (`name (2).zip`). |
| **File manager: extract** | zip (also jar/war/apk/whl), tar family, gz/bz2/xz/zst, and through `bsdtar` **7z, RAR, CAB, ISO, CPIO, DEB, RPM, LZH, XAR**. *Extract to a new folder* (uses the archive's own single top folder if it has one) or *Extract here* (never overwrites). |
| **File manager: safe extraction** | Every entry is created with `dir_fd` + `O_NOFOLLOW`: `..`, absolute and drive-letter names are refused, nothing is written through a symlink, devices/FIFOs/hard links are skipped, setuid bits dropped. Zip-bomb guards: expanded size (`RCONSOLE_MAX_EXTRACT_MB`), 200,000 entries, free disk space. Confined (VM) users only get `bsdtar` for external formats. |
| **File manager: background jobs** | Compress and extract run as a job with a progress bar and a **Cancel** button (temp files are cleaned up); one job per user at a time, and they never tie up a request. |
| **File manager: properties** | Shown only when exactly one item is selected: type, location, size (folders: files/folders counted without following links), size on disk, modified/accessed/changed, permissions, owner/group (names for the host admin), link target, MIME type, entries inside an archive. |
| **File manager: multi-delete** | Delete everything selected in one confirmation (folders with their contents; links are removed, never followed). |
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
| **`vm update [name]`** | Updates a VM to the latest version, safely (Alpine walks to the **newest release**, one release at a time; Debian/Ubuntu/Kali get all newer packages of their release; `--stay` = packages only): **locks** it (nothing can start in it, what ran there stops), checks the package database, takes a **snapshot** (hard links: almost no disk), upgrades without questions (config files kept), **verifies**, and on *any* problem — failure, timeout, shutdown, even a crash — **restores the snapshot**. `/root` is never touched. Admins must be in the host shell; a normal user with one system has everything stop while it runs (name optional), with several only that VM is locked. `--yes` confirms, `--log` shows the run, `vm status` shows progress. |
| **`vm ps [name]`** · **`vm kill <pid> [name]`** | Processes of one VM. Each VM has its **own PID namespace** (`RCONSOLE_VM_PIDNS`, default `auto`), so `ps`/`top`/`kill`/`/proc` inside it show only its processes (PIDs from 1) and `kill -9 -1` stays inside; where the host forbids user namespaces these two commands are the scoped view (found through proot) and `vm doctor` says "NOT isolated". |
| **`vm deps [name]`** | What a restore will reinstall in a VM: system packages, pip, global npm, virtualenvs and Node projects under `/root` (`deps.json`, taken before each backup). `vm deps capture` records now, `vm deps restore` reinstalls now. `RCONSOLE_RESTORE_DEPS=0` disables restoring. |
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
| **`service`** | Named commands rconsole **keeps running**: start at boot, restart on crash with 2 s → 60 s back-off (reset after a minute of stability), live in a VM or on the host, are restored after a redeploy (definitions are in the backups; processes are stopped and started again once the VM is rebuilt). "Boot" means Rconsole's own startup. `service add\|list\|start\|stop\|restart\|logs\|rm\|help`. |
| **`service add` options** | `--vm`, `--host` (admins), `--cwd`, `--env KEY=VALUE` (repeatable; good for bot tokens), `--expose PORT:NAME` (also publishes it), `--no-autostart`. Up to 10 per user. |
| **`kprocess`** | 🔒 Kill a process by PID, or everything the session hosts. Refuses to kill rconsole itself. |
| **Everything together** | `service add app --vm debian --cwd /root/app --expose 8000:app gunicorn -b 127.0.0.1:8000 app:app` starts it, restarts it, logs it and publishes it. |

---

## 6. Free-tier survival

Made for hosts that **sleep when idle and wipe the disk on every deploy** — see [FREE_HOSTING.md](FREE_HOSTING.md).

| Feature | What you get |
|---|---|
| **`keepalive`** | Pings the site's own public URL (`/healthz`) every 10 minutes so a free instance is not suspended for inactivity (it cannot prevent provider restarts or quota limits). On by default on Render; `keepalive now`, `sudo keepalive off\|on`; also pings extra URLs (`RCONSOLE_KEEPALIVE_EXTRA`). |
| **`backup`** | 🔒 Encrypted snapshots to a **private git repo** (history never accumulates: each push replaces the branch) or a folder. Saves users and secrets, per-user state, the workspace, exposures, services, each VM's `/root`, the VM list and `vm setup` profiles, and the installed release of `rconsole update`. `sudo backup`, `now`, `restore --yes`, `help`. Runs every 15 minutes if something changed and once at shutdown. |
| **Automatic restore** | On a blank instance the latest backup is restored **before anything else starts**; VMs are reinstalled, profiles re-applied and **packages reinstalled** (system, pip, npm, virtualenvs, Node projects) in the background; each VM stays locked (`restoring`) until done, then its services start by themselves. |
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
| **Local** | `uv run python serve.py` (Python ≥ 3.14, older versions are not supported); Windows supported (ConPTY, no VM). |
| **`serve.py` launcher** | `uv run python serve.py` runs gunicorn where it works (Linux, macOS, containers) and **falls back to a built-in threaded server where it does not (Windows)**, with the same shutdown work. `RCONSOLE_SERVER=gunicorn\|builtin` forces one; `RCONSOLE_HOST` sets the built-in server's bind address. gunicorn is not installed on Windows. |
| **CI and releases** | GitHub Actions runs lint and the test-suite; pushing a `vX.Y.Z` tag publishes a release with `rconsole-X.Y.Z.tar.gz`, a `.zip` and `SHA256SUMS` from the changelog — the archive `rconsole update` installs. Dependabot keeps dependencies current. |
| **Tests** | about 500 tests, including HTTP-level, authorization, path confinement, backups, the updater and documentation drift guards; real-VM end-to-end tests run when `proot` and a static `busybox` exist. |

---

## 14. Settings

All through environment variables (`config.py`); the full table with defaults is in
[docs/configuration.md](docs/configuration.md) (generated from the same list as this table).

<!-- BEGIN GENERATED: env-names -->

| Area | Variables |
|---|---|
| Server | `PORT`, `RCONSOLE_DATA_DIR`, `RCONSOLE_THREADS`, `RCONSOLE_LOG_LEVEL`, `RCONSOLE_SERVER`, `RCONSOLE_HOST`, `RCONSOLE_TRUST_PROXY`, `RCONSOLE_COOKIE_SECURE`, `RCONSOLE_ALLOWED_ORIGINS`, `RCONSOLE_PUBLIC_URL`, `RCONSOLE_RUN_AS_ROOT` |
| Accounts and login | `RCONSOLE_SECRET`, `RCONSOLE_PEPPER`, `RCONSOLE_ADMIN_PASSWORD`, `RCONSOLE_MIN_PASSWORD`, `RCONSOLE_MAX_USERS`, `RCONSOLE_LOGIN_ATTEMPTS`, `RCONSOLE_LOGIN_LOCKOUT` |
| Linux systems (VMs) | `RCONSOLE_VM`, `RCONSOLE_DEFAULT_MODE`, `RCONSOLE_DEFAULT_SYSTEM`, `RCONSOLE_ALLOW_USER_HOST`, `RCONSOLE_USER_VM_PROC`, `RCONSOLE_VM_PIDNS`, `RCONSOLE_HOST_HOME`, `RCONSOLE_VM_ROOTFS_URL`, `RCONSOLE_VM_ROOTFS_SHA256`, `RCONSOLE_VM_PROOT` |
| Services and hosted apps | `RCONSOLE_SUPERVISOR` |
| Files | `RCONSOLE_MAX_UPLOAD_MB`, `RCONSOLE_MAX_EXTRACT_MB` |
| Staying awake | `RCONSOLE_KEEPALIVE`, `RCONSOLE_KEEPALIVE_INTERVAL`, `RCONSOLE_KEEPALIVE_EXTRA` |
| Backups and restore | `RCONSOLE_BACKUP_URL`, `RCONSOLE_BACKUP_TOKEN`, `RCONSOLE_BACKUP_USER`, `RCONSOLE_BACKUP_BRANCH`, `RCONSOLE_BACKUP_DIR`, `RCONSOLE_BACKUP_PASSPHRASE`, `RCONSOLE_BACKUP_ENCRYPT`, `RCONSOLE_BACKUP_INTERVAL`, `RCONSOLE_BACKUP_MAX_MB`, `RCONSOLE_RESTORE_SETUP`, `RCONSOLE_RESTORE_DEPS` |
| Updating | `RCONSOLE_UPDATE_REPO`, `RCONSOLE_UPDATE_TOKEN`, `RCONSOLE_UPDATE_API` |
| Docker Compose files | `RCONSOLE_DOMAIN`, `RCONSOLE_VERSION` |
| Docker compatibility layer (inside VMs) | `DRUN_MODE`, `DRUN_PULL_OPTS`, `DOCKER_SHIM_LOCAL`, `DOCKER_HOST` |

<!-- END GENERATED: env-names -->

---|---|
| Server | `PORT`, `RCONSOLE_DATA_DIR`, `RCONSOLE_THREADS`, `RCONSOLE_TRUST_PROXY`, `RCONSOLE_ALLOWED_ORIGINS`, `RCONSOLE_COOKIE_SECURE`, `RCONSOLE_PUBLIC_URL` |
| Secrets and login | `RCONSOLE_SECRET`, `RCONSOLE_PEPPER`, `RCONSOLE_ADMIN_PASSWORD`, `RCONSOLE_MIN_PASSWORD`, `RCONSOLE_LOGIN_ATTEMPTS`, `RCONSOLE_LOGIN_LOCKOUT`, `RCONSOLE_MAX_USERS` |
| Systems | `RCONSOLE_VM`, `RCONSOLE_DEFAULT_MODE`, `RCONSOLE_DEFAULT_SYSTEM`, `RCONSOLE_ALLOW_USER_HOST`, `RCONSOLE_USER_VM_PROC`, `RCONSOLE_RUN_AS_ROOT`, `RCONSOLE_HOST_HOME`, `RCONSOLE_VM_ROOTFS_URL`, `RCONSOLE_VM_ROOTFS_SHA256`, `RCONSOLE_VM_PROOT` |
| Services | `RCONSOLE_SUPERVISOR` |
| Files | `RCONSOLE_MAX_UPLOAD_MB`, `RCONSOLE_MAX_EXTRACT_MB` |
| Staying alive | `RCONSOLE_KEEPALIVE`, `RCONSOLE_KEEPALIVE_INTERVAL`, `RCONSOLE_KEEPALIVE_EXTRA` |
| VM isolation / restore | `RCONSOLE_VM_PIDNS` (`auto` \| `on` \| `off`: one PID namespace per VM), `RCONSOLE_RESTORE_DEPS` (reinstall each VM's packages after a restore, default on) |
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
| Extracted size per archive | 2048 MB (`RCONSOLE_MAX_EXTRACT_MB`); 200,000 entries |
| Items per compress | 500; one compress / extract job per user at a time |
| Editor file | 5 MB |
| Backup | 40 MB total, 20 MB per file (`RCONSOLE_BACKUP_MAX_MB`) |
| Terminals | about one per thread (`RCONSOLE_THREADS`, 64) |
| One worker | State lives in process memory: do not run several workers. |
