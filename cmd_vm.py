"""``vm`` — manage and enter your virtual machines.

You can have **several** systems installed at once (Alpine, Debian, Ubuntu,
Kali, ...). Each one has its own root filesystem and its own ``/root``; nothing
is shared between them. Inside a VM you are ``root`` and can install anything,
even on platforms that give the server no privileges (Render, etc.).

Subcommands::

    vm                    show your systems and which one is active
    vm list               show every system you can install
    vm install <name>...  install one or more (or `all`), e.g. debian ubuntu:24.04
    vm shell [name]       open an interactive shell in a VM
    vm run [--vm name] <command>   run one command in a VM
    vm use <name>         make a VM the one plain commands run in
    vm mode [host|vm [name]]       choose where plain commands run
    vm reset [name] --yes          recreate a VM's system files (keeps /root)
    vm remove <name|all> --yes     delete a VM and its /root
    vm setup <profile> [name]      install Python/build tools/Node/... (see `vm setup`)
    vm doctor [name]      check that a VM works and show what is wrong if not

With a single VM you never need to type its name. With several, commands that
need to know which one show a picker (a dropdown in the browser).

``vm`` is available to every user: it only ever touches your own VMs.
"""
import platform
import subprocess
import textwrap
import time

import commands
import devsetup
import distros
import jobs
import sandbox

RUN_TIMEOUT = 90      # under Render's 100 s HTTP limit; use `vm shell` or `&` for more

_USAGE = """\
  vm                    your systems and the active one
  vm list               systems you can install
  vm install <name>...  alpine | debian | ubuntu | kali | all
                        e.g. vm install debian ubuntu:24.04
  vm shell [name]       interactive shell in a VM
  vm run [--vm n] <cmd> run one command in a VM
  vm use <name>         make a VM the default
  vm mode [host|vm [n]] where plain commands run
  vm reset [name] --yes recreate system files (keeps /root)
  vm remove <name|all> --yes   delete a VM and its /root
  vm setup <profile> [n] install python, build tools, node ... (`vm setup` lists them)
  vm doctor [name]      diagnose a VM"""


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _wrap(text, cols, indent=8):
    pad = " " * indent
    return textwrap.fill(text, width=max(cols, indent + 12), initial_indent=pad,
                         subsequent_indent=pad, break_long_words=False)


# ------------------------------------------------------------------- pickers
def _picker(sid, text, template, install=True):
    """A reply that makes the browser show a dropdown of systems."""
    return {"output": text, "choice": sandbox.choice(sid, template=template, install=install)}


def _no_vm_reply(sid):
    return {"output": "You have no system yet. Pick one to install:",
            "choice": sandbox.choice(sid)}


def _pick_vm(sid, rest, verb, template):
    """Work out which VM a command is about.

    Returns ``(vm, remaining_args, reply)``; ``reply`` is set instead of ``vm``
    when the user has to choose (picker) or something is wrong.
    """
    vms = sandbox.installed_vms(sid)
    if rest and rest[0] in vms:
        return rest[0], rest[1:], None
    if rest and rest[0] in distros.CATALOG:
        return None, rest, (f"vm: {rest[0]} is not installed. Run `vm install {rest[0]}` "
                            "first (or `vm list`).")
    if not vms:
        return None, rest, _no_vm_reply(sid)
    active = sandbox.active_vm(sid)
    if active:
        return active, rest, None
    return None, rest, _picker(sid, f"Which system should `{verb}` use?", template)


