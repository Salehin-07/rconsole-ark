# Why Rconsole exists, what it is, and what it can do

The long form of the project overview that used to fill the README. The README is now a short front page; nothing was removed, it lives here and in the other pages of this documentation.

## Why this exists

Plenty of providers give you a **free tier for a small website** — but *bot hosting* and *background workers*
are never free. If you have no budget, that leaves you with nothing: you cannot run a Telegram bot, a scraper, a
queue worker or an API with a background job anywhere, because every free plan sleeps, wipes its disk, and gives
you no shell.

Rconsole is built for exactly that person. Put it on **one free web service** (Render's free tier is the
reference) and you get a small Linux server that you control **from your phone, from anywhere** and that the free tier's idle
suspension does not take offline:

* **A real terminal and your own Linux environments** (Debian, Ubuntu, Kali, Alpine; PRoot-based, not true virtual
  machines) where you are `root` and can install anything — no host privileges needed.
* **Run anything, keep it running**: `service add` starts bots and workers at boot and restarts them when they
  crash. Publish websites (static *or* dynamic) with `service add … --expose` / `expose`.
* **It resists idle suspension**: a built-in keep-alive pings its own public URL every 10 minutes, which prevents
  *inactivity-based* sleeping. It cannot prevent provider restarts, maintenance, an exhausted monthly allowance or
  policy changes.
* **A wiped disk isn't the end**: encrypted backups go to a free private git repo (every 15 minutes when something
  changed); a fresh instance restores itself, rebuilds your Linux systems and starts your services again. Anything
  changed since the last backup, and every running process, is lost (see [Backups](backup.md)). `sudo reboot` restarts in place when you just need a
  restart, with no redeploy.
* **Made for a thumb**: a responsive UI you can install as an app.

It is designed for **one person or a small team** (default cap: 10 accounts) — not for the public — so it keeps
things simple and small. Be honest about the limits: free instances have ~512 MB RAM, restart when the provider
decides, and the Linux environments are a compatibility layer rather than a hard sandbox. "Free" means free on a
platform that has a free tier; other hosts in DEPLOY.md may charge. FREE_HOSTING.md spells out what to expect.

## What it is

Rconsole is a Flask web application that exposes a **Linux console** in the browser. It blends two modes:

1. **Line-mode console** — a stateful shell (`cd`, `help`, services, hosted-app management, user
   administration, background jobs, an AI agent) implemented natively in Python.
2. **Real interactive terminal** — full-screen, real programs (`nano`, `vim`, `python`, `bash`, `less`, `top`,
   `git`, `ssh`, …) streamed over a WebSocket-backed PTY.

Commands run on one of two **targets**:

| Target | What it is | Who |
|--------|------------|-----|
| `host` | The machine Rconsole runs on, with the server's privileges | admins |
| `vm`   | **Your own Linux system** (Debian by default) where you are `root` and can `apt install` anything — works even on locked-down hosts such as Render | everyone |

## Features

- Browser terminal powered by **xterm.js** + a real PTY (ConPTY on Windows,
  `pty`/`select` on Linux/macOS).
- **Several Linux environments (PRoot) per user** (`vm`): **Alpine, Debian, Ubuntu, Kali** —
  one, some or all, each with its own root filesystem and persistent `/root`. Be
  root inside, install any package, no host privileges needed; switch with a
  dropdown (see [The VM](vm.md#the-vm-vm)).
- **`service`**: bots and workers that **start at boot and restart when they crash** (`service add tgbot --vm
  debian python bot.py`), with logs, env vars and optional web exposure.
- **Free-tier survival**: built-in **keep-alive** (prevents inactivity-based suspension), **encrypted backups + automatic restore**
  (VMs and toolchains rebuilt, services restarted), `sudo reboot` without a redeploy.
- **`rconsole update`**: checks GitHub for a new release and installs it safely — console locked while it runs,
  checksum-verified, tested before it is applied, data untouched, automatic rollback. See
  [Updating rconsole](#updating-rconsole).
- **Installable on a phone** (home-screen app) with a layout built for small screens.
- **`expose <port> <name>`**: run any server you like (`python main.py &`, `npm run dev`, …) on the host or in a
  VM and publish it at `https://<domain>/<name>/` — private by default, `--public` for admins.
- **`neofetch` / `fetch`** that follow your target (the VM's own system in `vm`
  mode, Rconsole's in `host` mode) and adapt to phone-sized screens.
- Tabs, each owning its own terminal session that keeps running in the
  background and reappears when you switch back.
- Native, stateful console commands plus a real shell passthrough (pipes,
  redirects and `&&` work as in any shell).
- Visual file manager (📁 button): browse, open, create/rename/delete — in the host filesystem (admins) or confined
  to your VM — with multi-select (click, Ctrl/Shift, drag a rectangle, long-press on phones), **compress / extract**
  (zip, tar, tar.gz/bz2/xz, gz, 7z and more; RAR and 7z extraction), a **Properties** dialog and multi-delete.
- Built-in code editor with syntax highlighting for ~65 languages, auto-indent, auto-closing brackets and quotes,
  find/replace, comment toggling, line shortcuts, an adjustable font size and a touch symbol bar.
- Hosted web apps behind a path-based proxy: start them yourself and `expose` them, or let
  the `service` supervisor run them (autostart, crash restart, logs).
- Visual file browser (📁) that **shows photos, PDFs and Word / Excel / PowerPoint files and plays videos and music** in a built-in viewer (big PDFs open at once and load only the pages you look at; Office files are previewed in your browser, nothing is converted on the server), with **download** (files, or whole folders as a `.zip`) and
  **upload** (files or entire folders).
- **Dashboard** (`/dashboard`, ⚙ menu): **admins** get live CPU / memory / disk / network graphs, every process, service,
  job and terminal with kill / restart / logs, user management (create, promote, demote, reset, delete), per-system
  views, disk usage, an audit trail and alerts. **Normal users** get a limited dashboard with only their own
  services, apps, systems, jobs and terminals (read-only, with logs). **Everyone** can change their own password
  under **Account**, like `sudo cpass`. Plus a **logout button** next to the theme and system buttons.
- The file editor never opens **compressed or binary files** (archives, images, executables, databases…): it tells you
  why and offers the download instead, so a save can never corrupt them.
- Accounts with admin/non-admin roles, brute-force protection, a warning in the dashboard while the factory
  password is in use, session timeout, and factory reset.
- Native **AI agent** (`sudo agent`): streaming chats with tools, diffs, approvals and saved sessions; OpenRouter, Hugging Face, OpenAI, Anthropic, Groq and more, set up in the dashboard.

## Requirements

- Python **>= 3.14**, 3.15 recommended (see `.python-version`). Older versions are not supported. See [docs/compatibility.md](compatibility.md).
- [`uv`](https://docs.astral.sh/uv/) for dependency management (or `pip install -r requirements.txt`).
- **VM**: Linux. `proot` is fetched automatically on x86-64 (checksum-verified);
  on other CPUs install **proot 5.5.0 or newer** yourself (`apt install proot` / `apk add proot` may be older: check
  `proot --version`, or point `RCONSOLE_VM_PROOT` at a newer build).
- **Windows**: `pywinpty` (ConPTY) is installed automatically; the VM is unavailable there, and gunicorn is not
  installed — use `uv run python serve.py` (built-in server fallback).
