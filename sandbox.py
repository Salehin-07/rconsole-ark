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

ALPINE_BASE = "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/{arch}/"
MAX_DOWNLOAD = 400 * 1024 * 1024
_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


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
        arch = _ARCH.get(os.uname().machine.lower())
        if arch != "x86_64":
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
    entry = {}
    entries = []
    for line in text.splitlines():
        if line.strip() == "-":
            if entry:
                entries.append(entry)
            entry = {}
            continue
        m = re.match(r"\s+(\w+):\s*\"?([^\"]*?)\"?\s*$", line)
        if m:
            entry[m.group(1)] = m.group(2)
    if entry:
        entries.append(entry)
    for e in entries:
        if e.get("flavor") == flavor and e.get("file") and e.get("sha256"):
            return e["file"], e["sha256"]
    raise SandboxError(f"no {flavor} entry in the Alpine release index")


def _fetch_rootfs(progress=None):
    """Return the path of a verified rootfs tarball (cached)."""
    cache = config.VM_DIR / "cache"
    override = os.environ.get("RCONSOLE_VM_ROOTFS_URL")
    if override:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", override.rsplit("/", 1)[-1]) or "rootfs.tar.gz"
        dest = cache / name
        want = os.environ.get("RCONSOLE_VM_ROOTFS_SHA256")
        if dest.exists() and (not want or _sha256_file(dest) == want.lower()):
            return dest
        return _download(override, dest, want, progress)
    arch = _ARCH.get(os.uname().machine.lower())
    if not arch:
        raise SandboxError(f"unsupported CPU architecture: {os.uname().machine}")
    base = ALPINE_BASE.format(arch=arch)
    try:
        r = requests.get(base + "latest-releases.yaml", timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        raise SandboxError(f"could not reach the Alpine mirror: {e}") from e
    fname, digest = parse_alpine_release(r.text)
    dest = cache / fname
    if dest.exists() and _sha256_file(dest) == digest:
        return dest
    return _download(base + fname, dest, digest, progress)


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


def _extract(tarball, target):
    target = Path(target)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    with tarfile.open(tarball) as tf:
        tf.extractall(target, filter=_extract_filter)


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


def install(sid, wait=False):
    """Provision this user's VM. Returns ``(started, message)``."""
    if not supported():
        return False, "the VM is unavailable (needs Linux and RCONSOLE_VM=1)."
    with _installs_lock:
        if (_installs.get(sid) or {}).get("state") == "installing":
            return False, "already installing; check `vm status`."
    if installed(sid):
        return False, "VM already installed. Use `vm reset` to recreate it."
    _set_install(sid, "installing", "starting")
    t = threading.Thread(target=_install_worker, args=(sid,), daemon=True)
    t.start()
    if wait:
        t.join()
        st = install_status(sid)
        return st["state"] == "ready", st["message"] or st["state"]
    return True, "installing in the background; check `vm status`."


def _install_worker(sid):
    global _no_seccomp
    base = vm_base(sid)
    try:
        config.ensure_private_dir(config.VM_DIR)
        _set_install(sid, "installing", "fetching proot")
        ensure_proot()
        _set_install(sid, "installing", "downloading root filesystem")
        tarball = _fetch_rootfs()
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
        for d in ("dev", "proc", "tmp", "root", "etc"):
            (final / d).mkdir(exist_ok=True)
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
        (base / READY_MARKER).write_text(str(int(time.time())))
        _set_install(sid, "ready", "")
    except Exception as e:  # surfaced through `vm status`
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
    }
    if installed(sid):
        try:
            text = (rootfs_path(sid) / "etc" / "os-release").read_text()
            m = re.search(r'PRETTY_NAME="?([^"\n]+)', text)
            info["distro"] = m.group(1) if m else None
        except OSError:
            pass
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
