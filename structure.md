# rconsole — file structure

```
rconsole/
├── app.py                  # Flask app: CSRF + auth guards, login throttling, confined file APIs, proxy, /ws
├── main.py                 # Entry point (calls app.main())
├── config.py               # All environment-driven settings + persisted secrets
├── LICENSE                 # MIT
├── _ptyexec.py             # PTY launcher: setsid + controlling terminal (job control, Ctrl+C), then exec
├── sandbox.py              # THE authorization choke point: host vs per-user VM (proot), path confinement
├── distros.py              # VM system catalog (alpine/debian/ubuntu/kali) + checksum-verified resolvers
├── interp.py               # Command parsing/dispatch; interactive detection; host passthrough
├── commands.py             # Thin registry (COMMANDS/PROTECTED/@command) + module wiring
├── cmd_basic.py            # help, clear, history, rwhoami, cd, alias, theme
├── cmd_hosting.py          # serve / gunicorn / sprocess / kprocess (+port parsing, launcher)
├── cmd_users.py            # logout, suser/auser/nauser/ruser, muser/cpass/cuser, rboot
├── cmd_vm.py               # vm: list / install <name> / shell / run / mode / reset / remove / status
├── cmd_fetch.py            # neofetch / fetch (target aware, width responsive) + rfetch
├── vmtools/
│   └── rfetch.sh           # POSIX shim installed in every VM as neofetch / fetch / rfetch
├── cmd_jobs.py             # jobs, jobout, jobtail, fg, kill
├── cmd_env.py              # sudo env (per-user environment variables)
├── cmd_tooling.py          # sudo opencode (external TUI launcher)
├── editor.py               # In-console editor + file manager (edit, fm)
├── agent.py                # Native in-console AI agent (OpenAI-compatible / Anthropic, tools)
├── fun.py                  # Fun/novelty commands (cowsay, fortune, figlet, neofetch, …)
├── jobs.py                 # Background jobs (`cmd &`, jobs/jobout/jobtail/fg/kill %n)
├── termsess.py             # Persistent PTY sessions keyed by (user, tab)
├── ptyhost.py              # WebSocket ⇄ terminal-session bridge (attach/detach/kill)
├── shellutil.py            # Shared shell detection + subprocess/PTY environment builder
├── state.py                # In-memory session/tab/host state + JSON persistence
├── auth.py                 # User auth & persistence (data/users.json, PBKDF2 hashes)
├── _static_host.py         # Bootstrap: serves a directory over HTTP (used by `serve`)
├── _wsgi_host.py           # Bootstrap: wsgiref fallback WSGI server (used by `gunicorn` when real gunicorn is unavailable)
├── examples/               # Example hosted apps (try them with `sudo serve` / `sudo gunicorn`)
│   └── guestbook/app.py    # Single-file Flask guestbook demo
├── gunicorn.conf.py        # Production server settings (1 worker + threads: state is in-memory)
├── Dockerfile              # Image with proot/git/tini preinstalled (Render, any Docker host)
├── docker-entrypoint.sh    # Fixes root-owned volumes, then drops to uid 10001 (RCONSOLE_RUN_AS_ROOT=1 keeps root)
├── DEPLOY.md               # Full deployment guide: Render, Railway, Fly, VPS, k8s, cloud, home server
├── deploy/                 # docker-compose.yml, Caddyfile, nginx.conf, fly.toml, k8s.yaml, systemd unit, env examples
├── render.yaml             # Render Blueprint (Docker service + persistent disk)
├── requirements.txt        # Pinned runtime deps for Docker/pip (exported from uv.lock)
├── tests/                  # unittest suite (run: python -m unittest discover -s tests)
│   ├── helpers.py          # Isolated temp data dir; must be imported first
│   ├── test_auth.py        # Hashing + migration, policy, quarantine, throttle
│   ├── test_sandbox.py     # Path confinement (symlink/.. escapes), authorization
│   ├── test_multivm.py     # Several VMs per user: separation, picker, targets API, migration, doctor
│   ├── test_distros.py     # Catalog, aliases, mirror resolvers (HTTP faked)
│   ├── test_fetch.py       # neofetch layouts at phone/desktop widths, target awareness
│   ├── test_app.py         # HTTP: login, CSRF, file APIs, proxy, VM-backed commands
│   └── test_vm_e2e.py      # Real proot VM from a locally served rootfs
├── pyproject.toml          # Project metadata + dependencies (uv)
├── uv.lock                 # Resolved dependency lockfile (uv)
├── .python-version         # Pinned Python version
├── .gitignore              # Ignored files (venv, caches, data, logs)
├── README.md               # Project documentation
├── structure.md            # This file
├── static/                 # Front-end assets (served as static)
│   ├── console.js          # Tab/terminal UI, xterm.js wiring, WebSocket client
│   ├── style.css           # Console styling + terminal layout
│   ├── xterm.js            # xterm.js terminal emulator
│   ├── xterm.css           # xterm.js stylesheet
│   └── addon-fit.js        # xterm.js FitAddon (auto-fit terminal to container)
├── templates/              # Jinja2 templates
│   ├── console.html        # Main console page (loads xterm.js + console.js)
│   ├── login.html          # Login page
│   └── error.html          # Themed HTTP error page
├── data/                   # Runtime data (gitignored; override with RCONSOLE_DATA_DIR)
│   ├── users.json          # User accounts (hashed passwords, 0600)
│   ├── secret.key, pepper.key  # Generated secrets (0600) unless set via env
│   ├── state/              # Per-user persisted tab/alias/theme/env/mode state
│   ├── logs/               # Hosted-app logs (<folder>-<port>.log)
│   └── vm/                 # bin/proot, cache/ (rootfs tarballs), <user>/{rootfs,home}
└── .venv/                  # Virtual environment (gitignored)
```