# -------------------------------------------------------------------- status
def _status(sid, cols, only=None):
    if not sandbox.supported():
        return "VM status\n---------\n  available : no (needs Linux and RCONSOLE_VM=1)"
    infos = sandbox.describe_all(sid)
    if only:
        infos = [i for i in infos if i["vm"] == only]
        if not infos:
            return f"vm: no system called '{only}' (see `vm list`)."
    mode = sandbox.effective_mode(sid)
    active = sandbox.active_vm(sid)
    lines = []
    if not infos:
        lines += ["You have no systems yet.", "",
                  "Run `vm list` to see what you can install, then",
                  "`vm install <name>` (plain `vm install` gives you Debian)."]
    else:
        lines += [f"Your systems ({len(infos)})", "-----------------"]
        for i in infos:
            mark = "*" if i["vm"] == active and mode == sandbox.VM else " "
            st = {"ready": "", "installing": " (installing…)", "failed": " (install FAILED)",
                  "absent": ""}[i["state"]]
            title = i["distro"] or i["label"] or i["vm"]
            size = f" · {_human(i['disk'])}" if i["disk"] is not None else ""
            lines.append(f"{mark} {i['vm']:<8}{title}{size}{st}")
            if i["message"] and i["state"] in ("installing", "failed"):
                lines.append(_wrap(i["message"], cols or 80, 11))
        if only and infos[0]["family"]:
            pm = "apk add <pkg>" if infos[0]["family"] == "alpine" else "apt install -y <pkg>"
            lines.append(f"  packages : {pm}")
        lines.append("")
        lines.append("Tip: `vm setup python` installs Python + build tools (psycopg2, Pillow, …).")
        if mode == sandbox.HOST:
            lines.append("Plain commands run on the host (admin). `vm mode vm` to switch.")
        elif active:
            lines.append(f"Plain commands run in: {active}   (* = active)")
        else:
            lines.append("Plain commands will ask which system to use.")
    missing = [d.id for d in sandbox.installable(sid)]
    if missing and not only:
        lines.append("Not installed: " + ", ".join(missing) + "  (vm install <name>)")
    if not only:
        proot = sandbox.find_proot()
        lines.append("proot: " + (f"{sandbox.proot_version(proot)}" if proot else "will be fetched on install"))
    return "\n".join(lines)


def _list(sid, cols=80):
    """The catalog, wrapped to the screen width (phones are ~40 columns)."""
    cols = cols or 80
    have = set(sandbox.installed_vms(sid))
    lines = ["Systems you can install", "=======================", ""]
    for d in distros.CATALOG.values():
        mark = "  <- installed" if d.id in have else ""
        lines.append(f"{d.id:<8}{d.name} · {d.pkg} · {d.size}{mark}")
        lines.append(_wrap(d.summary, cols))
        lines.append(_wrap("release: " + d.release_help, cols))
        lines.append("")
    lines += ["Install one or several:", "  vm install debian", "  vm install ubuntu kali",
              "  vm install all", ""]
    lines.append(textwrap.fill(
        "You can keep as many as you like. Each has its own /root. "
        "Pick one with `vm use <name>` or `vm shell <name>`.", width=max(cols, 24)))
    return "\n".join(lines)


# ------------------------------------------------------------------ running
def _run(ctx, vm, cmd):
    """Run ``cmd`` inside ``vm`` and return its output."""
    sid = ctx["sid"]
    try:
        sp = sandbox.spec(sid, f"@vm:{vm} {cmd}", "/root")
        p = subprocess.run(sp.argv, cwd=sp.cwd, env=sp.env, capture_output=True,
                           text=True, timeout=RUN_TIMEOUT, stdin=subprocess.DEVNULL)
    except sandbox.SandboxError as e:
        return f"vm: {e}"
    except subprocess.TimeoutExpired:
        return (f"vm: timed out after {RUN_TIMEOUT}s. Long jobs belong in "
                "`vm shell` (no time limit) or run in the background with `&`.")
    except OSError as e:
        return f"vm: {e}"
    return ((p.stdout or "") + (p.stderr or "")).rstrip("\n")


def _start_setup(ctx, vm, profile):
    """Write the setup script into the VM and run it as a background job."""
    sid = ctx["sid"]
    try:
        sandbox.resolve(sid, f"vm:{vm}")           # authorizes + checks the VM is ready
        text = devsetup.script(profile, sandbox.family(sid, vm))
        path = sandbox.rootfs_path(sid, vm) / "tmp" / f"rconsole-setup-{profile}.sh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
        guest = f"/tmp/rconsole-setup-{profile}.sh"
        jid, _job = jobs.start_job(sid, f"@vm:{vm} {devsetup.quoted_path_cmd(guest)}", "/root")
    except sandbox.SandboxError as e:
        return f"vm setup: {e}"
    except OSError as e:
        return f"vm setup: could not prepare the script: {e}"
    return {"output": f"Setting up '{profile}' in {vm} as job %{jid} — live output below "
                      f"(it keeps going if you leave; `jobs`, `fg %{jid}`).",
            "jobtail": jid, "fg": True}


