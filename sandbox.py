"""Execution targets for rconsole: the real host, or a per-user VM.

Every place that runs a command (line passthrough, background jobs, PTY
sessions, hosted apps, the AI agent) and every place that touches files (file
APIs, editor, file manager) goes through this module. It is the **single
choke point for authorization**, so a client can never reach the host by
bypassing the command interpreter (e.g. by opening the terminal websocket with
an arbitrary command).

Targets
-------
``host``  The machine rconsole runs on, with the privileges of the server
          process. Admin-only (``RCONSOLE_ALLOW_USER_HOST=1`` relaxes this).
``vm``    A per-user Linux root filesystem (Alpine by default) run under
          `proot <https://proot-me.github.io/>`_. Inside it the user is
          ``root`` and may ``apk add`` anything — no host privileges needed, so
          it works on locked-down platforms such as Render.

The VM is a user-space compatibility layer, **not a hard security boundary**:
guest processes share the server's PID namespace and uid. Only create accounts
for people you trust; use one container per tenant for hostile multi-tenancy.
For that reason ``/proc`` is not exposed to non-admin guests by default.

Command prefix
--------------
A command string may start with ``@vm`` or ``@host`` to force a target (used
by ``vm shell``); the prefix is authorized exactly like the mode itself.
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

# Static proot build published by the proot project (x86_64 only).
PROOT_URL = "https://github.com/proot-me/proot/releases/download/v5.5.0/proot"
PROOT_SHA256 = "db185300a9daabbce86aca7632d8c7fdc4249f6f153171954013b70c5f491ec0"

MAX_DOWNLOAD = 400 * 1024 * 1024
DISTRO_FILE = "distro.json"
SAFE_PROC_FILES = ("uptime", "meminfo", "cpuinfo", "loadavg", "version")
CACHE_KEEP = 4                      # downloaded rootfs tarballs kept for reuse
TOOLS_SRC = config.HERE / "vmtools" / "rfetch.sh"


class SandboxError(Exception):
    """A command or path could not be mapped onto a target."""


class PermissionDenied(SandboxError):
    """The user is not allowed to use the requested target."""


@dataclass
class Spec:
    """How to launch a process: argv + working directory + environment."""
    argv: list
    cwd: str
    env: dict
    mode: str = HOST
    note: str = field(default="", repr=False)


# --------------------------------------------------------------------- helpers
def _safe(sid):
    """Filesystem-safe directory name for a username."""
    s = re.sub(r"[^A-Za-z0-9._-]", "_", str(sid))
    return "_" + s if s.startswith(".") or not s else s


def vm_base(sid):
    return config.VM_DIR / _safe(sid)


def rootfs_path(sid):
    return vm_base(sid) / ROOTFS_DIRNAME


def home_path(sid):
    return vm_base(sid) / HOME_DIRNAME


def supported():
    """The VM needs Linux (proot uses ptrace)."""
    return config.VM_ENABLED and sys.platform.startswith("linux")


def installed(sid):
    return (vm_base(sid) / READY_MARKER).exists() and rootfs_path(sid).is_dir()


def is_admin(sid):
    return auth.is_admin(sid)


# ----------------------------------------------------------------------- modes
def host_allowed(sid):
    return is_admin(sid) or config.ALLOW_USER_HOST


def effective_mode(sid):
    """Return the target (``host``/``vm``) commands for ``sid`` will use."""
    if not host_allowed(sid):
        return VM
    pref = state.get_mode(sid)
    if pref in (HOST, VM):
        return pref
    return config.DEFAULT_ADMIN_MODE if is_admin(sid) else VM


def set_mode(sid, mode):
    """Persist the preferred target. Returns ``(ok, message)``."""
    mode = (mode or "").lower()
    if mode not in (HOST, VM):
        return False, "mode must be 'host' or 'vm'"
    if mode == HOST and not host_allowed(sid):
        return False, "host access is restricted to admins"
    if mode == VM and not supported():
        return False, "the VM is unavailable (needs Linux and RCONSOLE_VM=1)"
    state.set_mode(sid, mode)
    return True, None


def default_cwd(sid, mode=None):
    mode = mode or effective_mode(sid)
    return VM_HOME if mode == VM else state.default_cwd()


def reset_cwds(sid):
    """After a mode switch, put every tab back in a valid directory."""
    sess = state.get_session(sid)
    if not sess:
        return
    cwd = default_cwd(sid)
    with state.STATE_LOCK:
        for tab in sess["tabs"].values():
            tab["cwd"] = cwd
    state.save_now(sid)


# --------------------------------------------------------------- authorization
def parse_target(cmd):
    """Split a leading ``@vm``/``@host`` marker. Returns ``(forced, rest)``."""
    cmd = cmd or ""
    for tag in (HOST, VM):
        marker = "@" + tag
        if cmd == marker or cmd.startswith(marker + " "):
            return tag, cmd[len(marker):].lstrip()
    return None, cmd


def resolve_target(sid, forced=None):
    """Decide the target for a launch and enforce authorization."""
    mode = forced or effective_mode(sid)
    if mode == HOST and not host_allowed(sid):
        raise PermissionDenied(
            "host access is restricted to admins. "
            "Use your VM instead: `vm install`, then `vm shell`.")
    if mode == VM:
        _require_vm(sid)
    return mode


def _require_vm(sid):
    if not supported():
        raise SandboxError("the VM is unavailable on this server "
                           "(needs Linux and RCONSOLE_VM=1).")
    if install_status(sid)["state"] == "installing":
        raise SandboxError("your VM is still installing; check `vm status`.")
    if not installed(sid):
        raise SandboxError("no VM yet. Run `vm install` (takes ~30s).")
    if not find_proot():
        raise SandboxError("proot is missing. Run `vm install` to fetch it.")
    sync_guest_tools(sid)


# ----------------------------------------------------------------- proot setup
_proot_lock = threading.Lock()
_no_seccomp = False
_NO_SECCOMP_MARKER = config.VM_DIR / ".no-seccomp"


def _seccomp_off():
    """proot's seccomp acceleration is disabled (probe found it unsupported).

    Remembered on disk, so a restart keeps using the setting that worked."""
    return _no_seccomp or bool(os.environ.get("PROOT_NO_SECCOMP")) \
        or _NO_SECCOMP_MARKER.exists()


def find_proot():
    for cand in (os.environ.get("RCONSOLE_VM_PROOT"),
                 shutil.which("proot"),
                 str(config.VM_DIR / "bin" / "proot")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


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
    """Return a usable proot path, downloading the static build if needed."""
    with _proot_lock:
        found = find_proot()
        if found:
            return found
        if distros.machine() not in ("x86_64", "amd64"):
            raise SandboxError(
                f"no prebuilt proot for {os.uname().machine}. Install it with "
                "your package manager (apt/apk install proot) or set "
                "RCONSOLE_VM_PROOT.")
        dest = config.VM_DIR / "bin" / "proot"
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
_installs = {}                   # sid -> {"state": ..., "message": ...}
_installs_lock = threading.Lock()


def install_status(sid):
    with _installs_lock:
        rec = dict(_installs.get(sid) or {})
    if rec.get("state") in ("installing", "failed"):
        return rec
    return {"state": "ready" if installed(sid) else "absent", "message": ""}


def _set_install(sid, st, msg=""):
    with _installs_lock:
        _installs[sid] = {"state": st, "message": msg}


def distro_info(sid):
    """What this user's VM was built from (``None`` if not installed)."""
    try:
        return json.loads((vm_base(sid) / DISTRO_FILE).read_text())
    except (OSError, ValueError):
        pass
    if installed(sid):    # VMs made before distros existed were always Alpine
        return {"id": "alpine", "release": "latest", "family": "alpine",
                "label": "Alpine Linux"}
    return None


