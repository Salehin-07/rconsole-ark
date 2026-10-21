"""Basic console commands: help, clear, history, rwhoami, cd, alias, theme."""
from pathlib import Path

import commands
import sandbox
import state
from version import __version__


@commands.command("help")
def cmd_help(ctx, args):
    std = [
        ("help", "this help"),
        ("clear", "clear the screen"),
        ("cd <dir>", "change directory (stateful)"),
        ("history", "command history for this tab"),
        ("rwhoami", "print the current user"),
        ("version", "print the rconsole version"),
        ("expose <port> <name>", "publish a running server at /<name>/"),
        ("edit <file>", "built-in code editor (great on phones)"),
        ("fm [path]", "in-console file manager + editor"),
        ("vm [install|shell|run|mode]", "your personal Linux VM (root, apk add anything)"),
    ]
    sudo = [
        ("sudo serve <port>", "host cwd as a static site"),
        ("sudo gunicorn <spec> [-b :p]", "host a WSGI app (prompts for port)"),
        ("sudo cpass / cuser", "change password / rename user"),
        ("sudo muser / ruser", "create / delete a user"),
        ("sudo suser / auser / nauser", "list / grant / revoke admin"),
        ("sudo rboot", "factory reset (kill hosts, reset users)"),
    ]
    out = [f"rconsole {__version__} — console commands (type a name, then Enter):", ""]
    for name, desc in std:
        out.append(f"  {name:<16} {desc}")
    out.append("")
    out.append("Shell features:")
    out.append("  alias name=cmd        define a persistent command alias")
    out.append("  unalias <name>        remove an alias")
    out.append("  theme [name]          list / switch console themes")
    out.append("  <cmd> &               run a command in the background")
    out.append("  jobs                  list background jobs")
    out.append("  jobout %n [lines]     show a background job's output")
    out.append("  jobtail %n            live-tail a job's output (any key to stop)")
    out.append("  fg %n                 bring a job's output to the foreground")
    out.append("  kill %n               kill a background job")
    out.append("")
    out.append("Fun commands:")
    out.append("  cowsay <text>  fortune  figlet <text>  banner <text>")
    out.append("  lolcat <text>   sl  matrix  neofetch  colors  cal  wc  rev")
    out.append("  date  uptime  echo -e <text>  hi")
    out.append("")
    out.append("Admin commands (need 'sudo'):")
    for name, desc in sudo:
        out.append(f"  {name:<28} {desc}")
    out.append("")
    out.append("Agent / tooling:")
    out.append("  sudo env set KEY=val   store an env var (e.g. api key) for your sessions")
    out.append("  sudo env import        copy provider keys from the server environment")
    out.append("  sudo env               list / sudo env unset KEY")
    out.append("  sudo opencode [args]   launch the opencode AI agent TUI")
    out.append("  sudo agent [prompt]    native in-console AI agent (no PTY; uses sudo env keys)")
    out.append("  sudo agent help        show agent slash commands (/clear /model /keys /models ...)")
    out.append("  sudo agent models      list models available from the current provider")
    out.append("  Inside `sudo agent`:")
    out.append("    /help /clear /model <name> /provider /keys /models /compact /system /exit")
    out.append("")
    out.append("Any other command runs in your current mode (see `vm mode`):")
    out.append("  host = the server's real shell (admins), vm = your own Linux VM")
    out.append("  ls  pwd  mkdir  cat  rm  cp  mv  echo  whoami  id")
    out.append("  python  pip  git  apk/apt  ...    (cd is native & stateful)")
    out.append("")
    out.append("Hosting is served at /<folder>/<port>/ through rconsole's proxy,")
    out.append("so no extra port-forwarding is needed.")
    out.append("")
    out.append("On a phone, prefer 'edit'/'fm' over nano/vim: the soft keyboard")
    out.append("plus xterm can be fragile for full-screen TUIs (or tap the")
    out.append("📁 button in the tab bar for a visual file navigator).")
    return "\n".join(out)


