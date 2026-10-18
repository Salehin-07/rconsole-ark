"""neofetch / fetch / rfetch — a responsive system summary.

``neofetch`` and ``fetch`` are *target aware*:

* **vm mode** (and the VM is installed): runs the ``rfetch`` shim inside your VM,
  so you see the VM's own distro, package count and shell — the same output you
  get by typing ``neofetch`` in ``vm shell``.
* **host mode**: rconsole's own summary of the server it runs on.

``rfetch`` always shows rconsole's summary. Every layout adapts to the width of
your screen (the browser reports it), so it stays readable on a phone:

    wide    logo on the left, details on the right
    medium  logo above the details
    narrow  details only, long values shortened with "~"
"""
import os
import re
import shutil
import subprocess
import sys
import time

import commands
import sandbox
import state
from fun import BOLD, RESET, START_TIME, _count_threads, _fmt_uptime, _mem_info, fg

DEFAULT_COLS = 80
MIN_INFO_WIDTH = 30          # info column needs at least this much beside a logo
MAX_STACKED_LOGO_ROWS = 7    # taller logos are dropped on narrow screens
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

LOGO = [
    "  ██████",
    "  ██  ██",
    "  ██  ██",
    "  ██  ██",
    "  ██████",
    "  ██  ██",
    "  ██ ██",
    "  ████",
    "  ██ ██",
    "  ██  ██",
]


def visible_len(s):
    """Printed width of ``s`` (ANSI colour codes take no space)."""
    return len(_ANSI.sub("", s))


def truncate(s, width):
    """Cut ``s`` to ``width`` printed columns, keeping colours balanced.

    A shortened line ends in ``~`` so it is clear something was dropped.
    """
    if visible_len(s) <= width:
        return s
    out, seen, i = [], 0, 0
    keep = max(width - 1, 0)
    while i < len(s) and seen < keep:
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(s[i])
        seen += 1
        i += 1
    return "".join(out) + "~" + RESET


def layout(logo, info, cols, color=2):
    """Arrange ``logo`` and ``info`` (lists of lines) for a ``cols``-wide screen."""
    cols = max(20, int(cols or DEFAULT_COLS))
    lw = max((visible_len(l) for l in logo), default=0)
    if logo and cols >= lw + 2 + MIN_INFO_WIDTH:
        room = cols - lw - 2
        rows = []
        for i in range(max(len(logo), len(info))):
            l = logo[i] if i < len(logo) else ""
            r = info[i] if i < len(info) else ""
            rows.append(fg(color) + l.ljust(lw) + RESET + "  " + truncate(r, room))
        return "\n".join(rows) + RESET
    rows = []
    # A tall logo stacked above the details would push everything off a phone
    # screen, so only short logos are shown in the narrow layout.
    if logo and cols >= lw and len(logo) <= MAX_STACKED_LOGO_ROWS:
        rows += [fg(color) + l + RESET for l in logo]
    rows += [truncate(r, cols) for r in info]
    return "\n".join(rows) + RESET


def _host_info(ctx):
    try:
        import socket
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    username = ctx.get("username", "user")
    theme = "default"
    try:
        s = state.get_session(username)
        if s:
            theme = s.get("theme", "default")
    except Exception:
        pass
    try:
        du = shutil.disk_usage(os.getcwd())
        disk = "%d/%d GB" % (du.free // 1024 ** 3, du.total // 1024 ** 3)
    except Exception:
        disk = "n/a"
    mode = sandbox.effective_mode(ctx.get("sid", username))
    b = fg(4)
    return [
        BOLD + str(username) + RESET + "@rconsole",
        b + "-----------" + RESET,
        "OS: " + b + "rconsole (%s)" % sys.platform + RESET,
        "Host: " + b + str(host) + RESET,
        "Shell: " + b + "rconsole-sh" + RESET,
        "Term: " + b + "xterm.js" + RESET,
        "Python: " + b + sys.version.split()[0] + RESET,
        "CPU: " + b + "%s cores" % (os.cpu_count() or "?") + RESET,
        "Memory: " + b + _mem_info() + RESET,
        "Disk(/): " + b + disk + RESET,
        "Target: " + b + mode + RESET,
        "Theme: " + b + str(theme) + RESET,
        "Role: " + b + ("admin" if ctx.get("is_admin") else "user") + RESET,
        "Uptime: " + b + _fmt_uptime(int(time.time() - START_TIME)) + RESET,
        "Threads: " + b + str(_count_threads()) + RESET,
        "PID: " + b + str(os.getpid()) + RESET,
    ]


def rconsole_fetch(ctx, args=()):
    """rconsole's own summary, laid out for the client's width."""
    logo = [] if "--no-logo" in args else LOGO
    return layout(logo, _host_info(ctx), ctx.get("cols") or DEFAULT_COLS)


def _vm_fetch(ctx, args):
    """Run the in-VM ``rfetch`` shim. Returns its output, or ``None`` on failure."""
    flags = " ".join(a for a in args if a in ("--no-logo", "--plain"))
    try:
        sp = sandbox.spec(ctx["sid"], ("@vm rfetch " + flags).strip(), ctx["tab"]["cwd"])
        env = dict(sp.env, COLUMNS=str(ctx.get("cols") or DEFAULT_COLS))
        p = subprocess.run(sp.argv, cwd=sp.cwd, env=env, capture_output=True, text=True,
                           timeout=15, stdin=subprocess.DEVNULL)
    except (sandbox.SandboxError, OSError, subprocess.SubprocessError):
        return None
    return p.stdout.rstrip("\n") if p.returncode == 0 and p.stdout.strip() else None


@commands.command("neofetch")
@commands.command("fetch")
def cmd_neofetch(ctx, args):
    sid = ctx["sid"]
    if sandbox.effective_mode(sid) == sandbox.VM:
        if sandbox.installed(sid):
            out = _vm_fetch(ctx, args)
            if out:
                return out
        else:
            return (rconsole_fetch(ctx, args)
                    + "\n\nNo VM yet: run `vm list`, then `vm install <name>` "
                      "to see your own Linux here.")
    return rconsole_fetch(ctx, args)


@commands.command("rfetch")
def cmd_rfetch(ctx, args):
    return rconsole_fetch(ctx, args)
