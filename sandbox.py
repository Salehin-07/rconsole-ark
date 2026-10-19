"""Execution targets for rconsole: the real host, or one of the user's VMs.

Every place that runs a command (line passthrough, background jobs, PTY
sessions, hosted apps, the AI agent) and every place that touches files (file
APIs, editor, file manager) goes through this module. It is the **single
choke point for authorization**, so a client can never reach the host by
bypassing the command interpreter (e.g. by opening the terminal websocket with
an arbitrary command).

Targets
-------
``host``      The machine rconsole runs on, with the privileges of the server
              process. Admin-only (``RCONSOLE_ALLOW_USER_HOST=1`` relaxes it).
``vm:<id>``   One of the user's **virtual machines** — a Linux root filesystem
              (alpine, debian, ubuntu, kali, ...) run under
              `proot <https://proot-me.github.io/>`_. Inside it the user is
              ``root`` and may install any package — no host privileges
              needed, so it works on locked-down platforms such as Render.

A user can have **several VMs at once** (one per system). Each has its own root
filesystem, its own ``/root`` home and its own packages; they share nothing.
Layout::

    <data>/vm/<user>/<id>/{rootfs/, home/, distro.json, .ready}

Which VM plain commands use is the user's *active* VM. With exactly one VM it is
chosen automatically; with several, the user picks (:class:`NeedVmChoice` makes
the client show a picker) and the choice is remembered.

The VM is a user-space compatibility layer, **not a hard security boundary**:
guest processes share the server's PID namespace and uid. Only create accounts
for people you trust; use one container per tenant for hostile multi-tenancy.
For that reason most of ``/proc`` is not exposed to non-admin guests.

Command prefix
--------------
A command string may start with ``@host``, ``@vm`` (the active VM) or
``@vm:<id>`` (a specific VM) to force a target (used by ``vm shell``); the prefix
is authorized exactly like the mode itself.
"""
import functools
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

import auth
import config
import distros
import state

HOST = "host"
VM = "vm"

ROOTFS_DIRNAME = "rootfs"
HOME_DIRNAME = "home"
READY_MARKER = ".ready"
VM_HOME = "/root"

# Static proot build published by the proot project (x86_64 only). On x86-64 we
# always prefer this exact, tested build over whatever a distro packages: older
# distro proots (e.g. Ubuntu's 5.1.0) lack flags and handle new glibc poorly.
PROOT_URL = "https://github.com/proot-me/proot/releases/download/v5.5.0/proot"
PROOT_SHA256 = "db185300a9daabbce86aca7632d8c7fdc4249f6f153171954013b70c5f491ec0"

MAX_DOWNLOAD = 400 * 1024 * 1024
DISTRO_FILE = "distro.json"
SAFE_PROC_FILES = ("uptime", "meminfo", "cpuinfo", "loadavg", "version")
CACHE_KEEP = 4                      # downloaded rootfs tarballs kept for reuse
TOOLS_SRC = config.HERE / "vmtools" / "rfetch.sh"
_VM_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z")


class SandboxError(Exception):
    """A command or path could not be mapped onto a target."""


class PermissionDenied(SandboxError):
    """The user is not allowed to use the requested target."""


class NeedVmChoice(Exception):
    """The user must choose which system to use (or install one) first.

    Deliberately *not* a :class:`SandboxError`: commands that swallow
    SandboxError to print a message let this one through, and the interpreter
    turns it into a picker (dropdown) in the browser instead of a dead-end error.
    """

    def __init__(self, vms=(), empty=False):
        self.vms = list(vms)
        self.empty = empty
        super().__init__("no VM installed yet" if empty else
                         "several systems are installed — choose one")


@dataclass
class Spec:
    """How to launch a process: argv + working directory + environment."""
    argv: list
    cwd: str
    env: dict
    mode: str = HOST
    vm: str = ""                          # which VM when mode == VM
    note: str = field(default="", repr=False)


@dataclass(frozen=True)
class Target:
    mode: str
    vm: str = ""


# --------------------------------------------------------------------- helpers
def _safe(sid):
    """Filesystem-safe directory name for a username."""
    s = re.sub(r"[^A-Za-z0-9._-]", "_", str(sid))
    return "_" + s if s.startswith(".") or not s else s


def user_dir(sid):
    """Directory holding all of one user's VMs."""
    return config.VM_DIR / _safe(sid)


def _valid_vm_id(vm):
    return bool(vm and _VM_ID_RE.match(vm))


def vm_dir(sid, vm):
    if not _valid_vm_id(vm):
        raise SandboxError(f"bad VM name '{vm}'")
    return user_dir(sid) / vm


def rootfs_path(sid, vm=None):
    return vm_dir(sid, vm or _any_vm(sid)) / ROOTFS_DIRNAME


def home_path(sid, vm=None):
    return vm_dir(sid, vm or _any_vm(sid)) / HOME_DIRNAME


def supported():
    """The VM needs Linux (proot uses ptrace)."""
    return config.VM_ENABLED and sys.platform.startswith("linux")


def is_admin(sid):
    return auth.is_admin(sid)


# ----------------------------------------------------------- which VMs exist
_migrated = set()
_migrate_lock = threading.Lock()


