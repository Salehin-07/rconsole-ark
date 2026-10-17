"""``vm`` — manage and enter your per-user virtual machine.

The VM is a Linux root filesystem run under proot, so you are ``root`` inside
it and can ``apk add`` anything, even on platforms that give the server no
privileges (Render, etc.). See :mod:`sandbox` for how it works and its limits.

Subcommands::

    vm                 show status (same as `vm status`)
    vm install         download and set up your VM (one time, ~30s)
    vm shell           open an interactive shell inside the VM
    vm run <command>   run one command inside the VM and print its output
    vm mode [host|vm]  show or choose where plain commands run
    vm reset --yes     recreate the root filesystem (keeps your home dir)
    vm remove --yes    delete the VM *and* its home directory

``vm`` is available to every user: it only ever touches your own VM.
"""
import subprocess

import commands
import sandbox

RUN_TIMEOUT = 120

_USAGE = """\
  vm                 show status
  vm install         download and set up your VM (one time, ~30s)
  vm shell           open an interactive shell inside the VM
  vm run <command>   run one command inside the VM
  vm mode [host|vm]  show or choose where plain commands run
  vm reset --yes     recreate the root filesystem (keeps /root)
  vm remove --yes    delete the VM and its /root home"""


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _status(sid):
    d = sandbox.describe(sid)
    lines = ["VM status", "---------"]
    if not d["supported"]:
        lines.append("  available : no (needs Linux and RCONSOLE_VM=1)")
        return "\n".join(lines)
    state_txt = {"ready": "installed", "absent": "not installed",
                 "installing": "installing…", "failed": "install failed"}[d["state"]]
    lines.append(f"  state     : {state_txt}")
    if d["message"]:
        lines.append(f"  detail    : {d['message']}")
    if d["distro"]:
        lines.append(f"  system    : {d['distro']}")
    if d["disk"] is not None:
        lines.append(f"  disk      : {_human(d['disk'])}")
    lines.append(f"  proot     : {d['proot'] or 'not found (fetched on install)'}")
    lines.append(f"  mode      : {d['mode']} (commands run on the {d['mode']})")
    lines.append(f"  host shell: {'allowed' if d['host_allowed'] else 'admins only'}")
    if d["state"] == "absent":
        lines += ["", "Run `vm install` to set it up."]
    elif d["state"] == "ready":
        lines += ["", "Try: vm shell   |   vm run apk add git   |   vm mode vm"]
    return "\n".join(lines)


def _run(ctx, cmd):
    """Run ``cmd`` in the VM regardless of the current mode."""
    sid = ctx["sid"]
    try:
        sp = sandbox.spec(sid, "@vm " + cmd, ctx["tab"]["cwd"])
        p = subprocess.run(sp.argv, cwd=sp.cwd, env=sp.env, capture_output=True,
                           text=True, timeout=RUN_TIMEOUT, stdin=subprocess.DEVNULL)
    except sandbox.SandboxError as e:
        return f"vm: {e}"
    except subprocess.TimeoutExpired:
        return f"vm: timed out after {RUN_TIMEOUT}s (use `vm shell` for long tasks)"
    except OSError as e:
        return f"vm: {e}"
    return ((p.stdout or "") + (p.stderr or "")).rstrip("\n")


@commands.command("vm")
def cmd_vm(ctx, args):
    sid = ctx["sid"]
    sub = args[0].lower() if args else "status"
    rest = args[1:]

    if sub in ("status", "info"):
        return _status(sid)

    if sub in ("help", "-h", "--help"):
        return "vm — your personal Linux VM\n" + _USAGE

    if sub == "install":
        ok, msg = sandbox.install(sid)
        return f"vm: {msg}" if ok or msg else "vm: nothing to do"

    if sub == "shell":
        try:
            sandbox.resolve_target(sid, sandbox.VM)
        except sandbox.SandboxError as e:
            return f"vm: {e}"
        # The PTY layer re-authorizes this on spawn; '@vm' forces the VM.
        return {"pty": True, "cmd": "@vm"}

    if sub == "run":
        if not rest:
            return "usage: vm run <command>"
        return _run(ctx, " ".join(rest))

    if sub == "mode":
        if not rest:
            return f"mode: {sandbox.effective_mode(sid)}"
        want = rest[0].lower()
        if want == sandbox.VM and not sandbox.installed(sid):
            return "vm: install it first with `vm install`."
        ok, msg = sandbox.set_mode(sid, want)
        if not ok:
            return f"vm: {msg}"
        sandbox.reset_cwds(sid)
        return f"mode: {want} — plain commands now run on the {want}. (tab directories reset)"

    if sub in ("reset", "remove"):
        if "--yes" not in rest:
            what = ("the VM's system files (your /root home is kept)" if sub == "reset"
                    else "the VM AND everything in its /root home")
            return f"This deletes {what}. Re-run with --yes to confirm: vm {sub} --yes"
        if sandbox.effective_mode(sid) == sandbox.VM and sandbox.host_allowed(sid):
            sandbox.set_mode(sid, sandbox.HOST)  # don't strand commands without a VM
        sandbox.reset(sid, keep_home=(sub == "reset"))
        sandbox.reset_cwds(sid)
        return "vm: removed." if sub == "remove" else "vm: reset. Run `vm install` to recreate it."

    return f"vm: unknown subcommand '{sub}'\n{_USAGE}"
