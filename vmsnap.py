"""VM snapshots, clones and pre-baked templates.

Three related things, all built from the same two parts (a VM's ``rootfs`` = its system files and ``home`` = its
``/root``, plus the small ``distro.json`` / ``setup.json`` / ``deps.json`` records):

**Snapshot** — a named, compressed copy of one VM at one moment (``vm snapshot create``). Put it back with
``vm snapshot restore`` and the VM is exactly as it was: system files, ``/root``, installed packages and
``vm setup`` records. Use it before something risky (``apt full-upgrade``, a big ``pip install``, a rewrite of
your app). Snapshots belong to one user and one VM name, are limited in number, and live on the data disk.

**Clone** — a second, independent VM made from a first one (``vm clone``), or from one of its snapshots
(``vm clone debian@before-upgrade test``). It takes seconds and no download: nothing is fetched, the files are
copied. The clone has its own ``/root``, its own processes and its own name; it is a normal VM in every way
(``vm shell``, ``vm update``, services, backup of its package list).

**Template** — a *pre-baked* VM stored as a recipe card: ``vm template save`` freezes a VM you already
prepared, ``vm template bake`` builds one from scratch (install the distro, run ``vm setup`` profiles, save,
throw the scratch VM away), and ``vm template use`` makes a new VM from it in seconds with Python, build tools,
Node ... already inside. Templates are private to you; an admin may make one ``--shared`` so everyone can start
from it. Secrets never go into a template (shell history, ``~/.ssh``, tokens: see :data:`SECRET_PATHS`).

Where things live: ``<data>/vmstore/snapshots/<user>/<vm>/<name>.tar.gz`` and
``<data>/vmstore/templates/<owner or _shared>/<name>.tar.gz``, each with a ``.json`` next to it. They are
deliberately **outside** the encrypted backup (a system is hundreds of MB) and are not rebuilt on a fresh disk;
the *VMs* are (a clone is reinstalled from its distro, its ``/root`` and recorded packages come back).

While one of these runs, the VM it touches is **locked** (state ``working``): nothing can start in it, like
during ``vm update``. One such job per user at a time. The copy is *crash-consistent*: files are copied as they
are, so a database that was mid-write is as a power cut would leave it; stop the app first for a clean copy.
"""
import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import config
import distros
import pidns
import procview
import sandbox
import vmupdate

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z")
MAX_SNAPSHOTS = 5                     # per VM
MAX_TEMPLATES = 8                     # per user (shared ones: SHARED_MAX)
SHARED_MAX = 12
MIN_FREE = 200 * 1024 * 1024          # bytes that must stay free on the data disk
SHARED = "_shared"
META_FILES = ("distro.json", "setup.json", "deps.json")
# Never copied into a template (a template is meant to be handed to somebody else).
SECRET_PATHS = (
    "home/.bash_history", "home/.zsh_history", "home/.python_history", "home/.node_repl_history",
    "home/.lesshst", "home/.viminfo", "home/.ssh", "home/.gnupg", "home/.netrc", "home/.git-credentials",
    "home/.config/gh", "home/.aws", "home/.azure", "home/.kube", "home/.docker/config.json",
    "home/.npmrc", "home/.pypirc", "home/.cargo/credentials.toml", "home/.cargo/credentials",
    "home/.cache/pip", "home/.rconsole-hush-done",
)
GZIP_LEVEL = 3                        # fast on tiny CPUs, still ~3x smaller

_ops_lock = threading.Lock()
_last = {}                            # (sid, vm) -> {"op", "ok", "message", "at"}
_last_lock = threading.Lock()


class SnapError(Exception):
    """A snapshot / clone / template request that cannot be done (the message says why)."""


# ------------------------------------------------------------------------------------ paths
def store_dir():
    return config.DATA_DIR / "vmstore"


def _safe(sid):
    return sandbox._safe(sid)


def snap_dir(sid, vm):
    if not sandbox._valid_vm_id(vm):
        raise SnapError(f"bad VM name '{vm}'")
    return store_dir() / "snapshots" / _safe(sid) / vm