def _migrate_legacy(sid):
    """Move a pre-multi-VM install (``<user>/rootfs``) into ``<user>/<id>/``."""
    with _migrate_lock:
        if sid in _migrated:
            return
        _migrated.add(sid)
        base = user_dir(sid)
        if not (base / ROOTFS_DIRNAME).is_dir():
            return
        try:
            meta = json.loads((base / DISTRO_FILE).read_text())
            vm = meta.get("id") or "alpine"
        except (OSError, ValueError):
            vm = "alpine"
        if not _valid_vm_id(vm):
            vm = "alpine"
        dest = base / vm
        if dest.exists():
            return
        dest.mkdir(parents=True)
        for name in (ROOTFS_DIRNAME, HOME_DIRNAME, DISTRO_FILE, READY_MARKER):
            if (base / name).exists() or (base / name).is_symlink():
                os.replace(base / name, dest / name)


def installed_vms(sid):
    """Ids of the user's installed VMs, oldest first."""
    _migrate_legacy(sid)
    base = user_dir(sid)
    found = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    for d in entries:
        if d.is_dir() and _valid_vm_id(d.name) and (d / READY_MARKER).exists() \
                and (d / ROOTFS_DIRNAME).is_dir():
            try:
                stamp = json.loads((d / DISTRO_FILE).read_text()).get("installed_at", 0)
            except (OSError, ValueError):
                stamp = int((d / READY_MARKER).stat().st_mtime)
            found.append((stamp, d.name))
    return [name for _, name in sorted(found)]


def installed(sid, vm=None):
    """Is ``vm`` installed (or, with no argument, is *any* VM installed)?"""
    vms = installed_vms(sid)
    return (vm in vms) if vm else bool(vms)


def _any_vm(sid):
    """A VM to use when the caller did not name one: active, else the first."""
    vm = active_vm(sid) or (installed_vms(sid) or [None])[0]
    if not vm:
        raise SandboxError("no VM installed yet")
    return vm


def active_vm(sid):
    """The VM plain commands use, or ``None`` if the user has to choose."""
    vms = installed_vms(sid)
    pref = state.get_vm(sid)
    if pref in vms:
        return pref
    return vms[0] if len(vms) == 1 else None


# ----------------------------------------------------------------------- modes
def host_allowed(sid):
    return is_admin(sid) or config.ALLOW_USER_HOST


def effective_mode(sid):
    """Return the target kind (``host``/``vm``) commands for ``sid`` will use."""
    if not host_allowed(sid):
        return VM
    pref = state.get_mode(sid)
    if pref in (HOST, VM):
        return pref
    return config.DEFAULT_ADMIN_MODE if is_admin(sid) else VM


def current_target(sid):
    """``"host"``, ``"vm:<id>"``, or ``None`` when a choice is pending."""
    if effective_mode(sid) == HOST:
        return HOST
    vm = active_vm(sid)
    return f"vm:{vm}" if vm else None


def set_mode(sid, mode, vm=None):
    """Persist the preferred target. Returns ``(ok, message)``."""
    mode = (mode or "").lower()
    if mode not in (HOST, VM):
        return False, "mode must be 'host' or 'vm'"
    if mode == HOST and not host_allowed(sid):
        return False, "host access is restricted to admins"
    if mode == VM:
        if not supported():
            return False, "the VM is unavailable (needs Linux and RCONSOLE_VM=1)"
        vms = installed_vms(sid)
        if vm:
            if vm not in vms:
                have = ", ".join(vms) or "none"
                return False, f"'{vm}' is not installed (installed: {have}). Try `vm list`."
            state.set_vm(sid, vm)
        elif len(vms) > 1 and state.get_vm(sid) not in vms:
            return False, ("several systems are installed: choose one, e.g. "
                           f"`vm mode vm {vms[0]}` (installed: {', '.join(vms)}).")
    state.set_mode(sid, mode)
    return True, None


def set_target(sid, target):
    """Switch to ``"host"`` or ``"vm:<id>"`` (used by the browser dropdown)."""
    if target == HOST:
        return set_mode(sid, HOST)
    if target and target.startswith("vm:"):
        return set_mode(sid, VM, target[3:])
    return False, "target must be 'host' or 'vm:<name>'"


def target_label(sid):
    """Short name shown in the prompt: ``rconsole`` for the host, else the VM."""
    cur = current_target(sid)
    if cur is None:
        return "choose-vm"
    return "rconsole" if cur == HOST else cur[3:]


def default_cwd(sid, mode=None):
    mode = mode or effective_mode(sid)
    return VM_HOME if mode == VM else state.default_cwd()


def reset_cwds(sid):
    """After a target switch, put every tab back in a valid directory."""
    sess = state.get_session(sid)
    if not sess:
        return
    cwd = default_cwd(sid)
    with state.STATE_LOCK:
        for tab in sess["tabs"].values():
            tab["cwd"] = cwd
    state.save_now(sid)


# --------------------------------------------------------------- authorization
_TARGET_RE = re.compile(r"^@(host|vm(?::[A-Za-z0-9._-]{1,32})?)(?:\s+(.*))?$", re.S)


def parse_target(cmd):
    """Split a leading ``@host`` / ``@vm`` / ``@vm:<id>`` marker.

    Returns ``(forced, rest)``; ``forced`` is ``None``, ``"host"``, ``"vm"`` or
    ``"vm:<id>"``.
    """
    cmd = cmd or ""
    m = _TARGET_RE.match(cmd)
    if not m:
        return None, cmd
    return m.group(1), (m.group(2) or "").lstrip()