---

## Module descriptions

### Backend (Python)

**`config.py`** — Every setting, read from the environment (data dir, VM,
modes, proxy trust, throttle limits) plus `persistent_secret()`, which creates
random secrets once and stores them `0600` so restarts/workers agree.

**`sandbox.py`** — Execution targets and the one place permissions are decided.
- `spec(sid, cmd, cwd)` / `argv_spec(...)`: argv + cwd + env for the right target
  (`host` or `vm`); raises `PermissionDenied` for non-admins asking for the host.
  `@vm` / `@host` prefixes force a target and are authorized the same way.
- `fs(sid)` → `FsView`: path namespace for file access. VM views resolve symlinks
  and `..` *inside* the root filesystem (`resolve_in_root`) and know the `/root`
  bind mount, so nothing can escape.
- **Several VMs per user** under `vm/<user>/<id>/{rootfs,home}`; `installed_vms`,
  `active_vm` (auto when exactly one), `resolve()` → `Target(mode, vm)` or
  `NeedVmChoice` (the interpreter turns that into a dropdown), `targets()` /
  `set_target()` for the browser menu, `choice()` for the inline picker,
  `@vm:<id>` command prefix. Legacy single-VM layouts migrate on first access.
- VM lifecycle: `install_many(sid, ["debian", "kali"])` (background thread: proot →
  rootfs download with the distro's own checksum → safe extract → per-family
  tweaks → install the `rfetch` shim → probe), `reset`, `describe`, `distro_info`,
  `family`, `rename_user`. Non-admin guests get only safe `/proc/*` info files.

**`distros.py`** — The system catalog. `parse_choice("debian:bookworm")`, aliases
(`কালী` → kali), and one resolver per system that finds the newest image and its
published SHA-256 (Alpine release index, Ubuntu `SHA256SUMS`, linuxcontainers
mirrors for Debian, kali.download for Kali).

**`cmd_fetch.py`** — `neofetch`/`fetch` pick the VM's `rfetch` (vm mode) or
rconsole's own summary (host mode); `layout()` fits logo + details to the width
the browser reports (`cols` in `/api/run`).

**`app.py`** — The Flask application.
- `origin_check()` then `guard()` (before-request): CSRF/Origin check; login,
  30-day inactivity timeout, user-still-exists, forced default-password change.
