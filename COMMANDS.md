# rconsole command reference

Every command rconsole understands, with usage, options and examples. If a command exists in the code it is
documented here (a test fails otherwise).

**Contents**
[How commands work](#how-commands-work) ·
[Quick index](#quick-index) ·
[Basics](#basics) ·
[Files and editing](#files-and-editing) ·
[Your Linux systems (`vm`)](#your-linux-systems-vm) ·
[Hosting and exposing apps](#hosting-and-exposing-apps) ·
[Services: bots and workers](#services-bots-and-workers) ·
[Staying alive: keep-alive, backups, reboot](#staying-alive-keep-alive-backups-reboot) ·
[Background jobs](#background-jobs) ·
[Users and administration](#users-and-administration) ·
[Environment and AI](#environment-and-ai) ·
[Fun commands](#fun-commands-eggshells) ·
[Interactive programs](#interactive-programs) ·
[Running anything else](#running-anything-else-real-shell)

---

## How commands work

Type a command and press **Enter**. rconsole looks at the first word:

1. **Console commands** (this document) are built in and run instantly. They are the same on every system.
2. **Interactive programs** (`nano`, `vim`, `python`, `bash`, `ssh`, …) open in a real terminal window.
   Close it with the **✕ button** (top-right) or **Esc**; either ends that session.
3. **Everything else** (`ls`, `git`, `pip`, `apt`, `curl`, `python main.py`, …) runs in a real shell on your
   current **target**: the server's **host** (admins only) or one of your **VMs**.

### Targets: host or VM

* Admins start on the **host**; normal users work in their **VMs** and never see the host.
* The prompt shows where you are: `ark@rconsole:/srv$` on the host, `ark@debian:/root$` in a VM.
* Switch with the **system dropdown** (tab bar on desktop, ⚙ menu on phones), `vm use <name>`, or `vm mode`.
* With several VMs and none chosen, a command that needs one shows a **picker** in the output — choose a system
  (or install another) and the command runs.
* Force a target for one command by prefixing it:

| Prefix | Runs in |
|---|---|
| `@host cmd` | the server host (admins only) |
| `@vm cmd` | your active VM |
| `@vm:kali cmd` | the VM called `kali` |

### Shell features that work everywhere

* Pipes, redirects, `&&`, `||`, `;`, globbing and quoting work as in any shell:
  `ls -la | grep py > files.txt && wc -l files.txt`.
* **`sudo`** prefix: required for admin commands (marked 🔒 below). A non-admin who types `sudo …` is refused.
  `sudo <anything else>` just runs it.
* **`&`** at the end runs a command as a [background job](#background-jobs).
* **`~`** means your home (`/root` in a VM). **Tab bar** `+` opens another tab; each tab has its own directory.
* Plain (non-interactive) commands are cut off after **60 s**; use `&`, `vm shell` or a job for long work.
* Aliases (see [`alias`](#alias)) expand before anything else.
* Inside a VM the commands you install in `~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, `~/go/bin` are always on
  `PATH`, and exports in `~/.profile` apply to plain commands too.

Legend: 🔒 = needs `sudo` (admin). `<x>` = required, `[x]` = optional.

---

## Quick index

| Command | What it does | |
|---|---|---|
| [`help`](#help) | list commands | |
| [`clear`](#clear--cls) / `cls` | clear the screen | |
| [`history`](#history) | commands typed in this tab | |
| [`rwhoami`](#rwhoami) | show your username | |
| [`version`](#version) | show the rconsole version | |
| [`cd`](#cd) | change directory | |
| [`alias`](#alias) / [`unalias`](#unalias) | command shortcuts | |
| [`theme`](#theme) | colour themes | |
| [`logout`](#logout) | sign out | |
| [`edit`](#edit) | built-in code editor | |
| [`fm`](#fm) | file manager | |
| [`vm`](#vm) | your Linux systems | |
| [`neofetch`](#neofetch--fetch--rfetch) / `fetch` / `rfetch` | system summary | |
| [`expose`](#expose) / [`unexpose`](#unexpose) | publish a running server at `/name/` | |
| [`kprocess`](#kprocess) | kill a process by PID | 🔒 |
| [`service`](#service) | supervised bots/workers: autostart, restart on crash | |
| [`keepalive`](#keepalive) | stay awake on free hosting (self-ping) | |
| [`backup`](#backup) | encrypted backups, auto-restore on a fresh instance | 🔒 |
| [`reboot`](#reboot) | restart rconsole without redeploying | 🔒 |
| [`jobs`](#jobs) · [`jobout`](#jobout) · [`jobtail`](#jobtail) · [`fg`](#fg) · [`kill`](#kill) | background jobs | |
| [`suser`](#suser) · [`auser`](#auser) · [`nauser`](#nauser) | list users / grant / revoke admin | 🔒 |
| [`muser`](#muser) · [`ruser`](#ruser) | create / delete a user | 🔒 |
| [`cpass`](#cpass) · [`cuser`](#cuser) | change a password / your username | 🔒 |
| [`rboot`](#rboot) | factory reset | 🔒 |
| [`env`](#env) | per-user environment variables | 🔒 |
| [`agent`](#agent) | AI coding agent | 🔒 |
| [`opencode`](#opencode) | OpenCode terminal AI | 🔒 |
| [`hello`](#hello--hi) / `hi` · [`cowsay`](#cowsay) · [`fortune`](#fortune) · [`lolcat`](#lolcat) · [`figlet`](#figlet--banner) / `banner` · [`sl`](#sl) · [`matrix`](#matrix) · [`colors`](#colors) | eggshells | |
| [`date`](#date) · [`uptime`](#uptime) · [`cal`](#cal) · [`echo`](#echo) · [`rev`](#rev) · [`wc`](#wc) | small utilities | |

---

## Basics

### `help`
Prints the list of console commands, shell features and admin commands.
```text
help
```

### `clear` / `cls`
Clears the output of the current tab. (History and state are kept.)

### `history`
Lists the commands you typed in **this tab**, numbered, newest last (the last 500 are kept).
```text
history
```

### `rwhoami`
Prints your username.

### `version`
Prints the rconsole version, e.g. `rconsole 1.1.0` (also in the first line of `help` and at `/healthz`).

### `cd`
Changes the tab's working directory. Each tab remembers its own directory. On the host new tabs start in the
**workspace** (`<data>/workspace`), which is part of every backup.
```text
cd /srv/site     # absolute
cd ..            # up
cd ~             # home (/root in a VM)
cd               # same as cd ~
```
On the host admins can go anywhere. In a VM you are confined to that VM's filesystem — `..` and symlinks can't
escape it. Errors: `No such file or directory`, `Not a directory`.

### `alias`
Defines persistent shortcuts (saved for your account).
```text
alias                    # list all aliases
alias ll=ls -la          # define (quotes optional)
alias ll                 # show one
```
An alias replaces the **first word** of what you type: after `alias ll=ls -la`, typing `ll /tmp` runs `ls -la /tmp`.
Names can't contain spaces.

### `unalias`
Removes an alias: `unalias ll`.

### `theme`
Lists or switches the colour theme (saved for your account and applied live).
```text
theme            # list themes, * marks the current one
theme dracula    # switch
```
Themes: `default` `light` `amber` `solarized` `dracula` `github` `ubuntu` `retro` `nord` `gruvbox` `matrix`
`tokyonight` `catppuccin` `monokai` `everforest` `ayu` `papercolor`.
You can also use the **theme** button (tab bar on desktop, ⚙ settings menu on phones).

### `logout`
Signs you out. Your tabs, history, aliases and running terminals are kept; log back in to resume.

---

## Files and editing

### `edit`
Opens the **built-in code editor** (syntax highlighting for ~65 languages, great on phones).
```text
edit main.py            # opens (creates it when you save)
edit /etc/hosts
```
Works on the host (admins) or inside the VM you are in. Files over 5 MB are refused.

### The 📁 file browser: download and upload
The 📁 button (tab bar) opens a visual file browser. Besides browsing, creating, renaming, deleting and editing:
* **⬇ Download** on every row: a file downloads as it is; a **folder downloads as a `.zip`** that is built while it
  streams (symlinks inside it are skipped, so a link can never pull in a file from outside the folder).
* **⬆ Upload files** adds one or many files to the folder you are looking at. **⬆ Upload folder** uploads a whole
  folder and keeps its structure (empty sub-folders are not sent: a browser only reports files).
* Files that already exist are not replaced until you confirm once for the whole batch. Each file is its own request,
  so one failure does not stop the rest; the status line shows progress and what failed.
* The per-file limit is **64 MB** by default (`RCONSOLE_MAX_UPLOAD_MB`). For bigger things use `git clone`, `curl`
  or `wget` inside a VM, or upload a `.zip` and `unzip` it.
* Folder upload needs a browser that supports folder pickers (desktop Chrome, Edge, Firefox, Safari; on phones it
  varies, in which case upload a `.zip` and unzip it).
* Everything goes through the same confinement as the rest of the console: in a VM you only see that VM.

### `fm`
An in-console **file manager** (also available as the 📁 button).
```text
fm              # current directory
fm /srv/app     # start somewhere else
```
Commands inside `fm`:

| Command | Does |
|---|---|
| `cd <dir>` | enter a directory |
| `up` | go up one level |
| `open <file>` | edit a file in the editor |
| `cat <file>` | print a file |
| `rm <file>` | delete a file or empty directory |
| `mkdir <dir>` / `touch <file>` | create |
| `mv <a> <b>` | rename or move |
| `?` · `q` | help · quit (your directory carries over) |

---

## Your Linux systems (`vm`)

You can have **several** systems at once — Alpine, Debian, Ubuntu, Kali — each with its **own root filesystem and
its own `/root`**. Inside one you are `root` and can install anything. Debian is the default.
`vm` works for everyone and only ever touches **your** systems.

### `vm`
| Subcommand | What it does |
|---|---|
| `vm` / `vm status [name]` | your systems, which is active, disk used |
| `vm list` | every system you can install (and which you have) |
| `vm install [name…]` | install one or more: `debian ubuntu:24.04 kali alpine`, or `all`. No name = Debian |
| `vm shell [name]` | interactive root shell in a VM (asks which, if several) |
| `vm run [--vm name] <cmd>` | run one command in a VM and print the output (90 s limit) |
| `vm use <name>` | make a VM the default for plain commands |
| `vm mode [host\|vm [name]]` | show / choose where plain commands run |
| `vm reset [name] --yes` | recreate a VM's system files, **keep** its `/root` |
| `vm remove <name\|all> --yes` | delete a VM **and** its `/root` |
| `vm setup <profile> [name]` | install Python / build tools / Node … (below) |
| `vm doctor [name]` | step-by-step health check (shell, programs, root, write, DNS, internet, packages) |
| `vm help` | short usage |

```text
vm list
vm install debian kali          # two systems, one after the other
vm shell kali                   # root shell in Kali
vm use debian                   # plain commands now run in Debian
vm run --vm ubuntu apt update
@vm:kali nmap -V                # one-off command in Kali
```
* Releases: `debian:bookworm`, `ubuntu:24.04`, `alpine:3.21`. Names also accept `deb`, `ubu`, `alp`, `কালী`.
* Installs run in the background; `vm status` shows progress. You can't install the same system twice (use
  `vm remove` first).
* `vm mode host` is admins only. `vm mode vm` with several systems asks which one.
* Installs never stop to ask questions (`DEBIAN_FRONTEND=noninteractive`), and `pip install` works without
  `--break-system-packages`.

#### `vm setup`
Installs a ready-made toolchain **inside a VM** as a background job with live progress:

| Profile | Installs |
|---|---|
| `python` | Python 3, pip, venv, compiler **and headers** for psycopg2, mysqlclient, Pillow, lxml, cryptography … |
| `build` | gcc, g++, make, cmake, autotools, pkg-config |
| `node` | Node.js, npm |
| `rust` | Rust, cargo |
| `db` | psql, mysql, redis-cli, sqlite3 |
| `tools` | nano, vim, wget, curl, git, ps, htop, zip, jq, ssh … |
| `docker` | Docker CLI, compose, buildx. **Client only:** a VM runs under proot, which has no namespaces, cgroups or netfilter, so `dockerd` cannot start inside it. Point it at a remote engine with `DOCKER_HOST` |
| `kali` | **Kali VMs only.** The command-line Kali toolset (`kali-linux-headless`, no desktop) plus the basics it needs. Several GB and a long install; refused on any other system. Want more: `apt install kali-linux-large` |
| `all` | `tools`, `build`, `python`, `node`, `rust`, `db` (not `docker` or `kali`) |

```text
vm setup                  # list profiles
vm setup python           # then: pip install psycopg2 mysqlclient Pillow
vm setup python kali      # a specific VM
vm setup docker           # Docker client + compose
vm setup kali             # in a Kali VM: the Kali toolset (`vm install kali` first)
```

### `neofetch` / `fetch` / `rfetch`
System summary that **fits your screen** (logo beside, above, or hidden on phones).
* `neofetch` and `fetch` follow your target: in a VM they show **that VM's** system (distro, packages, shell);
  on the host they show rconsole's summary. Typing `neofetch` inside `vm shell` works too.
* `rfetch` always shows rconsole's own summary.
* Flags: `--no-logo`; in a VM also `--plain` (no colours).

---

## Hosting and exposing apps

Two ways to put something on the web through rconsole's domain, and they work together. Both sit behind
rconsole's login unless stated. (`sudo serve` and `sudo gunicorn` were removed in 1.2.1: these two cover them.)

| | `expose` | `service` |
|---|---|---|
| What it does | publishes a server that is **already running** at `/<n>/` | **keeps it running** (autostart, restart on crash, logs) |
| Who starts the program | **you** (`python main.py &`, `npm run dev`, …) | rconsole's supervisor |
| Combine | | `service add … --expose PORT:NAME` does both in one line |

```text
# a static site
service add site --vm debian --cwd /root/site --expose 8000:site python3 -m http.server 8000
# a Python web app (install it once: vm setup python, then pip install gunicorn)
service add app --vm debian --cwd /root/app --expose 8000:app gunicorn -b 127.0.0.1:8000 app:app
service logs app                  # what it printed (replaces `sprocess`)
```

### `expose`
Publishes a server you already started — on the host or in any VM — at `https://<your-domain>/<name>/`.
```text
python main.py &                  # a web server on port 8000
expose 8000 myapp                 # -> https://your-domain/myapp/
expose 8000 myapp --rewrite       # also fix /static/… links inside HTML/CSS
expose 8000 hook --public         # no login needed (admins only)
expose list                       # your exposures (admins see all)
expose rm myapp                   # stop exposing
```
* Names: lowercase letters, digits, `-` (1–32), not only digits, not a reserved word (`login`, `api`, `static`,
  `ws`, `healthz`, `console`, …). Names are unique across all users; the owner or an admin can remove one.
* Ports 1024–65535, never the console's own port. Up to 20 per user.
* You can expose **before** starting the app; `expose list` shows `[up]`/`[down]`.
* The app is served under the prefix `/<name>`. rconsole sends `X-Forwarded-Prefix`, `X-Forwarded-Host` and
  `X-Forwarded-Proto`, and rewrites redirects (`Location`) back into the prefix. Frameworks that support a prefix
  (Django `FORCE_SCRIPT_NAME`, Flask `ProxyFix(x_prefix=1)`, Vite `--base`) can serve natively.
  `--rewrite` is a best-effort fix for root-relative links in HTML and CSS (not JavaScript).
* `--public` (admins only) removes the login requirement for that name — handy for webhooks and demos.
* Not supported: WebSockets through the proxy. Requests are buffered (not streamed) and time out after 30 s.
* The console's own session cookie is never forwarded to your app.

### `unexpose`
Same as `expose rm`: `unexpose myapp` (several names allowed; `unexpose all` removes all of yours).

### `kprocess`
🔒 Kills a process: `sudo kprocess <pid>` for one, `sudo kprocess` for everything this session still hosts. It
refuses to kill the rconsole server itself. For anything you started with `service`, use `service stop <n>`.

---

## Services: bots and workers

### `service`
A **service** is a named command that rconsole keeps running: it **starts at boot**, is **restarted when it
crashes**, can live in a VM or on the host, and survives redeploys (definitions are part of every
[backup](#backup)). This is how you host a Telegram/Discord bot, a scraper, a queue worker or a web app.

| Subcommand | What it does |
|---|---|
| `service add <name> [options] <command…>` | create and start a service |
| `service list` | every service with state, uptime, restarts, where it runs |
| `service start`, `stop`, `restart` `<name\|all>` | control one service or all of yours |
| `service logs <name> [lines]` | recent output (and the last exit code) |
| `service rm <name>` | stop and delete (also removes its `--expose`) |
| `service help` | short usage |

Options for `add` (put them **before** the command; `--` ends the options):

| Option | Meaning |
|---|---|
| `--vm <name>` | run inside that VM (default: your current system) |
| `--host` | run on the server itself (**admins only**) |
| `--cwd <dir>` | working directory (default: `/root` in a VM, the workspace on the host) |
| `--env KEY=VALUE` | environment variable (repeatable) — good for bot tokens |
| `--expose PORT:NAME` | publish the app at `/NAME/` (like [`expose`](#expose)) |
| `--no-autostart` | create it stopped; start with `service start` |

```text
service add tgbot --vm debian --cwd /root/bot --env BOT_TOKEN=123:abc python3 bot.py
service add web --vm debian --cwd /root/app --expose 8000:web gunicorn -b 127.0.0.1:8000 app:app
service add job --vm debian sh -c 'while true; do python3 run.py; sleep 3600; done'
service list
service logs tgbot 100
```
* **Restart policy:** after a crash it is restarted after 2 s, then 4, 8, … up to 60 s between attempts; a run that
  lasted a minute resets the delay. `service list` shows `retry in …s` and the last exit code.
* **After a redeploy or restore** services start again; if their VM is still being rebuilt they simply retry until
  it is ready.
* The command text is kept exactly as you typed it (quotes, `;`, `&&`). Names: lowercase letters, digits, `-` `_`.
  Up to 10 per user. Normal users can only run services inside their VMs. Admins see everyone's services; everyone
  else only their own.

---

## Staying alive: keep-alive, backups, reboot

Made for free hosting tiers that sleep when idle and wipe their disk on every deploy — see
[FREE_HOSTING.md](FREE_HOSTING.md).

### `keepalive`
Pings the site's own **public URL** (`/healthz`) every 10 minutes so a free instance never goes to sleep.
```text
keepalive           # status: URL, interval, last result
keepalive now       # ping right now and show the result
keepalive off       # (admins) pause until the next restart;  keepalive on  resumes
```
On by default on Render (the URL is detected). Elsewhere set `RCONSOLE_PUBLIC_URL` and `RCONSOLE_KEEPALIVE=1`;
`RCONSOLE_KEEPALIVE_EXTRA=url1,url2` also pings your other free sites; `RCONSOLE_KEEPALIVE_INTERVAL` changes the
period (seconds, minimum 60).

### `backup`
🔒 Encrypted snapshots of everything that matters, pushed to a **private git repo** (or a folder), restored
**automatically** on a fresh instance.
```text
sudo backup                 # status: target, last backup, size, problems
sudo backup now             # take one right now
sudo backup restore --yes   # replace local data with the latest backup
sudo backup help            # setup steps
```
* **Saved:** users and secrets, per-user state, the workspace, exposures, services, each VM's `/root`, and the list
  of VMs and `vm setup` profiles. **Not saved:** VM system files (re-downloaded), caches, `node_modules`/venvs,
  logs, files over 20 MB, running processes.
* Runs every 15 min **if something changed**, and once at shutdown. Size limit: `RCONSOLE_BACKUP_MAX_MB` (default 40).
* **Encrypted** with `RCONSOLE_BACKUP_PASSPHRASE` (required). Destination: `RCONSOLE_BACKUP_URL` +
  `RCONSOLE_BACKUP_TOKEN` (git) or `RCONSOLE_BACKUP_DIR` (a folder).
* On a blank instance (no users file) the latest backup is restored **before anything else starts**, then your VMs
  are reinstalled in the background and `vm setup` profiles re-applied.
* Restoring at boot never overwrites existing files; `sudo backup restore --yes` does.

### `reboot`
🔒 Restarts rconsole **without redeploying** — so a free host doesn't hand you a blank disk.
```text
sudo reboot --yes         # in place: final backup → stop everything → reload. Files and VMs stay.
sudo reboot --hard --yes  # exit the process; the platform starts a fresh one (may need a restore)
```
Services start again by themselves and the page reconnects. Use it after changing environment variables on the
host, or to recover from a wedged state. Without `--yes` it only explains what it will do.

---

## Background jobs

Add `&` to any command to run it in the background (`sleep 60 &`). Jobs are per user and survive closing the tab.
Refer to a job as `%n` (its number).

### `jobs`
Lists your jobs: number, pid, running/done, command.

### `jobout`
`jobout %n [lines]` — last lines of a job's output (default 50).

### `jobtail`
`jobtail %n` — live-follow a job's output; press any key to stop.

### `fg`
`fg %n` — follow a job's output live and stop automatically when it ends.

### `kill`
```text
kill %2             # stop background job 2
kill SIGKILL %2     # send a signal to job 2
kill -9 1234        # anything else goes to the real shell's kill
```

---

## Users and administration

Admin commands need `sudo` (🔒). Passwords must be at least 8 characters (`RCONSOLE_MIN_PASSWORD`). Usernames are
1–32 characters: letters, digits, `.` `_` `-`, starting with a letter or digit.

### `suser`
🔒 Lists all users and their role (`admin`/`user`); marks you with `(you)`.

### `auser`
🔒 `sudo auser <username>` — grant admin.

### `nauser`
🔒 `sudo nauser <username>` — revoke admin (the last admin can't be demoted).

### `muser`
🔒 Creates a user. It asks for **username**, **password** and **confirmation**. New users are normal (non-admin).

### `ruser`
🔒 `sudo ruser <username>` — deletes a user together with their saved state, VMs, jobs, terminals and exposures
(the last admin can't be deleted).

### `cpass`
🔒 Changes a password. It asks **which user** (Enter = yourself), then the new password twice. If you still use the
factory password (`ark`/`ark`), this is the command you must run first — until you do, only `cpass`, `logout`,
`help`, `clear`, `history` and `rwhoami` work.

### `cuser`
🔒 Renames **you**. It asks for the new username. Your tabs, aliases, VMs and exposures move with the name.

### `rboot`
🔒 **Factory reset**: stops hosted apps, jobs and terminals, wipes every user's saved state, VMs, logs and
exposures, and resets the user list to the default admin. There is no undo.

---

## Environment and AI

### `env`
🔒 Per-user environment variables, exported into **every** command you run (and into your VMs). Values are masked
when listed. Typical use: API keys for the AI tools.
```text
sudo env                       # list (masked)
sudo env set OPENAI_API_KEY=sk-…
sudo env unset OPENAI_API_KEY
sudo env import                # copy known provider keys from the server's environment
```

### `agent`
🔒 A native AI coding agent that can run shell commands and read/write/list files in **your current target** (the
host for admins, otherwise the VM you are in). Needs a provider key (see `env`).
```text
sudo agent                     # interactive session (persists until /exit)
sudo agent "add tests for utils.py"
sudo agent models              # list the provider's models
sudo agent help
```
Inside the session: `/help` `/clear` `/model <name>` `/provider` `/keys` `/models` `/compact` `/system`
`/exit` (or `/quit`). On the host, obviously destructive shell commands are refused; inside a VM they are allowed.

### `opencode`
🔒 Launches the **OpenCode** terminal AI. If it isn't installed it starts `npm install opencode-ai` as a job and tells
you to run it again when done. `sudo opencode run "your prompt"` runs a single prompt.

---

## Fun commands (eggshells)

These are built in and need no installation.

### `hello` / `hi`
Greets you by name.

### `cowsay`
`cowsay <text>` — a cow says it.

### `fortune`
A random quote.

### `lolcat`
`lolcat [text]` — rainbow-coloured text (default: "rconsole is awesome").

### `figlet` / `banner`
`figlet <text>` — big ASCII-art lettering (`banner` is the same).

### `sl`
Steam locomotive (for when you typed `sl` instead of `ls`).

### `matrix`
A frame of green falling characters.

### `colors`
Prints the terminal colour palette.

### `date`
Current date and time.

### `uptime`
How long rconsole has been running.

### `cal`
```text
cal              # the whole year
cal 3            # March of this year
cal 3 2027       # March 2027
```

### `echo`
Prints its arguments. `echo -n text` omits the newline; `echo -e "a\tb\nc"` interprets `\n \t \r \a \e \xNN`.
When the line has a pipe or redirect (`echo hi | tr a-z A-Z`) the **real** `echo` runs in your shell instead.

### `rev`
`rev <text>` reverses text; `rev <file> [file…]` reverses each line of files. (With pipes the real `rev` runs.)

### `wc`
`wc <text>` or `wc <file> [file…]` — lines, words and characters, with a total for several files.
(With pipes the real `wc` runs.)

---

## Interactive programs

These open in a **real terminal window** (a ✕ button in the top-right closes it; Esc does too):

`nano` `vi` `vim` `nvim` `emacs` `less` `more` `most` `man` `top` `htop` `btm` `glances` `watch` `tmux` `screen`
`mc` `ranger` `nnn` `fzf` `lynx` `w3m` `mutt` `irssi` `telnet` `ssh` `bash` `sh` `zsh` `fish` `pwsh` `powershell`
`cmd` `cmd.exe` `python` `python3` `py` `ipython` `pip` `irb` `node` `npm` `npx` `php` `lua` `opencode`

Install what you need in a VM (`vm setup tools`, `apt install vim`), then just type the name.

---

## Running anything else (real shell)

Any other word is run by a real shell in your current target, so every program you install works:
`git`, `curl`, `pip`, `apt`/`apk`, `make`, `docker`-less builds, `python main.py`, …

* Output is captured and shown when the command finishes (60 s limit). For long or interactive work use
  `vm shell`, `&`, or an interactive program.
* To keep a server running and reachable: start it with `&` (or in `vm shell`), then use [`expose`](#expose).
* `vm run`, `@vm cmd` and plain commands in VM mode all go through the same environment: `PATH` includes your
  user tool directories, `DEBIAN_FRONTEND=noninteractive`, `PIP_BREAK_SYSTEM_PACKAGES=1`, and any variables you set
  with `sudo env`.