def resolve(sid, forced=None):
    """Decide the :class:`Target` for a launch and enforce authorization.

    Raises :class:`PermissionDenied`, :class:`SandboxError`, or
    :class:`NeedVmChoice` (the user must pick a system first).
    """
    vm = None
    if forced and forced.startswith("vm:"):
        mode, vm = VM, forced[3:]
    else:
        mode = forced or effective_mode(sid)
    if mode == HOST:
        if not host_allowed(sid):
            raise PermissionDenied(
                "host access is restricted to admins. "
                "Use your VM instead: `vm install`, then `vm shell`.")
        return Target(HOST)
    _require_supported()
    vms = installed_vms(sid)
    if vm is None:
        vm = active_vm(sid)
        if vm is None:
            if not vms and installing(sid):
                raise SandboxError("your VM is still installing; check `vm status`.")
            raise NeedVmChoice(vms, empty=not vms)
    elif vm not in vms:
        have = ", ".join(vms) or "none"
        raise SandboxError(f"VM '{vm}' is not installed (installed: {have}). See `vm list`.")
    if install_state(sid, vm)["state"] == "installing":
        raise SandboxError(f"{vm} is still installing; check `vm status`.")
    _ensure_pinned_proot()
    if not find_proot():
        raise SandboxError("proot is missing. Run `vm install` to fetch it.")
    sync_guest_tools(sid, vm)
    return Target(VM, vm)


def resolve_target(sid, forced=None):
    """Like :func:`resolve` but returns just the kind (``host``/``vm``)."""
    return resolve(sid, forced).mode


def _ensure_pinned_proot():
    """Existing deployments may only have a distro proot, which is too old for
    newer systems (Ubuntu 26.04's Rust coreutils fail under it). Fetch the pinned
    build once; fall back to whatever proot exists if the download fails."""
    if distros.machine() in ("x86_64", "amd64") and not os.environ.get("RCONSOLE_VM_PROOT") \
            and not _pinned_proot().is_file():
        try:
            ensure_proot()
        except SandboxError:
            pass


def _require_supported():
    if not supported():
        raise SandboxError("the VM is unavailable on this server "
                           "(needs Linux and RCONSOLE_VM=1).")


# ----------------------------------------------------------------- proot setup
_proot_lock = threading.Lock()
_no_seccomp = False
_NO_SECCOMP_MARKER = config.VM_DIR / ".no-seccomp"


def _seccomp_off():
    """proot's seccomp acceleration is disabled (probe found it unsupported).

    Remembered on disk, so a restart keeps using the setting that worked."""
    return _no_seccomp or bool(os.environ.get("PROOT_NO_SECCOMP")) \
        or _NO_SECCOMP_MARKER.exists()


def _pinned_proot():
    return config.VM_DIR / "bin" / "proot"


def find_proot():
    """The proot to run: explicit override, then our pinned build, then the system's."""
    cands = [os.environ.get("RCONSOLE_VM_PROOT"), str(_pinned_proot())]
    if distros.machine() not in ("x86_64", "amd64"):
        cands.append(shutil.which("proot"))
    else:
        cands.append(shutil.which("proot"))   # last resort until the pinned one is fetched
    for cand in cands:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