def _doctor(ctx, vm):
    """Run a battery of checks inside ``vm`` and report each with its timing."""
    sid = ctx["sid"]
    d = sandbox.describe(sid, vm)
    fam = d["family"] or "alpine"
    proot = sandbox.find_proot()
    lines = [f"Doctor: {vm} ({d['distro'] or d['label'] or '?'})", "=" * 24,
             f"proot   : {proot or 'MISSING'} (v{sandbox.proot_version(proot) if proot else '?'})",
             f"seccomp : {'off' if sandbox._seccomp_off() else 'on'}",
             f"kernel  : {platform.release()}  cpu: {platform.machine()}", ""]
    host = {"alpine": "dl-cdn.alpinelinux.org", "debian": "deb.debian.org"}.get(
        vm, "archive.ubuntu.com" if vm == "ubuntu" else "http.kali.org" if vm == "kali"
        else "deb.debian.org")
    if fam == "alpine":
        dns = f"nslookup {host} >/dev/null 2>&1 && echo ok"
        net = f"nc -w 6 {host} 80 </dev/null >/dev/null 2>&1 && echo ok"
        pkg = "apk --version"
    else:
        dns = f"getent hosts {host} | grep -q . && echo ok"
        net = f"timeout 8 bash -c 'exec 3<>/dev/tcp/{host}/80' && echo ok"
        pkg = "apt-get --version | head -1"
    steps = [
        ("start a shell", "echo ok", lambda o: o.strip() == "ok"),
        ("run programs (ls)", "ls / >/dev/null && echo ok", lambda o: o.strip() == "ok"),
        ("you are root", "id -u", lambda o: o.strip() == "0"),
        ("write to /root", "echo x > /root/.rc-doctor && cat /root/.rc-doctor && rm /root/.rc-doctor",
         lambda o: o.strip() == "x"),
        ("write to /tmp", "echo y > /tmp/.rc-doctor && rm /tmp/.rc-doctor && echo ok",
         lambda o: o.strip() == "ok"),
        (f"DNS ({host})", dns, lambda o: o.strip() == "ok"),
        (f"internet ({host}:80)", net, lambda o: o.strip() == "ok"),
        ("package manager", pkg, lambda o: bool(o.strip())),
    ]
    bad = 0
    for label, cmd, ok in steps:
        t0 = time.time()
        try:
            sp = sandbox.spec(sid, f"@vm:{vm} {cmd}", "/root")
            p = subprocess.run(sp.argv, cwd=sp.cwd, env=sp.env, capture_output=True,
                               text=True, timeout=25, stdin=subprocess.DEVNULL)
            out, err = p.stdout or "", (p.stderr or "").strip()
            good = p.returncode == 0 and ok(out)
        except subprocess.TimeoutExpired:
            out, err, good = "", "timed out", False
        except (sandbox.SandboxError, OSError) as e:
            out, err, good = "", str(e), False
        dt = time.time() - t0
        lines.append(f"{'✓' if good else '✗'} {label} ({dt:.1f}s)")
        if not good:
            bad += 1
            detail = (err or out).strip().splitlines()
            if detail:
                lines.append("    " + detail[0][:90])
    lines.append("")
    if not bad:
        lines.append("All checks passed.")
    else:
        lines.append(f"{bad} check(s) failed. Include this output when asking for help.")
        if any("unknown program" in l or "prooted" in l for l in lines):
            lines.append("Hint: that is a proot too old for this system. Update rconsole "
                         "(it fetches proot 5.5.0) or pick another system.")
    return "\n".join(lines)


