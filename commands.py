"""Console-native command registry for the rconsole shell.

Most commands (ls, cat, mkdir, rm, cp, mv, python, git, ...) are NOT implemented
here; they are executed on the real host via the subprocess passthrough in
interp.py. The console-native commands that need special handling live in
focused modules and register themselves here with :func:`command`:

- ``cmd_basic``    help, clear, history, rwhoami, cd, alias, theme
- ``cmd_hosting``  kprocess
- ``cmd_vm``       vm (per-user Linux VM: list, install, shell, run, mode)
- ``cmd_expose``   expose / unexpose (publish a local port at /<name>/)
- ``cmd_service``   service (supervised bots/workers: autostart, restart on crash)
- ``cmd_reboot``    reboot (restart without redeploying)
- ``cmd_update``    rconsole version / update (self-update from GitHub releases)
- ``cmd_backup``    backup (encrypted backups to a git repo / folder)
- ``cmd_keepalive`` keepalive (self-ping so free instances never sleep)
- ``cmd_fetch``    neofetch / fetch (target aware, responsive) and rfetch
- ``cmd_users``    logout, suser/auser/nauser/ruser, muser/cpass/cuser, rboot
- ``cmd_jobs``     jobs, jobout, jobtail, fg, kill
- ``cmd_env``      sudo env (per-user environment variables)
- ``cmd_tooling``  sudo opencode (external TUI launcher)
- ``editor``       edit, fm (in-console editor + file manager)
- ``fun``          novelty commands (cowsay, fortune, figlet, ...)
- ``agent``        native in-console AI agent (registered manually below)

interp.py dispatches via COMMANDS and enforces PROTECTED (sudo/admin-only).
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Console-native commands that MUST be prefixed with `sudo` (admin only).
# NOTE: `logout` is deliberately public — anyone may log themselves out.
PROTECTED = {"rboot", "cpass", "cuser", "muser",
             "kprocess", "suser", "auser", "nauser", "ruser",
             "env", "opencode", "agent", "backup", "reboot"}

# Registry of built-in commands.
COMMANDS = {}


def command(name):
    def deco(fn):
        COMMANDS[name] = fn
        return fn
    return deco


# Register implementations. Each module imports this registry for the
# decorator (defined above), so import order here is safe.
import cmd_basic  # noqa: E402,F401
import cmd_env  # noqa: E402,F401
import cmd_hosting  # noqa: E402,F401
import cmd_jobs  # noqa: E402,F401
import cmd_tooling  # noqa: E402,F401
import cmd_users  # noqa: E402,F401
import cmd_vm  # noqa: E402,F401
import cmd_fetch  # noqa: E402,F401
import cmd_expose  # noqa: E402,F401
import cmd_keepalive  # noqa: E402,F401
import cmd_backup  # noqa: E402,F401
import cmd_reboot  # noqa: E402,F401
import cmd_update  # noqa: E402,F401
import cmd_service  # noqa: E402,F401
import editor  # noqa: E402,F401

# Register the fun/novelty commands (cowsay, fortune, figlet, ...).
import fun  # noqa: E402,F401

# Register the native in-console AI agent (a PTY-free alternative to opencode).
import agent  # noqa: E402,F401
COMMANDS["agent"] = agent.cmd_agent