- `login` (throttled per ip+user and per ip), `console_page`, `/healthz`.
- `/api/run`: dispatches a line of input to `interp.process`.
- `/api/file` (GET/POST), `/api/dir`, `/api/fs`: editor + navigator, all mapped
  through `sandbox.fs` (admins: whole host; others: their VM only).
- `/<folder>/<int:port>/…`: pooled reverse proxy to the hosted app; never
  forwards the console's own session cookie; keeps multiple `Set-Cookie`.
- `/ws`: terminal websocket; the `cmd` query value is authorized by `sandbox`
  before anything is persisted or spawned.

**`main.py`** — Thin entry point that imports `app.main` and runs it.

**`interp.py`** — Command interpreter.
- `process()`: routes built-ins to `commands.py`, interactive commands to a PTY
  (`{"pty": True, "cmd": …}`), background jobs (`trailing &`) to `jobs.py`,
  and everything else to a shell passthrough in the user's target
  (`_passthrough` → `sandbox.spec`, 60s timeout). The command text is passed to
  the shell verbatim (pipes/redirects work); `SHELL_PREFERRED` lets `echo`/`wc`/…
  fall through to the real tools when a line uses shell operators.
- `INTERACTIVE`: the set of commands run through a real PTY. Bare `git` is
  intentionally *not* in the set — only the pager/editor git subcommands
  (`log`/`diff`/`show`/`status`/`blame`/`branch`) take the PTY path, so a bare
  `git` (usage help) stays in the fast captured-output path.
- Shell selection and env construction live in `shellutil.py`.

**`commands.py`** — Thin command registry (`COMMANDS`, `PROTECTED`, `@command`)
plus module wiring. No command logic lives here; each `cmd_*` / `editor` / `fun`
module registers itself via the decorator (same pattern `agent.py` uses).
`interp.py` dispatches via `COMMANDS` and enforces `PROTECTED` (sudo/admin-only).

**`cmd_basic.py`** — help, clear/cls, history, rwhoami, cd (VM-aware, `~`), alias/unalias, theme.

**`cmd_vm.py`** — `vm` (public): status, install, shell (`{"pty": true, "cmd": "@vm"}`),
run, mode, reset, remove.

**`cmd_hosting.py`** — serve / gunicorn (+ bind/port parsing, process launcher
with real-gunicorn vs wsgiref fallback) and sprocess / kprocess.

**`cmd_users.py`** — logout (public), suser / auser / nauser / ruser,
muser / cpass / cuser (interactive prompts), rboot (global factory reset).

**`cmd_jobs.py`** — jobs, jobout, jobtail, fg, kill (delegates `%n` to `jobs.py`,
anything else to the host `kill`).

**`cmd_env.py`** — `sudo env` list/set/unset/import (per-user env vars).

**`cmd_tooling.py`** — `sudo opencode` launcher (local binary → PATH → local
`npm install opencode-ai` background job).

**`editor.py`** — In-console editor (`edit`, nano-style line workflow) and file
manager (`fm`: navigate, cat/rm/mkdir/touch/mv, hand off to the browser editor).
Path helpers (`_resolve_path`, …) also used by `fun.py`'s `rev`/`wc`.

**`agent.py`** — Native in-console AI agent (OpenAI-compatible / Anthropic).
- Tools: `bash`, `read_file`, `write_file`, `list_dir` (run inside rconsole's
  host environment, rendered through the line console — no PTY).
- Persistent per-tab conversation, context compaction, retries, slash commands.

**`fun.py`** — Fun/novelty commands (`cowsay`, `fortune`, `figlet`/`banner`,
  `lolcat`, `sl`, `matrix`, `neofetch`, `colors`, `cal`, `wc`, `rev`, `date`,
  `uptime`, `echo`, `hi`/`hello`). Pure-Python ANSI output.

**`jobs.py`** — Background jobs (`cmd &`): rolling output buffer, live tail
  (`/api/jobtail`), kill by `%n` or PID-tree. Env/shell via `shellutil.py`.