def tpl_dir(owner):
    return store_dir() / "templates" / (SHARED if owner == SHARED else _safe(owner))


def check_name(name, what="name"):
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise SnapError(f"a {what} is 1-32 characters: letters, digits, '.', '_' and '-' (start with a letter or digit)")
    return name


def default_snapshot_name():
    return time.strftime("snap-%Y%m%d-%H%M%S", time.gmtime())


# ------------------------------------------------------------------------------- bookkeeping
def _record(sid, vm, op, ok, message):
    with _last_lock:
        _last[(sid, vm)] = {"op": op, "ok": ok, "message": message, "at": int(time.time())}


def last_result(sid, vm):
    with _last_lock:
        rec = _last.get((sid, vm))
        return dict(rec) if rec else None


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _list_dir(folder):
    out = []
    try:
        names = sorted(p for p in os.listdir(folder) if p.endswith(".json"))
    except OSError:
        return out
    for fn in names:
        meta = _read_json(Path(folder) / fn)
        name = fn[:-5]
        if meta and (Path(folder) / (name + ".tar.gz")).is_file():
            out.append(dict(meta, name=name))
    out.sort(key=lambda m: m.get("created", 0))
    return out


def list_snapshots(sid, vm):
    """``[{name, created, bytes, unpacked, note, distro, release}]`` of one VM, oldest first."""
    return _list_dir(snap_dir(sid, vm))


def snapshot_vms(sid):
    """Names of VMs that have snapshots (even VMs that were removed since)."""
    base = store_dir() / "snapshots" / _safe(sid)
    try:
        return sorted(d for d in os.listdir(base) if (base / d).is_dir() and list_snapshots(sid, d))
    except OSError:
        return []


def list_templates(sid):
    """Templates this user may use: their own and the shared ones (their own win on a name clash)."""
    mine = _list_dir(tpl_dir(sid))
    names = {t["name"] for t in mine}
    shared = [dict(t, shared=True) for t in _list_dir(tpl_dir(SHARED)) if t["name"] not in names]
    return [dict(t, shared=False) for t in mine] + shared


def find_template(sid, name):
    name = (name or "").strip().lower()
    for t in list_templates(sid):
        if t["name"] == name:
            return t
    return None


def _archive_path(t, sid):
    return tpl_dir(SHARED if t.get("shared") else sid) / (t["name"] + ".tar.gz")


# ------------------------------------------------------------------------------- the disk
def free_bytes(path=None):
    p = Path(path or config.DATA_DIR)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return MIN_FREE * 2


def _need_free(needed, path=None):
    free = free_bytes(path)
    if free < needed + MIN_FREE:
        raise SnapError(f"not enough disk: this needs about {_mb(needed)} MB and {_mb(MIN_FREE)} MB must stay free, "
                        f"but only {_mb(free)} MB are free. Delete snapshots or VMs you no longer need.")