def family(sid):
    """Package-manager family of the user's VM: ``alpine`` or ``debian``."""
    info = distro_info(sid)
    return (info or {}).get("family", "alpine")


def install(sid, choice=None, wait=False):
    """Provision this user's VM from ``choice`` (``"debian:bookworm"`` etc.).

    Returns ``(started, message)``. Validation happens up front so typos are
    reported immediately instead of failing later in the background.
    """
    if not supported():
        return False, "the VM is unavailable (needs Linux and RCONSOLE_VM=1)."
    try:
        distro, release = distros.parse_choice(choice)
    except distros.DistroError as e:
        return False, str(e)
    with _installs_lock:
        if (_installs.get(sid) or {}).get("state") == "installing":
            return False, "already installing; check `vm status`."
    if installed(sid):
        cur = (distro_info(sid) or {}).get("label", "a system")
        return False, (f"VM already installed ({cur}). To switch systems run "
                       f"`vm reset --yes` (keeps /root), then `vm install {distro.id}`.")
    _set_install(sid, "installing", f"starting {distro.name}")
    t = threading.Thread(target=_install_worker, args=(sid, distro, release), daemon=True)
    t.start()
    if wait:
        t.join()
        st = install_status(sid)
        return st["state"] == "ready", st["message"] or st["state"]
    return True, (f"installing {distro.name} ({release}) in the background; "
                  f"check `vm status`.")


