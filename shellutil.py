"""Shared shell/environment helpers for rconsole.

Centralizes logic previously triplicated across ``interp.py``, ``jobs.py``
and ``termsess.py``:

- :func:`detect_shell` — pick an explicit shell executable + flag
  (POSIX ``sh``/``bash`` preferred, Git Bash on Windows, ``cmd.exe`` fallback).
  We never use ``shell=True``; the shell is always invoked explicitly.
- :func:`build_env` — base environment for host subprocesses / PTY sessions:
  venv on ``PATH``, sane ``TERM``/locale defaults (a server started without
  a TTY has none, which breaks TUIs), plus per-user vars from ``sudo env``.
"""

import os
import shutil
import sys
from pathlib import Path

import config


def detect_shell():
    """Return ``(executable, flag)`` for running ``exe flag <cmd>``."""
    candidates = [
        "sh", "bash",
        r"C:\Program Files\Git\bin\sh.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\sh.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ]
    for c in candidates:
        c_p = Path(c)
        if c_p.is_absolute():
            if c_p.exists():
                return str(c_p), "-c"
        elif shutil.which(c):
            return shutil.which(c), "-c"
    if os.name == "nt":
        return "cmd.exe", "/c"
    return "sh", "-c"


def build_env(sid=None):
    """Build the environment dict for a host command / PTY session."""
    env = os.environ.copy()
    # Server secrets must never be inherited by user commands or hosted apps.
    for name in config.SECRET_ENV_VARS:
        env.pop(name, None)
    venv_dir = Path(sys.executable).parent
    path = env.get("PATH", "")
    if str(venv_dir) not in path.split(os.pathsep):
        env["PATH"] = str(venv_dir) + os.pathsep + path
    # Same terminal environment everywhere (line passthrough, background
    # jobs, interactive PTY): a real TERM lets TUIs render instead of hanging.
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    env.setdefault("CLICOLOR", "1")
    env.setdefault("FORCE_COLOR", "1")
    # A default SHELL helps TUIs that spawn a sub-shell or read $SHELL.
    if not env.get("SHELL"):
        for _c in ("/bin/bash", "/usr/bin/bash", "/bin/sh", "/usr/bin/sh"):
            if os.path.exists(_c):
                env["SHELL"] = _c
                break
        else:
            env.setdefault("SHELL", "/bin/sh")
    # Per-user custom environment (e.g. API keys set via `sudo env`).
    if sid:
        try:
            import state as _state
            for k, v in (_state.get_env(sid) or {}).items():
                env[k] = v
        except Exception:
            pass
    return env


# --------------------------------------------------------------- the interactive host shell
HOST_RC_NAME = "host.bashrc"
HOST_RC_TEXT = r"""# Managed by rconsole: prompt with the path, command history on the arrow keys, history kept between sessions.
PS1='\[\e[1;32m\]rconsole@host\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]\$ '
[ -r "$HOME/.bashrc" ] && . "$HOME/.bashrc"          # yours wins: set your own PS1 there if you like
: "${HISTFILE:=$HOME/.bash_history}"
HISTSIZE=5000
HISTFILESIZE=20000
HISTCONTROL=ignoredups
shopt -s histappend checkwinsize 2>/dev/null
case ";$PROMPT_COMMAND;" in *"history -a"*) ;; *) PROMPT_COMMAND="history -a${PROMPT_COMMAND:+;$PROMPT_COMMAND}";; esac
[ -r "$HISTFILE" ] && history -n 2>/dev/null
"""
# dash / busybox ash have no line editing, but they do expand $PWD inside PS1.
HOST_ENV_TEXT = "PS1='rconsole@host:$PWD$ '\n"


def _history_file(cwd):
    """Where the host shell keeps its history: the home directory when writable, else the workspace."""
    for base in (os.environ.get("HOME"), cwd):
        if base and os.path.isdir(base) and os.access(base, os.W_OK):
            return os.path.join(base, ".bash_history")
    return None


def _write_if_changed(path, text):
    try:
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
        return True
    except OSError:
        return False


def interactive_host(cwd):
    """``(argv, extra_env)`` for an interactive host shell.

    ``sh`` is dash on Debian: no arrow-key history and a bare ``$`` prompt. So the interactive shell is
    bash when there is one (readline: up/down history, Ctrl+R, tab completion), started with a managed rc
    file that gives the prompt ``rconsole@host:<path>$`` and saves history between sessions. Without bash
    the plain shell still gets the prompt with the path, through ``$ENV``."""
    extra = {}
    if os.name == "nt":
        exe, _ = detect_shell()
        return [exe], extra
    hist = _history_file(cwd)
    if hist:
        extra["HISTFILE"] = hist
    bash = shutil.which("bash")
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if bash:
        rc = config.DATA_DIR / HOST_RC_NAME
        if _write_if_changed(rc, HOST_RC_TEXT):
            return [bash, "--rcfile", str(rc), "-i"], extra
        return [bash, "-i"], extra
    exe, _ = detect_shell()
    envf = config.DATA_DIR / "host.shrc"
    if _write_if_changed(envf, HOST_ENV_TEXT):
        extra["ENV"] = str(envf)
    return [exe, "-i"], extra
