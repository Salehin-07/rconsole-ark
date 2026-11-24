"""What the Rconsole agent knows about the place it runs in.

Two parts: a compact, always-loaded manual (``MANUAL``, kept short because every token counts on small hardware) and
``docs()``, which looks up the full reference (COMMANDS.md, FEATURES.md, ...) on demand through the ``rconsole_docs``
tool. A test checks that every console command is named in the manual, so it cannot silently go stale.
"""
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOC_FILES = ("COMMANDS.md", "FEATURES.md", "FREE_HOSTING.md", "DEPLOY.md", "SECURITY.md", "README.md")

MANUAL = """\
# Where you are: Rconsole
Rconsole (made by Md Abu Salehin) is a browser-based Linux console for small or free servers (Render and similar). \
You are its built-in AI agent, opened from the console with `sudo agent` or at /agent. The person talks to you in a \
chat; the console, the file browser and the dashboard are separate pages of the same app.

## Two kinds of commands (this matters)
1. Real shell commands (ls, git, python, pip, npm, apt, curl, ...) run through your `bash` tool.
2. Rconsole commands are built into the console page: vm, service, expose, unexpose, keepalive, storage, backup, \
reboot, rconsole (update), env, jobs, edit, fm, kprocess, suser/auser/nauser/muser/ruser/cpass/cuser, rboot, agent, \
opencode, theme, alias, history. They do NOT exist in your shell: running them with `bash` fails. When a task needs one, \
tell the person the exact line to type in the console (admin-only ones start with `sudo`). Never try to fake them.

## Targets: host or VM
- Host = the server's own shell (admins only). VM = a private Linux system (Debian default; Ubuntu, Alpine, Kali, Arch) \
run with proot: you are root inside it, it has its own /root, and it is not a real machine (no systemd, no Docker daemon, \
no raw sockets, no kernel features).
- Your system prompt's Environment section says which one you are on. Work only there.
- Inside a VM install software with the VM's own tools: `apt-get install -y x` (Debian/Ubuntu/Kali), `apk add x` (Alpine), \
`pip install x`, `npm i x`. Do NOT run `vm install` for that: `vm install` creates a whole new Linux system and is \
a console command. Toolchains can come from `vm setup <python|node|build|rust|db|tools|docker>` (console command, run by the \
person) or simply apt/apk. Prefer the lightest option; the machine is small and shared.
- On the host (admin) a normal `pip install` / `npm i` also works; do not run apt unless it is there and you are root.
- Docker daemons do not exist here; `docker run` is a userspace shim (no -d, exec, build, compose).

## Running things for the web
- A server you start must listen on 127.0.0.1 or 0.0.0.0 on a port 1024-65535 (not the console's own port). Start long-running \
programs with bash background=true while developing.
- To make an app reachable: `expose <port> <name>` publishes it at https://<domain>/<name>/ behind the console login \
(`--public` for admins). To keep it running across crashes and restarts use `service add <name> [--vm v] [--cwd dir] \
[--env K=V] [--expose PORT:NAME [--public]] <command>` (service list/logs/start/stop/restart/rm). Frameworks need a prefix-aware base \
path or relative links because the app is served under /<name>/.
- Free hosting sleeps and wipes its disk on redeploy: `keepalive` keeps it awake; `storage` keeps files in a GitHub repo; \
`backup` makes encrypted backups that restore on a fresh instance. Work only survives a redeploy if the person has set \
one of these up.

## Other facts
- Long commands: plain console commands are cut at 60 s, your bash tool has its own limit (default 120 s, max 900); use \
background=true for anything longer. `jobs`, `fg`, `kill` manage console jobs.
- Settings, providers and users live in the dashboard (/dashboard). The Dashboard > AI agent tab holds providers and keys. \
Secrets (keys, tokens, settings files) are off limits: never read, print or store them.
- Files: the 📁 browser (download/upload, preview, compress/extract) and `edit <file>` / `fm` are for the person; you use \
read/write/edit/ls/grep.
- Version info: `version` or `rconsole version`; update: `sudo rconsole update` (console command).
- For exact options of any console command, call the `rconsole_docs` tool instead of guessing.
"""

