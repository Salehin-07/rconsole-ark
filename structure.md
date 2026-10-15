# rconsole — file structure

```
rconsole/
├── app.py                  # Flask app: routes, auth guard, hosted-app proxy, /ws endpoint
├── main.py                 # Entry point (calls app.main())
├── interp.py               # Command parsing/dispatch; interactive detection; host passthrough
├── commands.py             # Thin registry (COMMANDS/PROTECTED/@command) + module wiring
├── cmd_basic.py            # help, clear, history, rwhoami, cd, alias, theme
├── cmd_hosting.py          # serve / gunicorn / sprocess / kprocess (+port parsing, launcher)
├── cmd_users.py            # logout, suser/auser/nauser/ruser, muser/cpass/cuser, rboot
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
├── data/                   # Runtime data (gitignored)
│   ├── users.json          # User accounts (hashed passwords)
│   └── state/              # Per-user persisted tab/alias/theme/env state
└── .venv/                  # Virtual environment (gitignored)
```

---

## Module descriptions

### Backend (Python)

**`app.py`** — The Flask application.
- `guard()` (before-request): enforces login, 30-day inactivity timeout, and that
  the logged-in user still exists.
- `login` / `console_page`: auth + console rendering.
- `/api/run`: dispatches a line of input to `interp.process`.
- `/api/file` (GET/POST): read / save a single file for the browser editor.
- `/api/dir` (GET): list a directory for the visual file navigator.
- `/api/fs` (POST): `mkdir` / `touch` / `rm` / `rmtree` / `rename` for the navigator.
- `/<folder>/<int:port>/…`: reverse-proxy (`_do_proxy`) forwarding to the
  registered hosted app at `127.0.0.1:<port>`.
- `/ws`: WebSocket route → `ptyhost.run_pty`, streaming a real PTY.
- `main()`: starts the dev server on `PORT` (default 5000).

**`main.py`** — Thin entry point that imports `app.main` and runs it.

**`interp.py`** — Command interpreter.
- `process()`: routes built-ins to `commands.py`, interactive commands to a PTY
  (`{"pty": True, "cmd": …}`), background jobs (`trailing &`) to `jobs.py`,
  and everything else to a real host-shell passthrough (`_passthrough`, 60s timeout).
- `INTERACTIVE`: the set of commands run through a real PTY. Bare `git` is
  intentionally *not* in the set — only the pager/editor git subcommands
  (`log`/`diff`/`show`/`status`/`blame`/`branch`) take the PTY path, so a bare
  `git` (usage help) stays in the fast captured-output path.
- Shell selection and env construction live in `shellutil.py`.

**`commands.py`** — Thin command registry (`COMMANDS`, `PROTECTED`, `@command`)
plus module wiring. No command logic lives here; each `cmd_*` / `editor` / `fun`
module registers itself via the decorator (same pattern `agent.py` uses).
`interp.py` dispatches via `COMMANDS` and enforces `PROTECTED` (sudo/admin-only).

**`cmd_basic.py`** — help, clear/cls, history, rwhoami, cd, alias/unalias, theme.

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

**`state.py`** — Global registry.
- `STATE`: per-session tabs/state.
- `ACTIVE_HOSTS`: live hosted apps keyed by `(folder, port)` with their
  `Popen` handles.
- `register_host` / `kill_host` / `kill_all_hosts`: lifecycle management.

**`auth.py`** — Authentication.
- PBKDF2-HMAC-SHA256 with per-user salt + server pepper (legacy unsalted
  `sha256(password + PEPPER)` hashes are accepted once, then upgraded).
- `seed_defaults()` creates `ark`/`ark` admin on first run.
- `verify`, `get_user`, `list_users`, `create_user`, `set_password`,
  `set_username`, `reset_to_defaults` (user file guarded by a thread lock).

**`_static_host.py`** — Hosts a directory over HTTP
(`SimpleHTTPRequestHandler`) on `127.0.0.1:<port>`. Used by `serve`.

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

**`data/`** — Runtime state. Contains `users.json` (hashed credentials).
Gitignored; do not commit.

**`pyproject.toml` / `uv.lock`** — Dependencies: `flask`, `flask-sock`,
`requests`, and `pywinpty` (Windows only). Managed with `uv`.

**`.python-version`** — Pinned Python (>= 3.14).