**`termsess.py`** — Persistent PTY sessions keyed by `(user, tab)`.
- Owns the real PTY process + rolling buffer; websockets attach/detach
  without killing the process; explicit `kill` (tab close) tears it down.
- Backends: `pywinpty`/ConPTY on Windows, `pty`/`select`/`termios` on POSIX.

**`ptyhost.py`** — WebSocket ⇄ terminal-session bridge.
- `attach()`: create-or-attach a `termsess.TermSession`, replay backlog,
  pump `data`/`resize`/`kill` messages. `run_pty()` is a backwards-compatible
  alias for `attach()`.

**`shellutil.py`** — Shared helpers: `detect_shell()` (explicit shell
  selection, never `shell=True`) and `build_env()` (venv on `PATH`, sane
  `TERM`/locale/`SHELL` defaults, per-user `sudo env` vars).

**`state.py`** — Global registry (atomic `0600` state files; stores each user's mode).
- `STATE`: per-session tabs/state.
- `ACTIVE_HOSTS`: live hosted apps keyed by `(folder, port)` with their
  `Popen` handles.
- `register_host` / `kill_host` / `kill_all_hosts`: lifecycle management.

**`auth.py`** — Authentication.
- PBKDF2-HMAC-SHA256 with per-user salt + a random persisted pepper (legacy
  unsalted / old-pepper hashes are accepted once, then upgraded).
- `seed_defaults()` creates the `ark` admin (`RCONSOLE_ADMIN_PASSWORD` or `ark`).
- User file: atomic `0600` writes, mtime-cached reads, corrupt file quarantined.
- `LoginThrottle`: exponential lockout used by the login route.
- `verify`, `get_user`, `list_users`, `create_user`, `set_password`, `set_username`,
  `set_admin`, `delete_user`, `reset_to_defaults`.

**`_static_host.py`** — Hosts a directory over HTTP
(`SimpleHTTPRequestHandler`) on `127.0.0.1:<port>`. Used by `serve`;
`--confine` (VM directories) refuses symlinks that leave the directory.

**`_wsgi_host.py`** — wsgiref fallback server for WSGI apps. `sudo gunicorn`
prefers the real `gunicorn` module (`python -m gunicorn <spec> -b
127.0.0.1:<port> --chdir <cwd>`); when gunicorn cannot run on the current
platform, this wsgiref server is used instead.

### Front-end (static/ + templates/)

**`templates/console.html`** — Loads `xterm.js`, `addon-fit.js`, and
`console.js`; defines the tab bar (tabs, ＋, 📁 navigator, theme), terminal
container (`#terms`), file-navigator drawer (`#files`), code editor
(`#editor`), and footer key bar (Tab / Esc / Ctrl / arrows) for touch devices.

**`static/console.js`** — UI controller.
- Tab management; line-mode input; `/api/run` calls.
- `openPty()` / `closePty()` / `updateView()`: xterm.js lifecycle, FitAddon
  auto-fit, WebSocket send/receive, sticky Ctrl for control chars.
- File navigator (`toggleFiles` / `loadDir` / `renderFiles` / `fsApi`): browse
  via `/api/dir`, mutate via `/api/fs`, open files in the editor.
- Editor highlighting: `langFromPath()` (300+ extensions + special basenames
  → ~65 rule ids), `HL_RULES` token patterns, `getHighlighter()` compiler.

**`static/style.css`** — Layout & theming, including the terminal host sizing
that makes the PTY fill the available width.

**`static/xterm.js`, `static/xterm.css`, `static/addon-fit.js`** — Vendored
xterm.js emulator and FitAddon.

### Data & environment

**`data/`** — Runtime state (see the tree above). Gitignored; do not commit.
Set `RCONSOLE_DATA_DIR` to put it on a persistent disk.

**`pyproject.toml` / `uv.lock`** — Dependencies: `flask`, `flask-sock`,
`requests`, and `pywinpty` (Windows only). Managed with `uv`.

**`.python-version`** — Pinned Python (>= 3.14).