def _post_setup(rootfs, distro):
    """Small per-family tweaks so the system behaves under proot."""
    rootfs = Path(rootfs)
    for d in ("dev", "proc", "tmp", "root", "etc", "usr/local/bin"):
        (rootfs / d).mkdir(parents=True, exist_ok=True)
    try:
        (rootfs / "tmp").chmod(0o1777)
    except OSError:
        pass
    if distro.family == "debian":
        # apt normally drops to an unprivileged `_apt` user, which fake-root
        # proot cannot do; and services must not try to start during installs.
        conf = rootfs / "etc/apt/apt.conf.d"
        conf.mkdir(parents=True, exist_ok=True)
        (conf / "99rconsole").write_text(
            'APT::Sandbox::User "root";\n'
            'Acquire::Retries "3";\n'
            'Dpkg::Options { "--force-unsafe-io"; };\n')
        policy = rootfs / "usr/sbin/policy-rc.d"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text("#!/bin/sh\nexit 101\n")
        policy.chmod(0o755)


def sync_guest_tools(sid):
    """Install the ``neofetch``/``fetch``/``rfetch`` shim into the VM.

    Lives in ``/usr/local/bin`` so it works in every distro and takes priority
    over a real neofetch installed later (which stays at /usr/bin/neofetch).
    """
    try:
        dest_dir = rootfs_path(sid) / "usr/local/bin"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "rfetch"
        if not dest.exists() or dest.stat().st_size != TOOLS_SRC.stat().st_size:
            shutil.copyfile(TOOLS_SRC, dest)
            dest.chmod(0o755)
        for alias in ("neofetch", "fetch"):
            link = dest_dir / alias
            if not link.exists() and not link.is_symlink():
                link.symlink_to("rfetch")
    except OSError:
        pass   # cosmetic: never block the VM on this


