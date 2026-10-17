"""Command line parsing and execution dispatcher.

Built-in console commands are handled in commands.py. Anything else is executed
through :mod:`sandbox` — on the real host (admins) or inside the user's VM —
via an explicit shell subprocess (real cwd, timeout, venv on PATH).
"""
import shlex
import subprocess

import commands
import jobs
import sandbox
import state

PASS_THROUGH_TIMEOUT = 60

# Commands that need a real, interactive terminal (full-screen TUIs, REPLs,
# pagers, remote shells, ...). These are run through a PTY rather than the
# captured stdout passthrough so that programs like nano/vim/python work.
INTERACTIVE = {
    "nano", "vim", "vi", "emacs", "nvim",
    "less", "more", "man", "most",
    "top", "htop", "btm", "glances", "watch",
    "python", "python3", "pip", "py", "ipython", "node", "npm", "npx", "opencode", "irb", "lua", "php",
    "bash", "sh", "zsh", "fish", "powershell", "pwsh", "cmd", "cmd.exe",
    "ssh", "telnet", "tmux", "screen", "lynx", "w3m", "mutt", "irssi",
    "fzf", "ranger", "nnn", "mc",
}


def _is_interactive(tokens):
    if not tokens:
        return False
    name = tokens[0]
    # Strip a leading path / extension for the comparison.
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = base.rsplit(".", 1)[0].lower()
    if base in INTERACTIVE:
        return True
    # Heuristics: git/ssh subcommands that drop into a pager or editor.
    if base in ("git",) and len(tokens) > 1 and tokens[1] in (
        "log", "diff", "show", "status", "blame", "branch", "-pager-err",
    ):
        return True
    return False


# Console-native novelty commands that shadow a real Unix tool. When the line
# uses shell operators (pipes, redirects, &&, ;) the real tool must run in the
# shell, otherwise e.g. `echo hi | tr a-z A-Z` would just print its own text.
SHELL_PREFERRED = {"echo", "cal", "wc", "rev", "date", "uptime"}


def has_shell_ops(line):
    """True if ``line`` contains an unquoted pipe/redirect/``&&``/``;`` operator."""
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return False
    return any(t and all(c in "();<>|&" for c in t) for t in toks)


def tokenize(line):
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def _expand_alias(tokens, aliases):
    """Replace the first token with its alias value (single pass)."""
    if not tokens or not aliases:
        return tokens
    name = tokens[0]
    if name in aliases:
        try:
            repl = shlex.split(aliases[name])
        except ValueError:
            repl = aliases[name].split()
        return repl + tokens[1:]
    return tokens


def _passthrough(ctx, host_cmd):
    """Run a command string via the user's target (host or VM) and capture it."""
    cwd = ctx["tab"]["cwd"]
    try:
        sp = sandbox.spec(ctx["sid"], host_cmd, cwd)
        proc = subprocess.run(
            sp.argv,
            cwd=sp.cwd,
            capture_output=True,
            text=True,
            timeout=PASS_THROUGH_TIMEOUT,
            shell=False,
            env=sp.env,
            stdin=subprocess.DEVNULL,
        )
    except sandbox.SandboxError as e:
        return f"rconsole: {e}"
    except subprocess.TimeoutExpired:
        return (f"command timed out after {PASS_THROUGH_TIMEOUT}s "
                "(append ' &' to run it as a background job)")
    except Exception as e:
        return f"error: {e}"
    out = proc.stdout or ""
    err = proc.stderr or ""
    return (out + err).rstrip("\n")