def _mb(n):
    return max(1, int(n) // (1024 * 1024))


def unpacked_size(sid, vm):
    base = sandbox.vm_dir(sid, vm)
    return sum(vmupdate.tree_size(base / d) for d in (sandbox.ROOTFS_DIRNAME, sandbox.HOME_DIRNAME)
               if (base / d).is_dir())


# ----------------------------------------------------------------------- tar in / tar out
def _members(base):
    out = [d for d in (sandbox.ROOTFS_DIRNAME, sandbox.HOME_DIRNAME) if (base / d).is_dir()]
    return out + [f for f in META_FILES if (base / f).is_file()]


def write_archive(base, out, exclude=()):
    """Compress ``rootfs``, ``home`` and the records of the VM directory ``base`` into ``out`` (atomically)."""
    if not shutil.which("tar"):
        raise SnapError("the `tar` program is not installed on this server")
    members = _members(Path(base))
    if sandbox.ROOTFS_DIRNAME not in members:
        raise SnapError("the system files are missing")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    cmd = ["tar", "--create", "--directory", str(base), "--warning=no-file-changed",
           "--warning=no-file-removed", "--ignore-failed-read"]
    for e in exclude:
        cmd += ["--exclude", e]
    cmd += ["--"] + members
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        try:
            with gzip.open(tmp, "wb", compresslevel=GZIP_LEVEL) as g:
                shutil.copyfileobj(p.stdout, g, 1024 * 1024)
        except BaseException:
            p.kill()
            tmp.unlink(missing_ok=True)
            raise
        finally:
            p.stdout.close()
        rc = p.wait()
        if rc not in (0, 1):                         # 1 = "some files changed while reading": fine
            err.seek(0)
            tail = err.read().decode(errors="replace").strip().splitlines()[-1:] or ["tar failed"]
            tmp.unlink(missing_ok=True)
            raise SnapError(f"could not pack the system: {tail[0][:200]}")
    os.replace(tmp, out)
    return out.stat().st_size


def extract_archive(archive, dest):
    """Unpack an archive made by :func:`write_archive` into the empty directory ``dest``."""
    if not shutil.which("tar"):
        raise SnapError("the `tar` program is not installed on this server")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as err:
        r = subprocess.run(["tar", "--extract", "--gzip", "--same-permissions", "--no-same-owner",
                            "--directory", str(dest), "--file", str(archive)], stderr=err)
        if r.returncode != 0:
            err.seek(0)
            tail = err.read().decode(errors="replace").strip().splitlines()[-1:] or ["tar failed"]
            raise SnapError(f"could not unpack it: {tail[0][:200]}")
    if not (dest / sandbox.ROOTFS_DIRNAME).is_dir():
        raise SnapError("the archive holds no system (corrupted?)")


def _rm(path):
    """Remove a tree even when package scripts left read-only directories in it."""
    path = Path(path)
    if not (path.exists() or path.is_symlink()):
        return

    def fix(func, p, _exc):
        try:
            os.chmod(os.path.dirname(p), 0o700)
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=fix)


def _copy_tree(src, dst):
    """Copy a directory tree keeping permissions, symlinks and hard links (``cp -a``; Python as a fallback)."""
    src, dst = Path(src), Path(dst)
    if shutil.which("cp"):
        dst.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["cp", "-a", "--no-target-directory", str(src), str(dst)], capture_output=True, text=True)
        if r.returncode == 0:
            return
        err = (r.stderr or "").strip().splitlines()[-1:] or ["cp failed"]
        raise SnapError(f"could not copy the files: {err[0][:200]}")
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True,
                    ignore_dangling_symlinks=True)           # pragma: no cover - cp exists wherever VMs do


# ----------------------------------------------------------------------- locking one user's job
def _guard(sid, vm=None, new=None):
    """Check that a job may start and lock ``vm`` (state ``working``) and ``new`` (state ``installing``).

    Must be called with :data:`_ops_lock` held. Raises :class:`SnapError` with the reason otherwise."""
    if not sandbox.supported():
        raise SnapError("the VM is unavailable (needs Linux and RCONSOLE_VM=1).")
    if vm is not None:
        if vm not in sandbox.installed_vms(sid):
            raise SnapError(f"'{vm}' is not installed (see `vm`).")
    busy = sandbox.installing(sid) + sandbox.updating(sid) + sandbox.working(sid)
    if busy:
        raise SnapError(f"{', '.join(busy)} is still being installed, updated or copied; one job at a time "
                        "(see `vm status`).")
    if vm is not None:
        reason = sandbox.busy_reason(sid, vm)
        if reason:
            raise SnapError(reason)