@commands.command("version")
def cmd_version(ctx, args):
    """Print the rconsole version."""
    return f"rconsole {__version__}"


@commands.command("clear")
@commands.command("cls")
def cmd_clear(ctx, args):
    return {"clear": True}


@commands.command("history")
def cmd_history(ctx, args):
    hist = ctx["tab"]["history"]
    return "\n".join(f"{i+1}  {c}" for i, c in enumerate(hist))


@commands.command("rwhoami")
def cmd_rwhoami(ctx, args):
    return ctx["username"]


@commands.command("cd")
def cmd_cd(ctx, args):
    """Change the tab's working directory (host path, or a path inside the VM)."""
    try:
        view = sandbox.fs(ctx["sid"])
    except sandbox.SandboxError as e:
        return f"cd: {e}"
    target = args[0] if args else "~"
    home = view.default_cwd() if not view.unrestricted else str(Path.home())
    if target == "~" or target.startswith("~/"):
        target = home + target[1:]
    try:
        p = view.host(target, ctx["tab"]["cwd"])
        p = p.resolve() if view.unrestricted else p
    except (OSError, sandbox.SandboxError) as e:
        return f"cd: {target}: {e}"
    if p.is_dir():
        ctx["tab"]["cwd"] = view.virt(p)
        return ""
    if p.exists():
        return f"cd: {target}: Not a directory"
    return f"cd: {target}: No such file or directory"


# ---------------------------------------------------------------------------
# Aliases (per-user, persisted)
# ---------------------------------------------------------------------------

@commands.command("alias")
def cmd_alias(ctx, args):
    """Define or list command aliases.

    Usage:
      alias                 -> list all aliases
      alias name=command    -> define an alias (quotes optional)
      alias name            -> show one alias
    """
    sid = ctx["sid"]
    if not args:
        a = state.get_aliases(sid)
        if not a:
            return "No aliases defined. Try: alias ll=ls -la"
        out = ["Aliases:"]
        for k in sorted(a):
            out.append(f"  alias {k}='{a[k]}'")
        return "\n".join(out)
    arg = " ".join(args)
    if "=" not in arg:
        name = arg.strip()
        a = state.get_aliases(sid).get(name)
        if a is None:
            return f"alias: {name}: not found"
        return f"alias {name}='{a}'"
    name, value = arg.split("=", 1)
    name = name.strip()
    value = value.strip().strip("'\"")
    if not name or " " in name:
        return "alias: invalid name (no spaces allowed)"
    state.set_alias(sid, name, value)
    return ""


@commands.command("unalias")
def cmd_unalias(ctx, args):
    if not args:
        return "Usage: unalias <name>"
    ok = state.remove_alias(ctx["sid"], args[0].strip())
    if not ok:
        return f"unalias: {args[0]}: not found"
    return ""


# ---------------------------------------------------------------------------
# Themes (per-user, persisted; applied live in the browser)
# ---------------------------------------------------------------------------

THEMES = ["default", "light", "amber", "solarized", "dracula", "github",
          "ubuntu", "retro", "nord", "gruvbox", "matrix", "tokyonight",
          "catppuccin", "monokai", "everforest", "ayu", "papercolor"]


@commands.command("theme")
def cmd_theme(ctx, args):
    """List or switch console themes.

    Usage:
      theme            -> list available themes + current
      theme <name>     -> apply a theme (persisted for your account)
    """
    sid = ctx["sid"]
    sess = state.get_session(sid)
    current = sess.get("theme", state.DEFAULT_THEME) if sess else state.DEFAULT_THEME
    if not args:
        out = ["Available themes (use: theme <name>):", ""]
        for t in THEMES:
            mark = " *" if t == current else ""
            out.append(f"  {t:<12}{mark}")
        out.append("")
        out.append(f"Current theme: {current}")
        return "\n".join(out)
    name = args[0].strip().lower()
    if name not in THEMES:
        return f"theme: unknown theme '{name}'. Try: theme  (to list)"
    state.set_theme(sid, name)
    return {"output": f"Theme set to '{name}'.", "theme": name}