def process(tab, username, is_admin, sid, raw):
    """Process a line of input for a tab.

    Returns a dict with some of: output, clear, action, update_session, prompt.
    """
    line = raw.rstrip("\n")

    # Interactive command awaiting input?
    if tab.get("gen") is not None:
        gen = tab["gen"]
        try:
            r = gen.send(line)
        except StopIteration as e:
            tab["gen"] = None
            tab["prompt"] = None
            return _normalize(e.value)
        return _normalize_gen(r)

    if line.strip() == "":
        return {}

    tokens = tokenize(line)
    if not tokens:
        return {}

    sudo = False
    # ``host_cmd`` is the text handed to the shell. It is edited as *text*
    # (never rebuilt with shlex.join) so pipes, redirects and && survive.
    host_cmd = line.strip()
    if tokens[0] == "sudo":
        sudo = True
        host_cmd = host_cmd[4:].lstrip()  # drop the leading "sudo"
        tokens = tokens[1:]
        if not tokens:
            return {"output": "usage: sudo <command>"}

    # Background job? A trailing '&' detaches the command from the console.
    # Besides the spaced form (`sleep 30 &`), accept the glued form
    # (`sleep 30&`) — but never treat `&&` as a background marker.
    bg = False
    if tokens and tokens[-1] == "&":
        bg = True
        tokens = tokens[:-1]
    elif (tokens and tokens[-1].endswith("&")
            and not tokens[-1].endswith("&&")):
        bg = True
        tokens[-1] = tokens[-1][:-1]
        if not tokens[-1]:
            tokens.pop()
    if bg and not tokens:
        return {"output": "syntax error near unexpected token `&'"}
    if bg:
        host_cmd = host_cmd.rstrip()[:-1].rstrip()  # drop the trailing '&'

    # Expand a user alias on the (post-sudo) command.
    aliases = state.get_aliases(sid)
    expanded = _expand_alias(tokens, aliases)
    if expanded is not tokens and tokens and tokens[0] in aliases:
        # Substitute the alias in the text, keeping the rest verbatim.
        first = host_cmd.split(None, 1)
        host_cmd = aliases[tokens[0]] + (" " + first[1] if len(first) > 1 else "")
    tokens = expanded

    name = tokens[0]
    args = tokens[1:]

    ctx = {
        "username": username,
        "is_admin": is_admin,
        "tab": tab,
        "sid": sid,
        "sudo": sudo,
        "raw": raw,
    }

    if sudo and not is_admin:
        return {"output": f"{username} is not in the sudoers file. This incident will be reported."}

    # Console-native built-in? (shell-operator lines go to the real shell when
    # the built-in merely shadows a Unix tool — see SHELL_PREFERRED.)
    native = name in commands.COMMANDS and not (
        name in SHELL_PREFERRED and has_shell_ops(host_cmd))
    if native:
        if name in commands.PROTECTED and not sudo:
            return {"output": f"Permission denied: '{name}' requires sudo. Try: sudo {name}"}
        fn = commands.COMMANDS[name]
        try:
            result = fn(ctx, args)
        except Exception as e:
            return {"output": f"{name}: error: {e}"}
        if hasattr(result, "send") and hasattr(result, "__next__"):
            try:
                r = next(result)
                tab["gen"] = result
                tab["prompt"] = None
                return _normalize_gen(r)
            except StopIteration as e:
                return _normalize(e.value)
        return _normalize(result)

    # Everything else -> run on the real host shell.
    if bg:
        # A trailing '&' always wins: even an interactive program (python,
        # vim, ...) should be detached rather than opened in a PTY.
        jid, _ = jobs.start_job(sid, host_cmd, ctx["tab"]["cwd"])
        return {"output": f"[job {jid}] {host_cmd} &   (jobs: list | jobout %{jid}: output | kill %{jid}: stop)"}
    # `@vm vim x` / `@host top`: judge interactivity by the real command.
    probe = tokens[1:] if tokens and tokens[0] in ("@vm", "@host") else tokens
    if _is_interactive(probe):
        # Hand off to a real PTY so interactive programs work.
        return {"pty": True, "cmd": host_cmd}
    return {"output": _passthrough(ctx, host_cmd)}


def _normalize(result):
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    return {"output": str(result)}


def _normalize_gen(r):
    """Normalize a value yielded by an interactive generator.

    A generator may yield either a plain prompt string or a dict carrying a
    prompt plus optional fields (output, clear) so an interactive command (e.g.
    an in-console editor) can render content between user inputs.
    """
    if isinstance(r, dict):
        r.setdefault("prompt", "")
        return r
    return {"prompt": str(r)}