def _run_job(sid, vm, label, fn, *, new=None, wait=False, op="job"):
    """Run ``fn(set_progress)`` in a thread with ``vm`` locked (and ``new`` marked as being created)."""
    def progress(msg):
        text = f"{label}: {msg}" if msg else label
        if vm:
            sandbox._set_install(sid, vm, "working", text)
        if new:                                  # a VM that exists already (a bake's scratch system) is "working"
            sandbox._set_install(sid, new, "working" if sandbox.installed(sid, new) else "installing", text)

    def body():
        sandbox._tl.restorer = True            # our own steps may use the locked VM (busy_reason checks this flag)
        ok, msg = False, "failed"
        try:
            result = fn(progress)
            ok, msg = True, (result or "done")
        except SnapError as e:
            ok, msg = False, str(e)
        except Exception as e:                  # never leave a VM locked
            ok, msg = False, f"{type(e).__name__}: {e}"
        finally:
            sandbox._tl.restorer = False
            if vm:
                sandbox._clear_install(sid, vm)
            if new:
                sandbox._clear_install(sid, new)
            _record(sid, new or vm, op, ok, msg)
            try:
                import vmports
                vmports.forget(sid)
            except Exception:
                pass
        return ok, msg

    with _ops_lock:                              # check + lock atomically
        try:
            _guard(sid, vm, new)
        except SnapError as e:
            return False, str(e)
        if vm:
            sandbox._set_install(sid, vm, "working", label)
        if new:
            sandbox._set_install(sid, new, "installing", label)
    result = {}

    def runner():
        result["r"] = body()
    t = threading.Thread(target=runner, name=f"vmsnap-{op}", daemon=True)
    t.start()
    if wait:
        t.join()
        return result.get("r", (False, "failed"))
    return True, label + " in the background; `vm status` shows progress."


