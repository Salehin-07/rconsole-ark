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