# ------------------------------------------------------------------- command
@commands.command("vm")
def cmd_vm(ctx, args):
    sid = ctx["sid"]
    cols = ctx.get("cols")
    sub = args[0].lower() if args else "status"
    rest = args[1:]

    if sub in ("status", "info"):
        return _status(sid, cols, rest[0] if rest else None)

    if sub in ("help", "-h", "--help"):
        return "vm — your Linux systems\n" + _USAGE

    if sub in ("list", "ls", "distros", "images", "available", "options"):
        return _list(sid, cols)

    if sub == "install":
        choices = rest or [None]
        everything = any(c.lower() == "all" for c in choices)
        if everything:
            choices = list(distros.CATALOG)
        ok, msg = sandbox.install_many(sid, choices, skip_installed=everything)
        return f"vm: {msg}"

    if sub == "shell":
        vm, _rest, reply = _pick_vm(sid, rest, "vm shell", "vm shell {vm}")
        if reply:
            return reply
        try:
            sandbox.resolve(sid, f"vm:{vm}")
        except sandbox.SandboxError as e:
            return f"vm: {e}"
        # The PTY layer re-authorizes this on spawn; '@vm:<id>' pins the system.
        return {"pty": True, "cmd": f"@vm:{vm}"}

    if sub == "run":
        rest = list(rest)
        named = None
        if rest[:1] in (["--vm"], ["-n"]) and len(rest) >= 2:
            named, rest = rest[1], rest[2:]
        if not rest:
            return "usage: vm run [--vm <name>] <command>"
        if named:
            if named not in sandbox.installed_vms(sid):
                return f"vm: '{named}' is not installed (see `vm`)."
            vm = named
        else:
            vm, _r, reply = _pick_vm(sid, [], "vm run", "vm run --vm {vm} " + " ".join(rest))
            if reply:
                return reply
        return _run(ctx, vm, " ".join(rest))

    if sub == "use":
        if not rest:
            return "usage: vm use <name>   (see `vm` for your systems)"
        ok, msg = sandbox.set_mode(sid, sandbox.VM, rest[0])
        if not ok:
            return f"vm: {msg}"
        sandbox.reset_cwds(sid)
        return f"Using {rest[0]}: plain commands now run there. (tab directories reset)"

    if sub == "mode":
        if not rest:
            cur = sandbox.current_target(sid)
            return f"mode: {sandbox.effective_mode(sid)}" + (f" ({cur})" if cur else " (choose a system)")
        want = rest[0].lower()
        name = rest[1] if len(rest) > 1 else None
        if want == sandbox.VM and not sandbox.installed(sid):
            return _no_vm_reply(sid)
        ok, msg = sandbox.set_mode(sid, want, name)
        if not ok:
            if want == sandbox.VM and "several systems" in (msg or ""):
                return {"output": "Which system should plain commands run in?",
                        "choice": sandbox.choice(sid, install=False)}
            return f"vm: {msg}"
        sandbox.reset_cwds(sid)
        cur = sandbox.current_target(sid)
        where = "host" if cur == sandbox.HOST else f"vm ({cur[3:]})" if cur else want
        return f"mode: {where} — plain commands run there now. (tab directories reset)"

    if sub in ("reset", "remove"):
        yes = "--yes" in rest
        names = [r for r in rest if r != "--yes"]
        vms = sandbox.installed_vms(sid)
        if sub == "remove" and names[:1] == ["all"]:
            targets = vms
        elif names:
            targets = [n for n in names if n in vms]
            missing = [n for n in names if n not in vms]
            if missing:
                return f"vm: not installed: {', '.join(missing)} (see `vm`)."
        elif len(vms) == 1:
            targets = vms
        elif not vms:
            return "vm: you have no systems."
        else:
            return (f"vm: which one? Name it: vm {sub} <name> --yes   (yours: {', '.join(vms)})")
        what = ("system files (your /root is kept)" if sub == "reset"
                else "the system AND everything in its /root")
        if not yes:
            return (f"This deletes {what} of: {', '.join(targets)}.\n"
                    f"Re-run with --yes to confirm: vm {sub} {' '.join(names)} --yes".replace("  ", " "))
        for vm in targets:
            sandbox.reset(sid, vm, keep_home=(sub == "reset"))
        if sandbox.effective_mode(sid) == sandbox.VM and not sandbox.installed(sid) \
                and sandbox.host_allowed(sid):
            sandbox.set_mode(sid, sandbox.HOST)       # don't strand commands without a VM
        sandbox.reset_cwds(sid)
        verb = "removed" if sub == "remove" else "reset (run `vm install <name>` to recreate)"
        return f"vm: {', '.join(targets)} {verb}."

    if sub in ("setup", "dev", "tools"):
        if not rest or rest[0] in ("list", "help", "-h"):
            return ("Install a ready-made toolchain inside a VM:\n\n" + devsetup.describe() +
                    "\n\nUsage: vm setup <profile> [name]      e.g. vm setup python\n"
                    "It runs as a background job and shows progress; `vm setup python` is what "
                    "you need for pip packages such as psycopg2, Pillow, lxml or mysqlclient.")
        profile = rest[0].lower()
        if profile not in devsetup.PROFILES:
            return (f"vm setup: unknown profile '{profile}'.\n\n" + devsetup.describe())
        vm, _r, reply = _pick_vm(sid, rest[1:], f"vm setup {profile}", f"vm setup {profile} {{vm}}")
        if reply:
            return reply
        return _start_setup(ctx, vm, profile)

    if sub in ("doctor", "check", "diagnose"):
        vms = sandbox.installed_vms(sid)
        name = rest[0] if rest else sandbox.active_vm(sid)
        if not vms:
            return _no_vm_reply(sid)
        if not name:
            return _picker(sid, "Which system should I check?", "vm doctor {vm}", install=False)
        if name not in vms:
            return f"vm: '{name}' is not installed (see `vm`)."
        return _doctor(ctx, name)

    return f"vm: unknown subcommand '{sub}'\n{_USAGE}"