# ------------------------------------------------------------------------------ snapshots
def create_snapshot(sid, vm, name=None, note="", wait=False):
    """``vm snapshot create``. Returns ``(ok, message)``."""
    try:
        name = check_name(name or default_snapshot_name(), "snapshot name")
        folder = snap_dir(sid, vm)
        have = list_snapshots(sid, vm)
        if any(s["name"] == name for s in have):
            raise SnapError(f"{vm} already has a snapshot called '{name}'. Pick another name or delete it first.")
        if len(have) >= MAX_SNAPSHOTS:
            raise SnapError(f"{vm} already has {MAX_SNAPSHOTS} snapshots (the most). Delete one first: "
                            f"`vm snapshot delete {have[0]['name']} {vm}`.")
        if vm not in sandbox.installed_vms(sid):
            raise SnapError(f"'{vm}' is not installed (see `vm`).")
        size = unpacked_size(sid, vm)
        _need_free(size // 2 + 1024 * 1024, folder)       # compressed, roughly half
    except SnapError as e:
        return False, str(e)
    info = sandbox.distro_info(sid, vm) or {}

    def work(progress):
        progress("compressing the system and /root")
        archive = folder / (name + ".tar.gz")
        packed = write_archive(sandbox.vm_dir(sid, vm), archive)
        _write_json(folder / (name + ".json"), {
            "created": int(time.time()), "bytes": packed, "unpacked": size, "note": (note or "")[:200],
            "distro": info.get("id") or vm, "release": info.get("release") or "", "label": info.get("label") or "",
            "family": info.get("family") or "", "vm": vm})
        _prune_empty(snap_dir(sid, vm).parent)
        return f"snapshot '{name}' of {vm} saved ({_mb(packed)} MB)"
    return _run_job(sid, vm, f"snapshotting '{name}'", work, wait=wait, op="snapshot")


def delete_snapshot(sid, vm, name):
    name = (name or "").strip().lower()
    folder = snap_dir(sid, vm)
    if not any(s["name"] == name for s in list_snapshots(sid, vm)):
        return False, f"{vm} has no snapshot called '{name}' (see `vm snapshot list {vm}`)."
    for ext in (".tar.gz", ".json", ".tar.gz.part"):
        (folder / (name + ext)).unlink(missing_ok=True)
    _prune_empty(folder)
    return True, f"snapshot '{name}' of {vm} deleted."


def _prune_empty(folder):
    try:
        folder = Path(folder)
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass


def restore_snapshot(sid, vm, name, wait=False):
    """``vm snapshot restore``: replace the VM's system and ``/root`` with the snapshot. Stops what runs in it."""
    name = (name or "").strip().lower()
    snap = next((s for s in list_snapshots(sid, vm) if s["name"] == name), None)
    if snap is None:
        return False, f"{vm} has no snapshot called '{name}' (see `vm snapshot list {vm}`)."
    if sandbox.is_admin(sid) and sandbox.effective_mode(sid) == sandbox.VM \
            and sandbox.current_target(sid) == f"vm:{vm}":
        return False, ("admins restore from the host shell, not from inside the VM being replaced. "
                       "Run `vm mode host`, then restore again.")
    try:
        _need_free(snap.get("unpacked") or snap.get("bytes", 0) * 3, sandbox.vm_dir(sid, vm))
    except SnapError as e:
        return False, str(e)
    archive = snap_dir(sid, vm) / (name + ".tar.gz")

    def work(progress):
        base = sandbox.vm_dir(sid, vm)
        progress("stopping what runs in it")
        vmupdate.stop_running(sid, vm, False)
        pidns.stop(sid, vm)
        procview.forget(base)
        new = base / ".restore-new"
        _rm(new)
        try:
            progress("unpacking the snapshot")
            extract_archive(archive, new)
            progress("switching over")
            _swap_in(base, new)
        finally:
            _rm(new)
        sandbox.sync_guest_tools(sid, vm)
        return f"{vm} is back to snapshot '{name}'"
    return _run_job(sid, vm, f"restoring '{name}'", work, wait=wait, op="restore")


def _swap_in(base, new):
    """Replace ``rootfs``, ``home`` and the records of ``base`` with the ones unpacked in ``new``.

    The old ones are moved aside first and put back if anything fails, so a failed restore leaves the VM as it was."""
    parts = [sandbox.ROOTFS_DIRNAME, sandbox.HOME_DIRNAME, *META_FILES]
    moved = []
    try:
        for part in parts:
            src, cur = new / part, base / part
            if not src.exists():
                if part in META_FILES and cur.exists():      # the snapshot had no such record: drop the stale one
                    os.replace(cur, base / (part + ".old"))
                    moved.append((part, False))
                continue
            if cur.exists() or cur.is_symlink():
                os.replace(cur, base / (part + ".old"))
                moved.append((part, True))
            else:
                moved.append((part, None))
            os.replace(src, cur)
    except OSError as e:
        for part, had in reversed(moved):                    # undo
            cur, old = base / part, base / (part + ".old")
            try:
                if had is None:
                    _rm(cur)
                else:
                    if cur.exists() and had:
                        _rm(cur)
                    if old.exists():
                        os.replace(old, cur)
            except OSError:
                pass
        raise SnapError(f"could not switch the files over ({e}); nothing was changed")
    for part, _had in moved:
        _rm(base / (part + ".old"))


# ---------------------------------------------------------------------------------- clones
def _check_new_name(sid, new):
    new = (new or "").strip().lower()
    if not sandbox._valid_vm_id(new):
        raise SnapError("a VM name is 1-32 characters: lowercase letters, digits, '.', '_' and '-' (start with a "
                        "letter or digit)")
    if new in distros.CATALOG or new in distros._alias_map():
        raise SnapError(f"'{new}' is the name of a system you can install; pick another (e.g. {new}-2).")
    if new in sandbox.installed_vms(sid) or sandbox.vm_dir(sid, new).exists():
        raise SnapError(f"you already have a system called '{new}'.")
    return new


def _finish_new_vm(sid, new, source_meta):
    """Write the records of a freshly copied/unpacked VM and mark it ready."""
    base = sandbox.vm_dir(sid, new)
    meta = dict(_read_json(base / "distro.json") or source_meta or {})
    meta["installed_at"] = int(time.time())
    meta["cloned_from"] = source_meta.get("cloned_from") if source_meta else None
    meta.setdefault("family", (source_meta or {}).get("family", "debian"))
    _write_json(base / "distro.json", meta)
    home = base / sandbox.HOME_DIRNAME
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    (base / sandbox.READY_MARKER).write_text(str(int(time.time())))
    sandbox.sync_guest_tools(sid, new)
    try:
        import gitstore
        gitstore.materialize_vm(sid, new)
    except Exception:
        pass


def clone(sid, source, new, snapshot=None, wait=False):
    """``vm clone <source>[@snapshot] <new>``: a second VM from a VM (as it is now) or from one of its snapshots."""
    try:
        new = _check_new_name(sid, new)
        archive = None
        if snapshot:
            snap = next((s for s in list_snapshots(sid, source) if s["name"] == snapshot), None)
            if snap is None:
                raise SnapError(f"{source} has no snapshot called '{snapshot}' (see `vm snapshot list {source}`).")
            archive = snap_dir(sid, source) / (snapshot + ".tar.gz")
            _need_free(snap.get("unpacked") or snap.get("bytes", 0) * 3)
            lock_vm = source if source in sandbox.installed_vms(sid) else None
        else:
            if source not in sandbox.installed_vms(sid):
                raise SnapError(f"'{source}' is not installed (see `vm`).")
            _need_free(unpacked_size(sid, source))
            lock_vm = source
        src_meta = sandbox.distro_info(sid, source) or {}
    except SnapError as e:
        return False, str(e)
    label = f"cloning {source}" + (f"@{snapshot}" if snapshot else "")

    def work(progress):
        base = sandbox.vm_dir(sid, new)
        config.ensure_private_dir(config.VM_DIR)
        config.ensure_private_dir(sandbox.user_dir(sid))
        try:
            config.ensure_private_dir(base)
            if archive is not None:
                progress("unpacking the snapshot")
                extract_archive(archive, base)
            else:
                progress("copying the system")
                src = sandbox.vm_dir(sid, source)
                _copy_tree(src / sandbox.ROOTFS_DIRNAME, base / sandbox.ROOTFS_DIRNAME)
                if (src / sandbox.HOME_DIRNAME).is_dir():
                    progress("copying /root")
                    _copy_tree(src / sandbox.HOME_DIRNAME, base / sandbox.HOME_DIRNAME)
                for f in META_FILES:
                    if (src / f).is_file():
                        shutil.copy2(src / f, base / f)
            progress("finishing")
            record = dict(_read_json(base / "distro.json") or src_meta)
            record["cloned_from"] = source + (f"@{snapshot}" if snapshot else "")
            _write_json(base / "distro.json", record)
            _finish_new_vm(sid, new, record)
        except BaseException:
            _rm(base)
            raise
        return f"{new} is ready (a copy of {source}{'@' + snapshot if snapshot else ''}); `vm shell {new}`"
    return _run_job(sid, lock_vm, label, work, new=new, wait=wait, op="clone")


# -------------------------------------------------------------------------------- templates
def _template_exclude():
    return list(SECRET_PATHS)


def save_template(sid, vm, name, shared=False, description="", wait=False):
    """``vm template save``: freeze a VM you prepared as a template (``shared``: for everyone, admins only)."""
    try:
        name = check_name(name, "template name")
        if shared and not sandbox.is_admin(sid):
            raise SnapError("only admins can make a shared template.")
        owner = SHARED if shared else sid
        have = _list_dir(tpl_dir(owner))
        if any(t["name"] == name for t in have):
            raise SnapError(f"there is already a {'shared ' if shared else ''}template called '{name}'. "
                            f"Delete it first: `vm template delete {name}{' --shared' if shared else ''}`.")
        cap = SHARED_MAX if shared else MAX_TEMPLATES
        if len(have) >= cap:
            raise SnapError(f"that is the most templates ({cap}); delete one first (`vm template list`).")
        if vm not in sandbox.installed_vms(sid):
            raise SnapError(f"'{vm}' is not installed (see `vm`).")
        size = unpacked_size(sid, vm)
        _need_free(size // 2 + 1024 * 1024, tpl_dir(owner))
    except SnapError as e:
        return False, str(e)
    info = sandbox.distro_info(sid, vm) or {}
    setups = sandbox.recorded_setups(sid, vm)

    def work(progress):
        progress("compressing (secrets left out)")
        folder = tpl_dir(owner)
        packed = write_archive(sandbox.vm_dir(sid, vm), folder / (name + ".tar.gz"), exclude=_template_exclude())
        _write_json(folder / (name + ".json"), {
            "created": int(time.time()), "bytes": packed, "unpacked": size, "owner": sid,
            "description": (description or "")[:200], "distro": info.get("id") or vm,
            "release": info.get("release") or "", "label": info.get("label") or "", "family": info.get("family") or "",
            "setups": setups, "from": vm})
        return f"{'shared ' if shared else ''}template '{name}' saved from {vm} ({_mb(packed)} MB)"
    return _run_job(sid, vm, f"saving template '{name}'", work, wait=wait, op="template")


def delete_template(sid, name, shared=False):
    name = (name or "").strip().lower()
    if shared and not sandbox.is_admin(sid):
        return False, "only admins can delete a shared template."
    owner = SHARED if shared else sid
    folder = tpl_dir(owner)
    if not any(t["name"] == name for t in _list_dir(folder)):
        hint = "" if shared else " (a shared template needs `--shared`)"
        return False, f"you have no {'shared ' if shared else ''}template called '{name}'{hint}."
    for ext in (".tar.gz", ".json", ".tar.gz.part"):
        (folder / (name + ext)).unlink(missing_ok=True)
    _prune_empty(folder)
    return True, f"{'shared ' if shared else ''}template '{name}' deleted."


def use_template(sid, name, new, wait=False):
    """``vm template use <name> <new>``: a new VM from a template, in seconds."""
    try:
        t = find_template(sid, name)
        if t is None:
            raise SnapError(f"no template called '{name}' (see `vm template list`).")
        new = _check_new_name(sid, new)
        _need_free(t.get("unpacked") or t.get("bytes", 0) * 3)
        archive = _archive_path(t, sid)
        if not archive.is_file():
            raise SnapError(f"template '{name}' is damaged (its archive is gone); delete it.")
    except SnapError as e:
        return False, str(e)

    def work(progress):
        base = sandbox.vm_dir(sid, new)
        config.ensure_private_dir(config.VM_DIR)
        config.ensure_private_dir(sandbox.user_dir(sid))
        try:
            config.ensure_private_dir(base)
            progress("unpacking the template")
            extract_archive(archive, base)
            record = dict(_read_json(base / "distro.json") or {})
            record["cloned_from"] = f"template:{t['name']}"
            _write_json(base / "distro.json", record)
            progress("finishing")
            _finish_new_vm(sid, new, record)
        except BaseException:
            _rm(base)
            raise
        return f"{new} is ready from template '{t['name']}'; `vm shell {new}`"
    return _run_job(sid, None, f"creating {new} from '{t['name']}'", work, new=new, wait=wait, op="template-use")


def bake_template(sid, name, distro_choice, profiles, shared=False, description="", wait=False):
    """``vm template bake``: install a system, run ``vm setup`` profiles in it, save it as a template, delete it.

    Builds in a scratch VM called ``bake-<name>`` that is removed at the end, whatever happens."""
    import devsetup
    try:
        name = check_name(name, "template name")
        if shared and not sandbox.is_admin(sid):
            raise SnapError("only admins can make a shared template.")
        owner = SHARED if shared else sid
        have = _list_dir(tpl_dir(owner))
        if any(t["name"] == name for t in have):
            raise SnapError(f"there is already a {'shared ' if shared else ''}template called '{name}'.")
        if len(have) >= (SHARED_MAX if shared else MAX_TEMPLATES):
            raise SnapError("that is the most templates; delete one first (`vm template list`).")
        try:
            distro, release = distros.parse_choice(distro_choice)
        except distros.DistroError as e:
            raise SnapError(str(e))
        for p in profiles:
            if p not in devsetup.PROFILES:
                raise SnapError(f"unknown setup profile '{p}' (see `vm setup`).")
            refused = devsetup.check_target(p, distro.id)
            if refused:
                raise SnapError(f"profile '{p}': {refused}")
        scratch = f"bake-{name}"[:32]
        if not sandbox._valid_vm_id(scratch) or scratch in sandbox.installed_vms(sid):
            raise SnapError("a scratch system for this name already exists; pick another template name.")
        _need_free(1200 * 1024 * 1024, config.VM_DIR)       # a rootfs plus toolchains: be generous
    except SnapError as e:
        return False, str(e)

    def work(progress):
        base = sandbox.vm_dir(sid, scratch)
        try:
            progress(f"installing {distro.name}")
            sandbox._install_one(sid, distro, release, vm=scratch)
            if not sandbox.installed(sid, scratch):
                raise SnapError("the system could not be installed: "
                                + (sandbox.install_state(sid, scratch).get("message") or "see `vm status`"))
            sandbox._set_install(sid, scratch, "installing", f"baking {name}")
            for p in profiles:
                progress(f"setting up '{p}' (this takes a while)")
                text = devsetup.script(p, sandbox.family(sid, scratch))
                path = sandbox.rootfs_path(sid, scratch) / "tmp" / f"rconsole-setup-{p}.sh"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                path.chmod(0o755)
                sp = sandbox.spec(sid, f"@vm:{scratch} sh /tmp/{path.name}", "/root")
                r = subprocess.run(sp.argv, cwd=sp.cwd, env=sp.env, capture_output=True, text=True,
                                   timeout=3600, stdin=subprocess.DEVNULL)
                if r.returncode != 0:
                    tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-1:] or [""]
                    raise SnapError(f"setup '{p}' failed: {tail[0][:200]}")
                sandbox.record_setup(sid, scratch, p)
            progress("saving the template")
            size = unpacked_size(sid, scratch)
            info = sandbox.distro_info(sid, scratch) or {}
            folder = tpl_dir(owner)
            packed = write_archive(base, folder / (name + ".tar.gz"), exclude=_template_exclude())
            _write_json(folder / (name + ".json"), {
                "created": int(time.time()), "bytes": packed, "unpacked": size, "owner": sid,
                "description": (description or "")[:200], "distro": info.get("id") or distro.id,
                "release": info.get("release") or "", "label": info.get("label") or "",
                "family": info.get("family") or distro.family, "setups": list(profiles), "from": f"baked:{distro.id}"})
            return f"template '{name}' baked ({distro.name}{' + ' + ', '.join(profiles) if profiles else ''}, " \
                   f"{_mb(packed)} MB)"
        finally:
            sandbox._clear_install(sid, scratch)
            pidns.stop(sid, scratch)
            procview.forget(base)
            _rm(base)
    label = f"baking '{name}'"
    return _run_job(sid, None, label, work, new=scratch, wait=wait, op="bake")


# ----------------------------------------------------------------------------- housekeeping
def sweep_leftovers():
    """Remove scratch directories an interrupted job left behind (called at start-up)."""
    n = 0
    base = config.VM_DIR
    try:
        users = [u for u in base.iterdir() if u.is_dir() and u.name not in ("bin", "cache", "tmp")]
    except OSError:
        return 0
    for u in users:
        try:
            for v in [x for x in u.iterdir() if x.is_dir()]:
                if v.name.startswith("bake-") and not (v / sandbox.READY_MARKER).exists() \
                        and not sandbox.install_state(u.name, v.name)["state"] in ("installing", "working"):
                    _rm(v)
                    n += 1
                else:
                    for junk in (".restore-new",):
                        if (v / junk).exists():
                            _rm(v / junk)
                            n += 1
        except OSError:
            continue
    for p in store_dir().rglob("*.part") if store_dir().exists() else []:
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def remove_user(sid):
    """Delete every snapshot and private template of a user (the account is being deleted)."""
    for d in (store_dir() / "snapshots" / _safe(sid), tpl_dir(sid)):
        _rm(d)


def rename_user(old, new):
    for kind in ("snapshots", "templates"):
        src, dst = store_dir() / kind / _safe(old), store_dir() / kind / _safe(new)
        if src.exists() and not dst.exists():
            try:
                os.replace(src, dst)
            except OSError:
                pass


def summary(sid):
    """Dashboard data: ``{"snapshots": {vm: [..]}, "templates": [..]}`` (cheap: reads small JSON files)."""
    snaps = {vm: list_snapshots(sid, vm) for vm in snapshot_vms(sid)}
    return {"snapshots": snaps, "templates": list_templates(sid)}