def _install_worker(sid, distro, release):
    global _no_seccomp
    base = vm_base(sid)
    try:
        config.ensure_private_dir(config.VM_DIR)
        _set_install(sid, "installing", "fetching proot")
        ensure_proot()
        _set_install(sid, "installing", f"downloading {distro.name}")
        tarball, label = _fetch_rootfs(distro, release)
        _set_install(sid, "installing", "extracting")
        config.ensure_private_dir(base)
        tmp_root = base / (ROOTFS_DIRNAME + ".new")
        _extract(tarball, tmp_root)
        final = rootfs_path(sid)
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp_root, final)
        home_path(sid).mkdir(parents=True, exist_ok=True)
        os.chmod(home_path(sid), 0o700)
        (config.VM_DIR / "tmp").mkdir(parents=True, exist_ok=True)
        _post_setup(final, distro)
        sync_guest_tools(sid)
        _set_install(sid, "installing", "testing proot")
        err = _probe(sid)
        if err:
            _no_seccomp = True            # retry without seccomp acceleration
            err = _probe(sid)
            if not err:
                _NO_SECCOMP_MARKER.write_text("1")
        if err:
            shutil.rmtree(final, ignore_errors=True)
            raise SandboxError(
                "proot cannot run on this host (ptrace may be blocked): " + err)
        (base / DISTRO_FILE).write_text(json.dumps({
            "id": distro.id, "release": release, "family": distro.family,
            "label": label, "installed_at": int(time.time())}))
        (base / READY_MARKER).write_text(str(int(time.time())))
        _set_install(sid, "ready", "")
    except Exception as e:  # surfaced through `vm status`
        shutil.rmtree(base / (ROOTFS_DIRNAME + ".new"), ignore_errors=True)
        _set_install(sid, "failed", str(e))


def _probe(sid):
    """Run ``true`` inside the VM. Returns an error string, or ``None``."""
    import subprocess
    argv = _proot_prefix(sid, VM_HOME, admin=True) + ["/bin/sh", "-c", "echo ok"]
    try:
        p = subprocess.run(argv, env=_vm_env(sid, probe=True), capture_output=True,
                           text=True, timeout=30, cwd=str(config.VM_DIR))
    except Exception as e:
        return str(e)
    if p.returncode != 0 or "ok" not in p.stdout:
        return (p.stderr or p.stdout or f"exit {p.returncode}").strip()[:300]
    return None


def reset(sid, keep_home=True):
    """Delete the VM's root filesystem (and optionally its home dir)."""
    base = vm_base(sid)
    if not base.exists():
        return False
    (base / READY_MARKER).unlink(missing_ok=True)
    shutil.rmtree(rootfs_path(sid), ignore_errors=True)
    if not keep_home:
        shutil.rmtree(base, ignore_errors=True)
    _set_install(sid, "absent", "")
    return True


def rename_user(old, new):
    """Move a user's VM directory when the account is renamed."""
    src, dst = vm_base(old), vm_base(new)
    if src.exists() and not dst.exists():
        os.replace(src, dst)
    with _installs_lock:
        _installs.pop(old, None)


def disk_usage(sid):
    total = 0
    for dirpath, _dirs, files in os.walk(vm_base(sid)):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def describe(sid):
    """Human-readable facts for ``vm status``."""
    st = install_status(sid)
    info = {
        "supported": supported(), "state": st["state"], "message": st["message"],
        "mode": effective_mode(sid), "proot": find_proot(),
        "host_allowed": host_allowed(sid), "distro": None, "disk": None,
        "family": None, "distro_id": None, "release": None,
    }
    if installed(sid):
        meta = distro_info(sid) or {}
        info.update(distro_id=meta.get("id"), release=meta.get("release"),
                    family=meta.get("family"))
        try:
            text = (rootfs_path(sid) / "etc" / "os-release").read_text()
            m = re.search(r'PRETTY_NAME="?([^"\n]+)', text)
            info["distro"] = m.group(1) if m else meta.get("label")
        except OSError:
            info["distro"] = meta.get("label")
        info["disk"] = disk_usage(sid)
    return info


# --------------------------------------------------------------- launch specs
def _guest_shell(sid):
    root = rootfs_path(sid)
    for cand in ("/bin/bash", "/bin/sh"):
        p = root / cand.lstrip("/")
        if p.exists() or p.is_symlink():
            return cand
    return "/bin/sh"