@functools.lru_cache(maxsize=4)
def proot_version(proot):
    """Best-effort version string of a proot binary (for `vm doctor`)."""
    import subprocess
    try:
        p = subprocess.run([proot, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    text = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"\bv?(\d+\.\d+(?:\.\d+)?)", text)
    return m.group(1) if m else "unknown"


@functools.lru_cache(maxsize=8)
def proot_supports(proot, flag):
    """Does this proot build know ``flag``? (Distro packages can be old: Ubuntu
    24.04 ships 5.1.0, which lacks ``--kill-on-exit``.)"""
    import subprocess
    try:
        p = subprocess.run([proot, "--help"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return flag in (p.stdout or "") + (p.stderr or "")


def _download(url, dest, sha256=None, progress=None):
    """Stream ``url`` to ``dest`` (atomically), verifying ``sha256`` if given."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".part{os.getpid()}")
    h = hashlib.sha256()
    try:
        with requests.get(url, stream=True, timeout=(10, 60)) as r:
            r.raise_for_status()
            total = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD:
                        raise SandboxError("download too large; aborting")
                    h.update(chunk)
                    f.write(chunk)
                    if progress:
                        progress(total)
        if sha256 and h.hexdigest() != sha256.lower():
            raise SandboxError(
                f"checksum mismatch for {url} (expected {sha256[:12]}…, "
                f"got {h.hexdigest()[:12]}…)")
        os.replace(tmp, dest)
    except requests.RequestException as e:
        raise SandboxError(f"download failed: {e}") from e
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return dest


def ensure_proot():
    """Return a usable proot path, downloading the pinned static build if needed.

    On x86-64 the pinned, tested build is preferred over a distro package; on
    other CPUs a system ``proot`` (``apt/apk install proot``) must be present.
    """
    with _proot_lock:
        override = os.environ.get("RCONSOLE_VM_PROOT")
        if override and os.access(override, os.X_OK):
            return override
        if distros.machine() not in ("x86_64", "amd64"):
            found = find_proot()
            if found:
                return found
            raise SandboxError(
                f"no prebuilt proot for {os.uname().machine}. Install it with "
                "your package manager (apt/apk install proot) or set "
                "RCONSOLE_VM_PROOT.")
        dest = _pinned_proot()
        if not (dest.is_file() and os.access(dest, os.X_OK)):
            _download(PROOT_URL, dest, PROOT_SHA256)
            os.chmod(dest, 0o755)
        return str(dest)


def parse_alpine_release(text, flavor="alpine-minirootfs"):
    """Pick ``(file, sha256)`` for ``flavor`` from Alpine's latest-releases.yaml."""
    try:
        return distros.parse_alpine_release(text, flavor)
    except distros.DistroError as e:
        raise SandboxError(str(e)) from e


def _prune_cache(cache, keep):
    """Keep the newest few tarballs (and ``keep``); older ones just waste disk."""
    files = sorted((p for p in cache.glob("*") if p.is_file() and ".part" not in p.name),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[CACHE_KEEP:]:
        if p != keep:
            p.unlink(missing_ok=True)


def _fetch_rootfs(distro, release, progress=None):
    """Return ``(tarball_path, label)`` for a verified root filesystem (cached).

    ``RCONSOLE_VM_ROOTFS_URL`` (+ optional ``RCONSOLE_VM_ROOTFS_SHA256``)
    overrides the catalog, e.g. for air-gapped mirrors and tests.
    """
    cache = config.VM_DIR / "cache"
    override = os.environ.get("RCONSOLE_VM_ROOTFS_URL")
    if override:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", override.rsplit("/", 1)[-1]) or "rootfs.tar.gz"
        dest = cache / name
        want = os.environ.get("RCONSOLE_VM_ROOTFS_SHA256")
        if dest.exists() and (not want or _sha256_file(dest) == want.lower()):
            return dest, distro.name
        return _download(override, dest, want, progress), distro.name
    try:
        src = distros.resolve(distro, release)
    except distros.DistroError as e:
        raise SandboxError(str(e)) from e
    dest = cache / src.filename
    if not (dest.exists() and _sha256_file(dest) == src.sha256):
        _download(src.url, dest, src.sha256, progress)
    _prune_cache(cache, keep=dest)
    return dest, src.label


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_filter(member, dest):
    """tarfile filter: safe paths, no device nodes, always owner-writable."""
    member = tarfile.tar_filter(member, dest)
    if member is None or member.isdev():
        return None
    if member.isdir():
        return member.replace(mode=member.mode | 0o700)
    if member.isfile():
        return member.replace(mode=member.mode | 0o600)
    return member


def _flatten_single_dir(target):
    """Some tarballs (Kali NetHunter) wrap the system in one top-level folder."""
    kids = list(Path(target).iterdir())
    if len(kids) == 1 and kids[0].is_dir() and not (Path(target) / "etc").exists():
        inner = kids[0]
        for item in list(inner.iterdir()):
            os.replace(item, Path(target) / item.name)
        inner.rmdir()


def _extract(tarball, target):
    target = Path(target)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    with tarfile.open(tarball) as tf:
        tf.extractall(target, filter=_extract_filter)
    _flatten_single_dir(target)


# ------------------------------------------------------------------ installing
_installs = {}                   # (sid, vm) -> {"state": ..., "message": ...}
_installs_lock = threading.Lock()


def install_state(sid, vm):
    """State of one VM: ``installing`` / ``failed`` / ``ready`` / ``absent``."""
    with _installs_lock:
        rec = dict(_installs.get((sid, vm)) or {})
    if rec.get("state") in ("installing", "failed"):
        return rec
    return {"state": "ready" if installed(sid, vm) else "absent", "message": ""}


def installing(sid):
    """Names of this user's VMs that are being installed right now."""
    with _installs_lock:
        return [vm for (s, vm), rec in _installs.items()
                if s == sid and rec.get("state") == "installing"]


def _set_install(sid, vm, st, msg=""):
    with _installs_lock:
        _installs[(sid, vm)] = {"state": st, "message": msg}


def _clear_install(sid, vm):
    with _installs_lock:
        _installs.pop((sid, vm), None)


def distro_info(sid, vm=None):
    """What a VM was built from (``None`` if it is not installed)."""
    vm = vm or active_vm(sid) or (installed_vms(sid) or [None])[0]
    if not vm:
        return None
    try:
        return json.loads((vm_dir(sid, vm) / DISTRO_FILE).read_text())
    except (OSError, ValueError):
        pass
    if installed(sid, vm):
        d = distros.CATALOG.get(vm)
        return {"id": vm, "release": "", "family": d.family if d else "alpine",
                "label": d.name if d else vm}
    return None


def family(sid, vm=None):
    """Package-manager family of a VM: ``alpine`` or ``debian``."""
    return (distro_info(sid, vm) or {}).get("family", "alpine")


def install(sid, choice=None, wait=False):
    """Provision one VM from ``choice`` (``"debian:bookworm"`` etc.)."""
    return install_many(sid, [choice], wait=wait)


def install_many(sid, choices, wait=False, skip_installed=False):
    """Install several systems one after another in a background thread.

    Returns ``(started, message)``. Everything is validated up front so typos
    and duplicates are reported immediately instead of failing later. With
    ``skip_installed`` (used for ``vm install all``) systems you already have are
    left alone instead of being an error.
    """
    if not supported():
        return False, "the VM is unavailable (needs Linux and RCONSOLE_VM=1)."
    plan, seen = [], set()
    for choice in choices:
        try:
            distro, release = distros.parse_choice(choice)
        except distros.DistroError as e:
            return False, str(e)
        if distro.id in seen:
            continue
        seen.add(distro.id)
        if installed(sid, distro.id):
            if skip_installed:
                continue
            return False, (f"{distro.name} is already installed. Use `vm remove "
                           f"{distro.id} --yes` first if you want a fresh one.")
        plan.append((distro, release))
    if not plan:
        return False, "everything you asked for is already installed." if skip_installed \
            else "nothing to install."
    busy = installing(sid)
    if busy:
        return False, f"already installing {', '.join(busy)}; check `vm status`."
    for distro, _ in plan:
        _set_install(sid, distro.id, "installing", "queued")
    t = threading.Thread(target=_install_worker, args=(sid, plan), daemon=True)
    t.start()
    if wait:
        t.join()
        bad = [d.id for d, _ in plan if install_state(sid, d.id)["state"] != "ready"]
        if bad:
            return False, install_state(sid, bad[0])["message"] or "failed"
        return True, "ready"
    names = ", ".join(f"{d.name} ({r})" for d, r in plan)
    return True, f"installing {names} in the background; check `vm status`."


def _fix_resolv_conf(rootfs):
    """Make ``etc/resolv.conf`` a plain file.

    Debian-family images ship it as a symlink into /run/systemd (dangling in a
    VM); a regular file keeps the host-DNS bind mount and any tool that rewrites
    the file working.
    """
    conf = Path(rootfs) / "etc" / "resolv.conf"
    try:
        if conf.is_symlink() or not conf.exists():
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.unlink(missing_ok=True)
            host = ""
            try:
                host = Path("/etc/resolv.conf").read_text()
            except OSError:
                pass
            conf.write_text(host if "nameserver" in host else "nameserver 1.1.1.1\nnameserver 8.8.8.8\n")
    except OSError:
        pass


def _seed_home(rootfs, home, vm):
    """Give a fresh ``/root`` the distro's dotfiles (the bind hides rootfs/root).

    Also gives bash a prompt that says *which* VM you are in.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    skel = Path(rootfs) / "root"
    try:
        if skel.is_dir() and not any(home.iterdir()):
            for item in skel.iterdir():
                if item.is_file() and not item.is_symlink():
                    shutil.copy2(item, home / item.name)
    except OSError:
        pass
    rc = home / ".bashrc"
    marker = "# rconsole prompt"
    try:
        text = rc.read_text() if rc.exists() else ""
        if marker not in text:
            with open(rc, "a") as f:
                f.write(f"\n{marker}\n"
                        f"PS1='\\[\\e[1;32m\\]\\u@{vm}\\[\\e[0m\\]:\\[\\e[1;34m\\]\\w\\[\\e[0m\\]\\$ '\n")
        prof = home / ".profile"
        ptext = prof.read_text() if prof.exists() else ""
        if ".bashrc" not in ptext:
            with open(prof, "a") as f:
                f.write('\n[ -n "$BASH_VERSION" ] && [ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"\n')
    except OSError:
        pass


def _post_setup(rootfs, distro):
    """Small per-family tweaks so the system behaves under proot."""
    rootfs = Path(rootfs)
    for d in ("dev", "proc", "tmp", "root", "etc", "usr/local/bin"):
        (rootfs / d).mkdir(parents=True, exist_ok=True)
    try:
        (rootfs / "tmp").chmod(0o1777)
    except OSError:
        pass
    _fix_resolv_conf(rootfs)
    if distro.family == "debian":
        # apt normally drops to an unprivileged `_apt` user, which fake-root
        # proot cannot do; services must not try to start during installs; and
        # `apt` (as opposed to apt-get) nags "does not have a stable CLI
        # interface" whenever its output is not a terminal (e.g. `vm run`).
        conf = rootfs / "etc/apt/apt.conf.d"
        conf.mkdir(parents=True, exist_ok=True)
        (conf / "99rconsole").write_text(
            'APT::Sandbox::User "root";\n'
            'APT::Cmd::Disable-Script-Warning "true";\n'
            'Acquire::Retries "3";\n'
            'Dpkg::Options { "--force-unsafe-io"; };\n')
        policy = rootfs / "usr/sbin/policy-rc.d"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text("#!/bin/sh\nexit 101\n")
        policy.chmod(0o755)


def sync_guest_tools(sid, vm=None):
    """Install the ``neofetch``/``fetch``/``rfetch`` shim into a VM.

    Lives in ``/usr/local/bin`` so it works in every distro and takes priority
    over a real neofetch installed later (which stays at /usr/bin/neofetch).
    """
    try:
        dest_dir = rootfs_path(sid, vm) / "usr/local/bin"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "rfetch"
        if not dest.exists() or dest.stat().st_size != TOOLS_SRC.stat().st_size:
            shutil.copyfile(TOOLS_SRC, dest)
            dest.chmod(0o755)
        for alias in ("neofetch", "fetch"):
            link = dest_dir / alias
            if not link.exists() and not link.is_symlink():
                link.symlink_to("rfetch")
    except (OSError, SandboxError):
        pass   # cosmetic: never block the VM on this


def _install_worker(sid, plan):
    """Install each ``(distro, release)`` in ``plan``; a failure doesn't stop the rest."""
    for distro, release in plan:
        _install_one(sid, distro, release)


def _install_one(sid, distro, release):
    global _no_seccomp
    vm = distro.id
    base = vm_dir(sid, vm)
    try:
        config.ensure_private_dir(config.VM_DIR)
        config.ensure_private_dir(user_dir(sid))
        _set_install(sid, vm, "installing", "fetching proot")
        ensure_proot()
        _set_install(sid, vm, "installing", f"downloading {distro.name}")
        tarball, label = _fetch_rootfs(distro, release)
        _set_install(sid, vm, "installing", "extracting")
        config.ensure_private_dir(base)
        tmp_root = base / (ROOTFS_DIRNAME + ".new")
        _extract(tarball, tmp_root)
        final = rootfs_path(sid, vm)
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp_root, final)
        (config.VM_DIR / "tmp").mkdir(parents=True, exist_ok=True)
        _post_setup(final, distro)
        _seed_home(final, home_path(sid, vm), vm)
        sync_guest_tools(sid, vm)
        _set_install(sid, vm, "installing", "testing proot")
        err = _probe(sid, vm)
        if err:
            _no_seccomp = True            # retry without seccomp acceleration
            err = _probe(sid, vm)
            if not err:
                _NO_SECCOMP_MARKER.write_text("1")
        if err:
            shutil.rmtree(final, ignore_errors=True)
            raise SandboxError(
                "proot cannot run on this host (ptrace may be blocked): " + err)
        (base / DISTRO_FILE).write_text(json.dumps({
            "id": vm, "release": release, "family": distro.family,
            "label": label, "installed_at": int(time.time())}))
        (base / READY_MARKER).write_text(str(int(time.time())))
        _set_install(sid, vm, "ready", "")
        _clear_install(sid, vm)
    except Exception as e:  # surfaced through `vm status`
        shutil.rmtree(base / (ROOTFS_DIRNAME + ".new"), ignore_errors=True)
        _set_install(sid, vm, "failed", str(e))


def _probe(sid, vm):
    """Run ``echo ok`` inside the VM. Returns an error string, or ``None``."""
    import subprocess
    # `ls` is a real external program on every system (on Ubuntu 26.04 it is the
    # Rust coreutils multi-call binary, which old proots cannot run).
    argv = _proot_prefix(sid, vm, VM_HOME, admin=True) + [
        "/bin/sh", "-c", "echo ok; ls / >/dev/null || exit 3"]
    try:
        p = subprocess.run(argv, env=_vm_env(sid, vm, probe=True), capture_output=True,
                           text=True, timeout=30, cwd=str(config.VM_DIR))
    except Exception as e:
        return str(e)
    if p.returncode != 0 or "ok" not in p.stdout:
        msg = (p.stderr or p.stdout or f"exit {p.returncode}").strip()[:300]
        if "unknown program" in msg or "prooted" in msg:
            msg += (f" — this proot ({proot_version(find_proot())}) is too old for this "
                    "system; rconsole needs proot 5.5.0+ (it downloads one on x86-64; "
                    "elsewhere install a newer proot or try an older release such as "
                    "`ubuntu:24.04`).")
        return msg
    return None


def reset(sid, vm=None, keep_home=True):
    """Delete a VM's root filesystem (and optionally its home dir).

    With ``keep_home=False`` the whole VM directory goes. ``vm=None`` means
    *every* VM of this user (used when deleting an account).
    """
    if vm is None:
        _migrate_legacy(sid)
        names = [d.name for d in user_dir(sid).iterdir() if d.is_dir()] \
            if user_dir(sid).exists() else []
        if not names:
            return False
        for name in names:
            reset(sid, name, keep_home)
        if not keep_home:
            shutil.rmtree(user_dir(sid), ignore_errors=True)
        return True
    base = vm_dir(sid, vm)
    if not base.exists():
        return False
    (base / READY_MARKER).unlink(missing_ok=True)
    (base / DISTRO_FILE).unlink(missing_ok=True)
    shutil.rmtree(rootfs_path(sid, vm), ignore_errors=True)
    shutil.rmtree(base / (ROOTFS_DIRNAME + ".new"), ignore_errors=True)
    if not keep_home:
        shutil.rmtree(base, ignore_errors=True)
    _clear_install(sid, vm)
    if state.get_vm(sid) == vm:
        state.set_vm(sid, None)
    return True


def rename_user(old, new):
    """Move a user's VM directory when the account is renamed."""
    src, dst = user_dir(old), user_dir(new)
    if src.exists() and not dst.exists():
        os.replace(src, dst)
    with _installs_lock:
        for key in [k for k in _installs if k[0] == old]:
            _installs.pop(key, None)
    _migrated.discard(old)


def disk_usage(sid, vm=None):
    total = 0
    root = vm_dir(sid, vm) if vm else user_dir(sid)
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def describe(sid, vm=None):
    """Human-readable facts about one VM (``vm=None``: the active/first one)."""
    vm = vm or active_vm(sid) or (installed_vms(sid) or [None])[0]
    st = install_state(sid, vm) if vm else {"state": "absent", "message": ""}
    info = {
        "supported": supported(), "vm": vm, "state": st["state"], "message": st["message"],
        "mode": effective_mode(sid), "proot": find_proot(),
        "host_allowed": host_allowed(sid), "distro": None, "disk": None,
        "family": None, "distro_id": vm, "release": None, "label": None,
        "active": bool(vm and vm == active_vm(sid)),
    }
    if vm and installed(sid, vm):
        meta = distro_info(sid, vm) or {}
        info.update(release=meta.get("release"), family=meta.get("family"),
                    label=meta.get("label"))
        try:
            text = (rootfs_path(sid, vm) / "etc" / "os-release").read_text()
            m = re.search(r'PRETTY_NAME="?([^"\n]+)', text)
            info["distro"] = m.group(1) if m else meta.get("label")
        except OSError:
            info["distro"] = meta.get("label")
        info["disk"] = disk_usage(sid, vm)
    return info


def describe_all(sid):
    """:func:`describe` for every VM that exists or is being installed."""
    names = list(installed_vms(sid))
    with _installs_lock:
        extra = [vm for (s, vm) in _installs if s == sid and vm not in names]
    return [describe(sid, vm) for vm in names + sorted(extra)]


# ------------------------------------------------------------------ UI helpers
def installable(sid):
    """Catalog entries this user has not installed yet."""
    have = set(installed_vms(sid))
    return [d for d in distros.CATALOG.values() if d.id not in have]


def targets(sid):
    """Everything the browser's target dropdown needs."""
    items = []
    if host_allowed(sid):
        items.append({"id": HOST, "label": "Host (server)", "kind": "host"})
    for vm in installed_vms(sid):
        meta = distro_info(sid, vm) or {}
        items.append({"id": f"vm:{vm}", "label": meta.get("label") or vm,
                      "kind": "vm", "family": meta.get("family")})
    return {
        "current": current_target(sid),
        "label": target_label(sid),
        "targets": items,
        "installing": installing(sid),
        "installable": [{"id": d.id, "name": d.name, "size": d.size}
                        for d in installable(sid)],
        "supported": supported(),
    }


def choice(sid, then=None, template=None, install=True):
    """Picker payload for the browser: pick a system, then run commands.

    * ``then`` — the command that triggered the question; after picking, the
      system becomes the user's default (``vm use <id>``) and ``then`` is re-run.
    * ``template`` — instead, run ``template.format(vm=<id>)`` for the picked
      system without changing the default (``"vm shell {vm}"``).
    * ``install`` — also offer the systems that are not installed yet.
    """
    opts = []
    for vm in installed_vms(sid):
        meta = distro_info(sid, vm) or {}
        label = meta.get("label") or vm
        if template:
            cmds = [template.format(vm=vm)]
        else:
            cmds = [f"vm use {vm}"] + ([then] if then else [])
        opts.append({"label": label, "cmds": cmds})
    if host_allowed(sid) and then and not template:
        opts.append({"label": "Host (server shell)", "cmds": ["vm mode host", then]})
    if install:
        for d in installable(sid):
            opts.append({"label": f"+ Install {d.name} ({d.size})",
                         "cmds": [f"vm install {d.id}"], "group": "install"})
    return {"title": "Choose a system", "options": opts}


# --------------------------------------------------------------- launch specs
def _guest_shell(sid, vm):
    root = rootfs_path(sid, vm)
    for cand in ("/bin/bash", "/bin/sh"):
        p = root / cand.lstrip("/")
        if p.exists() or p.is_symlink():
            return cand
    return "/bin/sh"


def _vm_env(sid, vm, probe=False, tty=False):
    env = {
        "HOME": VM_HOME, "USER": "root", "LOGNAME": "root",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "TERM": os.environ.get("TERM") or "xterm-256color",
        "COLORTERM": "truecolor", "LANG": "C.UTF-8", "HOSTNAME": vm,
        "SHELL": _guest_shell(sid, vm), "RCONSOLE_VM": vm,
        "PROOT_TMP_DIR": str(config.VM_DIR / "tmp"),
    }
    if not tty:
        # No terminal to answer prompts: installs must never block waiting for
        # debconf/needrestart dialogs (tzdata and friends).
        env["DEBIAN_FRONTEND"] = "noninteractive"
        env["NEEDRESTART_MODE"] = "a"
        env["APT_LISTCHANGES_FRONTEND"] = "none"
    if _seccomp_off():
        env["PROOT_NO_SECCOMP"] = "1"
    if not probe:
        for k, v in (state.get_env(sid) or {}).items():
            if not k.startswith("PROOT_"):
                env[k] = v
    return env


def _proot_prefix(sid, vm, cwd, admin, extra_binds=()):
    proot = find_proot()
    argv = [proot, "-0", "-r", str(rootfs_path(sid, vm)),
            "-b", "/dev", "-b", f"{home_path(sid, vm)}:{VM_HOME}"]
    if admin or config.env_bool("RCONSOLE_USER_VM_PROC", False):
        argv += ["-b", "/proc"]
    else:
        # The full /proc exposes every host process (and their environments),
        # so non-admins get only the harmless system-wide info files. That is
        # enough for free, uptime, neofetch and friends.
        for name in SAFE_PROC_FILES:
            host = f"/proc/{name}"
            if os.path.exists(host):
                argv += ["-b", f"{host}:{host}"]
    if os.path.exists("/etc/resolv.conf"):
        argv += ["-b", "/etc/resolv.conf:/etc/resolv.conf"]
    for host_path, guest_path in extra_binds:
        argv += ["-b", f"{host_path}:{guest_path}"]
    argv += ["-w", cwd or VM_HOME]
    if proot_supports(proot, "--kill-on-exit"):
        argv.append("--kill-on-exit")   # reap guest daemons when the shell exits
    return argv


def _vm_cwd(sid, vm, cwd):
    """Validate a guest cwd; fall back to the guest home."""
    if not cwd or not cwd.startswith("/"):
        return VM_HOME
    try:
        host, _ = resolve_in_root(rootfs_path(sid, vm), cwd, binds=_binds(sid, vm))
        return cwd if host.is_dir() or cwd == VM_HOME else VM_HOME
    except (OSError, SandboxError):
        return VM_HOME


def spec(sid, cmd, cwd, forced=None, tty=False):
    """Build the launch :class:`Spec` for a shell command (or login shell).

    ``cmd`` may carry an ``@host``/``@vm``/``@vm:<id>`` prefix. An empty command
    means an interactive shell. ``tty`` says the process gets a real terminal
    (then prompts may be interactive). Raises :class:`SandboxError` if not
    permitted, or :class:`NeedVmChoice` if the user must pick a system first.
    """
    tag, cmd = parse_target(cmd)
    tgt = resolve(sid, forced or tag)
    if tgt.mode == HOST:
        import shellutil
        exe, flag = shellutil.detect_shell()
        argv = [exe, flag, cmd] if cmd else [exe]
        host_cwd = cwd if cwd and os.path.isdir(cwd) else state.default_cwd()
        return Spec(argv, host_cwd, shellutil.build_env(sid), HOST)
    config.ensure_private_dir(config.VM_DIR)
    guest_cwd = _vm_cwd(sid, tgt.vm, cwd)
    sh = _guest_shell(sid, tgt.vm)
    inner = [sh, "-c", cmd] if cmd else [sh, "-l"]
    argv = _proot_prefix(sid, tgt.vm, guest_cwd, is_admin(sid)) + inner
    return Spec(argv, str(config.VM_DIR), _vm_env(sid, tgt.vm, tty=tty), VM, tgt.vm,
                note=guest_cwd)


def argv_spec(sid, inner_argv, cwd, forced=None, extra_env=None, extra_binds=()):
    """Like :func:`spec` but for an explicit argv (hosted apps) — no shell.

    ``extra_env`` is merged into the environment; ``extra_binds`` is a list of
    ``(host_path, guest_path)`` pairs mounted into the VM (ignored on host).
    """
    tgt = resolve(sid, forced)
    if tgt.mode == HOST:
        import shellutil
        env = shellutil.build_env(sid)
        env.update(extra_env or {})
        return Spec(list(inner_argv), cwd, env, HOST)
    config.ensure_private_dir(config.VM_DIR)
    guest_cwd = _vm_cwd(sid, tgt.vm, cwd)
    argv = _proot_prefix(sid, tgt.vm, guest_cwd, is_admin(sid), extra_binds) + list(inner_argv)
    env = _vm_env(sid, tgt.vm)
    env.update(extra_env or {})
    return Spec(argv, str(config.VM_DIR), env, VM, tgt.vm, note=guest_cwd)


# ------------------------------------------------------------- path confinement
def _split(path):
    return [p for p in path.split("/") if p not in ("", ".")]


def _to_host(root, binds, parts):
    """Map guest path components to a host path, honouring bind mounts."""
    for vprefix, hdir in (binds or {}).items():
        pre = _split(vprefix)
        if parts[:len(pre)] == pre:
            return Path(hdir).joinpath(*parts[len(pre):])
    return root.joinpath(*parts)


def resolve_in_root(root, vpath, follow_last=True, max_links=40, binds=None):
    """Resolve ``vpath`` inside ``root`` as if ``root`` were ``/``.

    Symlinks (including absolute ones) are interpreted relative to ``root`` and
    ``..`` can never climb above it, so the result is always inside ``root`` (or
    inside one of the ``binds``, host directories mounted at a guest path — the
    VM's ``/root`` home). Returns ``(host_path, virtual_path)``.
    """
    root = Path(os.path.realpath(root))
    pending = list(reversed(_split(vpath)))
    resolved = []
    links = 0
    while pending:
        comp = pending.pop()
        if comp == "..":
            if resolved:
                resolved.pop()
            continue
        host = _to_host(root, binds, [*resolved, comp])
        if (follow_last or pending) and host.is_symlink():
            links += 1
            if links > max_links:
                raise OSError("too many levels of symbolic links")
            target = os.readlink(host)
            if target.startswith("/"):
                resolved = []
            pending.extend(reversed(_split(target)))
            continue
        resolved.append(comp)
    return _to_host(root, binds, resolved), "/" + "/".join(resolved)


class FsView:
    """A path namespace: the whole host, or a VM's root filesystem."""

    def __init__(self, root=None, binds=None):
        self.root = Path(root) if root else None
        self.binds = dict(binds or {})

    @property
    def unrestricted(self):
        return self.root is None

    def host(self, path, cwd=None, follow_last=True):
        """Map a (possibly relative) view path to a real host ``Path``."""
        path = (path or "").strip()
        if self.root is None:
            p = path if os.path.isabs(path) else os.path.join(cwd or state.default_cwd(), path)
            return Path(os.path.abspath(p))
        if not path.startswith("/"):
            path = posixpath.join(cwd or VM_HOME, path)
        return resolve_in_root(self.root, path, follow_last, binds=self.binds)[0]

    def virt(self, host_path):
        """Map a real host path back to the view's path."""
        if self.root is None:
            return str(host_path)
        real = os.path.realpath(host_path)
        for vprefix, hdir in self.binds.items():
            h = os.path.realpath(hdir)
            if real == h or real.startswith(h + os.sep):
                rel = os.path.relpath(real, h)
                return vprefix if rel == "." else vprefix + "/" + rel.replace(os.sep, "/")
        rel = os.path.relpath(real, os.path.realpath(self.root))
        if rel == ".." or rel.startswith(".." + os.sep):
            raise SandboxError("path escapes the VM")
        return "/" if rel == "." else "/" + rel.replace(os.sep, "/")

    def default_cwd(self):
        return state.default_cwd() if self.root is None else VM_HOME

    def child_virt(self, parent_virt, name):
        if self.root is None:
            return os.path.join(parent_virt, name)
        return posixpath.join(parent_virt, name)


def _binds(sid, vm):
    """Directories mounted into the VM: guest path -> host directory."""
    return {VM_HOME: home_path(sid, vm)}


def fs(sid, forced=None):
    """File namespace for ``sid`` (raises :class:`SandboxError` if unavailable)."""
    tgt = resolve(sid, forced)
    if tgt.mode == HOST:
        return FsView(None)
    return FsView(rootfs_path(sid, tgt.vm), _binds(sid, tgt.vm))