WORKSPACE_RULES = """\
# Where to put things
- Look at the working directory first (the Environment section lists what is in it).
- If it already is a project folder (it has .git, package.json, pyproject.toml, requirements.txt, manage.py, index.html, \
Cargo.toml, go.mod, a README or source files that fit the request) work right there. Do not create another folder inside it.
- If it is the general landing folder (the home/workspace root, empty or full of unrelated things) and the person asks \
for something new (a website, bot, script, app), create a folder for it with a short lowercase name (for example \
`portfolio-site`, `tg-bot`), build everything inside it, and say where it is. Never scatter files across the landing folder.
- Several related files (more than one) always belong in a folder. A single throwaway script may stay where it is.
- When the person names a folder or path, use exactly that. When you pick one, tell them in one line.
- Keep each project self-contained: its own dependencies (a venv or node_modules inside the folder), a short README or \
run notes when it is more than a script, and a .gitignore if you use git.
- After creating a project, make it easy to run: tell the person the one command to start it and, if it serves a page, the \
`expose` line (or `service add` line) to type in the console.
"""

_MARKERS = (".git", "package.json", "pyproject.toml", "requirements.txt", "manage.py", "index.html", "Cargo.toml",
            "go.mod", "Makefile", "composer.json", "Gemfile", "pom.xml", "README.md", "AGENTS.md", "app.py", "main.py")


def cwd_summary(hp, is_default):
    """One line about the working directory for the prompt: what is in it and whether it looks like a project."""
    try:
        names = sorted(os.listdir(hp))
    except OSError:
        return "unreadable", False
    shown = [n + ("/" if os.path.isdir(os.path.join(hp, n)) else "") for n in names if not n.startswith(".") or n in (".git",)]
    found = [m for m in _MARKERS if m in names]
    project = bool(found) and not (is_default and len(names) > 30)
    head = ", ".join(shown[:14]) + (f", ... ({len(shown)} entries)" if len(shown) > 14 else "")
    if not shown:
        return "empty", False
    state = ("looks like a project (" + ", ".join(found[:4]) + ")") if project else \
            ("the general landing folder" if is_default else "a plain folder without project files")
    return f"{state}; contains: {head}", project


def _sections(text):
    """Split a markdown file into (heading, body) pairs at ## and ### headings."""
    out, cur, buf = [], "intro", []
    for line in text.splitlines():
        m = re.match(r"^(#{2,3})\s+(.*)", line)
        if m:
            out.append((cur, "\n".join(buf)))
            cur, buf = m.group(2).strip("` "), [line]
        else:
            buf.append(line)
    out.append((cur, "\n".join(buf)))
    return out


def docs(topic, cap=9000):
    """Reference text for a command or topic: sections of the shipped docs whose heading matches, else a search."""
    t = (topic or "").strip().lower().lstrip("`/ ").rstrip("` ")
    if not t:
        return "Give a command name (for example vm, service, expose) or a topic (for example free hosting, backup)."
    exact, word, loose = [], [], []
    for fn in DOC_FILES:
        p = HERE / fn
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for head, body in _sections(text):
            h = head.lower().strip("` ")
            names = [x.strip("` ") for x in h.split(" / ")]
            words = re.split(r"[\s/,()`:]+", h)
            if t in names:
                exact.append(f"[{fn}] {body.strip()}")
            elif t in words or h.startswith(t + " "):
                word.append(f"[{fn}] {body.strip()}")
            elif t in body.lower():
                loose.append((fn, head))
    hits = exact or word
    if hits:
        return "\n\n".join(hits)[:cap]
    if loose:
        return ("No section is named that. Sections that mention it: "
                + "; ".join(f"{h} ({fn})" for fn, h in loose[:12]) + ". Ask again with one of those names.")
    return "Nothing found. Try a command name from `help`."


def command_names():
    """Every console command registered in the code (used by the consistency test)."""
    import commands
    return sorted(commands.COMMANDS)