def _vm_env(sid, probe=False):
    env = {
        "HOME": VM_HOME, "USER": "root", "LOGNAME": "root",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "TERM": os.environ.get("TERM") or "xterm-256color",
        "COLORTERM": "truecolor", "LANG": "C.UTF-8", "HOSTNAME": "rconsole",
        "SHELL": _guest_shell(sid),
        "PROOT_TMP_DIR": str(config.VM_DIR / "tmp"),
    }
    if _seccomp_off():
        env["PROOT_NO_SECCOMP"] = "1"
    if not probe:
        for k, v in (state.get_env(sid) or {}).items():
            if not k.startswith("PROOT_"):
                env[k] = v
    return env


def _proot_prefix(sid, cwd, admin, extra_binds=()):
    proot = find_proot()
    argv = [proot, "-0", "-r", str(rootfs_path(sid)),
            "-b", "/dev", "-b", f"{home_path(sid)}:{VM_HOME}"]
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


def _vm_cwd(sid, cwd):
    """Validate a guest cwd; fall back to the guest home."""
    if not cwd or not cwd.startswith("/"):
        return VM_HOME
    try:
        host, _ = resolve_in_root(rootfs_path(sid), cwd, binds=_binds(sid))
        return cwd if host.is_dir() or cwd == VM_HOME else VM_HOME
    except (OSError, SandboxError):
        return VM_HOME


def spec(sid, cmd, cwd, forced=None):
    """Build the launch :class:`Spec` for a shell command (or login shell).

    ``cmd`` may carry an ``@vm``/``@host`` prefix. An empty command means an
    interactive shell. Raises :class:`SandboxError` if not permitted.
    """
    tag, cmd = parse_target(cmd)
    mode = resolve_target(sid, forced or tag)
    if mode == HOST:
        import shellutil
        exe, flag = shellutil.detect_shell()
        argv = [exe, flag, cmd] if cmd else [exe]
        host_cwd = cwd if cwd and os.path.isdir(cwd) else state.default_cwd()
        return Spec(argv, host_cwd, shellutil.build_env(sid), HOST)
    config.ensure_private_dir(config.VM_DIR)
    guest_cwd = _vm_cwd(sid, cwd)
    sh = _guest_shell(sid)
    inner = [sh, "-c", cmd] if cmd else [sh, "-l"]
    argv = _proot_prefix(sid, guest_cwd, is_admin(sid)) + inner
    return Spec(argv, str(config.VM_DIR), _vm_env(sid), VM, note=guest_cwd)


def argv_spec(sid, inner_argv, cwd, forced=None, extra_env=None, extra_binds=()):
    """Like :func:`spec` but for an explicit argv (hosted apps) — no shell.

    ``extra_env`` is merged into the environment; ``extra_binds`` is a list of
    ``(host_path, guest_path)`` pairs mounted into the VM (ignored on host).
    """
    mode = resolve_target(sid, forced)
    if mode == HOST:
        import shellutil
        env = shellutil.build_env(sid)
        env.update(extra_env or {})
        return Spec(list(inner_argv), cwd, env, HOST)
    config.ensure_private_dir(config.VM_DIR)
    guest_cwd = _vm_cwd(sid, cwd)
    argv = _proot_prefix(sid, guest_cwd, is_admin(sid), extra_binds) + list(inner_argv)
    env = _vm_env(sid)
    env.update(extra_env or {})
    return Spec(argv, str(config.VM_DIR), env, VM, note=guest_cwd)


# ------------------------------------------------------------- path confinement
def _split(path):
    return [p for p in path.split("/") if p not in ("", ".")]


def _binds(sid):
    """Directories mounted into the VM: guest path -> host directory."""
    return {VM_HOME: home_path(sid)}


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


def fs(sid):
    """File namespace for ``sid`` (raises :class:`SandboxError` if unavailable)."""
    mode = resolve_target(sid)
    if mode == HOST:
        return FsView(None)
    return FsView(rootfs_path(sid), _binds(sid))
