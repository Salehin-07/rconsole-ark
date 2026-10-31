"""Self-update: ``rconsole update`` checks GitHub for a newer release and installs it safely.

What "safely" means here, in the order things happen:

1. **Lock.** From the moment an update starts, the console is locked: every request except the health
   probe and the status endpoint gets ``503`` (see ``app.update_gate``), so no command, file edit,
   terminal or proxied app can be used until the update is over. The browser shows a progress screen.
2. **Download + verify.** The release's source archive is downloaded and checked against the release's
   own ``SHA256SUMS``. An archive without a matching checksum is refused. It is unpacked by a strict
   extractor (no links, no ``..``, no absolute paths, size and count limits).
3. **Test before touching anything.** The new tree must carry the right version, compile, and *import*
   (in a separate process with a throw-away data directory). Changed Python dependencies are installed
   into ``<data>/update/site-<version>`` (never into the system) and are part of that test.
4. **Quiet the server.** Only now are services, jobs and terminals stopped and a final backup taken.
5. **Swap the program files.** Every file about to be replaced or removed is first copied to
   ``<data>/update/previous``; files are replaced one by one, atomically. Your data (``<data>/users.json``,
   VMs, workspace, state, services, secrets, backups …) is never part of the program tree and is never
   touched. Any failure restores the previous files.
6. **Restart** in place. The new process confirms it is healthy after a short while; if it never does
   (it keeps crashing on start) the next container start restores the previous version by itself.

A container image is rebuilt from your repository on every redeploy, which would undo an in-place update;
so the verified release is also kept in ``<data>/update/releases`` and ``docker-entrypoint.sh`` re-applies
it (``python updater.py sync``) whenever the image is older than that. On a host with no persistent disk
(Render's free plan) the copy travels with the encrypted backups: after the restore at boot,
:func:`after_restore` re-applies it and restarts the server once. Without backups an update lasts
until the instance restarts; redeploy from the new release to make it permanent (see FREE_HOSTING.md).

This module only needs the standard library at import time, so the entrypoint can run it before
anything else starts. ``python updater.py sync|status|check|rollback`` works without the web server.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import config

APP_DIR = config.HERE
UPDATE_DIR = config.UPDATE_DIR
STATE_FILE = UPDATE_DIR / "state.json"
MANIFEST_FILE = UPDATE_DIR / "manifest.json"
PREVIOUS_DIR = UPDATE_DIR / "previous"
RELEASES_DIR = UPDATE_DIR / "releases"
WORK_DIR = UPDATE_DIR / "work"

API = config.UPDATE_API
USER_AGENT = "rconsole-updater"
MAX_DOWNLOAD = 150 * 1024 * 1024        # bytes: the whole source archive
MAX_UNPACKED = 300 * 1024 * 1024
MAX_FILES = 6000
MAX_BOOTS = 2                           # failed starts of a new version before it is rolled back
CONFIRM_AFTER = 25                      # seconds a new process must stay up to count as healthy
RELOAD_DELAY = 2.5                      # seconds between "restarting" and the restart (lets the UI see it)
RELOAD_GRACE = 90                       # seconds to wait for the restart before unlocking again
SMOKE_TIMEOUT = 90
DEPS_TIMEOUT = 900

# Top-level names that belong to the machine, not to the release: never replaced, never removed.
PROTECTED_TOP = {"data", ".venv", "venv", ".git", ".env", "__pycache__", ".ruff_cache", ".pytest_cache"}
REQUIRED_FILES = ("app.py", "version.py", "requirements.txt", "commands.py", "updater.py",
                  "docker-entrypoint.sh", "Dockerfile")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
VERSION_LINE = re.compile(r'^__version__\s*=\s*"(\d+\.\d+\.\d+)"', re.M)


class UpdateError(Exception):
    """An update step failed; the message is meant for the person who typed the command."""


# --------------------------------------------------------------------------- versions
def parse_version(text):
    """``"v1.3.0"`` / ``"1.3.0"`` -> ``(1, 3, 0)``; anything else -> ``None``."""
    m = VERSION_RE.match((text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def read_version(root):
    """The ``__version__`` written in ``root/version.py`` (read from disk, not imported)."""
    try:
        m = VERSION_LINE.search((Path(root) / "version.py").read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def installed_version():
    """The version of the program files on disk right now."""
    return read_version(APP_DIR) or "0.0.0"


# --------------------------------------------------------------------------- state file
def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_state():
    """``{"active": {version, deps}, "previous_active": …, "pending": {version, boots},
    "bad": [versions], "last": {ok, from, to, at, message, mode}}``"""
    data = _read_json(STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def write_state(state):
    _write_json(STATE_FILE, state)


def deps_dir():
    """Directory of dependencies installed by an update, or ``None``."""
    name = (read_state().get("active") or {}).get("deps")
    if not name or "/" in name or ".." in name:
        return None
    path = UPDATE_DIR / name
    return path if path.is_dir() else None


def activate_deps():
    """Put the update's dependency directory first on ``sys.path`` (called before Flask is imported)."""
    path = deps_dir()
    if path and str(path) not in sys.path:
        sys.path.insert(0, str(path))
    return path


# --------------------------------------------------------------------------- live status / the lock
_lock = threading.RLock()
_live = {"active": False, "mode": "", "stage": "", "pct": 0, "from": "", "to": "", "log": [],
         "started": 0.0, "error": ""}


def is_locked():
    """True while an update (or rollback) is running: the console accepts nothing else."""
    return _live["active"]


def _step(stage, pct, message):
    with _lock:
        _live.update(stage=stage, pct=pct)
        _live["log"].append(f"{time.strftime('%H:%M:%S')}  {message}")
        del _live["log"][:-60]


def status(detail=False):
    """JSON-safe status for ``/api/update/status``. ``detail`` adds the progress log (logged-in users)."""
    with _lock:
        out = {"updating": _live["active"], "mode": _live["mode"], "stage": _live["stage"],
               "pct": _live["pct"], "from": _live["from"], "to": _live["to"],
               "error": _live["error"], "version": installed_version()}
        if detail:
            out["log"] = list(_live["log"])
    last = read_state().get("last")
    if isinstance(last, dict):
        out["last"] = {k: last.get(k) for k in ("ok", "from", "to", "at", "message", "mode")}
    return out


def _begin(mode, from_v, to_v):
    with _lock:
        if _live["active"]:
            raise UpdateError("an update is already running")
        _live.update(active=True, mode=mode, stage="starting", pct=1, log=[], error="",
                     started=time.time(), **{"from": from_v, "to": to_v})
    _step("starting", 1, f"{mode} {from_v} -> {to_v}")


def _end():
    with _lock:
        _live["active"] = False


def _fail(message):
    with _lock:
        _live["error"] = message
    _step("failed", _live["pct"], f"FAILED: {message}")
    _record_last(False, message)
    _end()


def _record_last(ok, message):
    state = read_state()
    state["last"] = {"ok": bool(ok), "from": _live["from"], "to": _live["to"], "mode": _live["mode"],
                     "at": time.time(), "message": message}
    try:
        write_state(state)
    except OSError:
        pass


# --------------------------------------------------------------------------- talking to GitHub
class _Redirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never send our token to another host (release assets live on a CDN)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_Redirect)


def _request(url, accept="application/vnd.github+json", timeout=20):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT,
                                               "X-GitHub-Api-Version": "2022-11-28"})
    if config.UPDATE_TOKEN and urlsplit(url).netloc in (urlsplit(API).netloc, "github.com"):
        req.add_header("Authorization", f"Bearer {config.UPDATE_TOKEN}")
    try:
        return _opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError(f"nothing found at {urlsplit(url).path} (is RCONSOLE_UPDATE_REPO="
                              f"{config.UPDATE_REPO} right, and does it have a release?)") from None
        if e.code in (401, 403, 429):
            raise UpdateError(f"GitHub refused the request (HTTP {e.code}) — probably the anonymous rate "
                              "limit or a private repo; set RCONSOLE_UPDATE_TOKEN") from None
        raise UpdateError(f"GitHub answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise UpdateError(f"cannot reach GitHub: {getattr(e, 'reason', e)}") from None


def _get_bytes(url, accept="application/vnd.github+json", limit=2 * 1024 * 1024):
    with _request(url, accept) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("unexpectedly large response")
    return data


def check_repo_name():
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config.UPDATE_REPO):
        raise UpdateError(f"RCONSOLE_UPDATE_REPO must look like owner/repo, not {config.UPDATE_REPO!r}")


def check():
    """Ask GitHub for the latest release. Returns a dict (see keys below) or raises UpdateError."""
    check_repo_name()
    data = json.loads(_get_bytes(f"{API}/repos/{config.UPDATE_REPO}/releases/latest"))
    tag = str(data.get("tag_name") or "")
    ver = parse_version(tag)
    if not ver:
        raise UpdateError(f"the latest release has an unusable tag ({tag!r}); expected vX.Y.Z")
    latest = ".".join(map(str, ver))
    current = installed_version()
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    tar_name = f"rconsole-{latest}.tar.gz"
    return {
        "current": current, "latest": latest, "tag": tag,
        "newer": ver > (parse_version(current) or (0, 0, 0)),
        "same": ver == parse_version(current),
        "notes": str(data.get("body") or ""), "published": str(data.get("published_at") or "")[:10],
        "url": str(data.get("html_url") or ""),
        "tar_name": tar_name, "tar": assets.get(tar_name), "sums": assets.get("SHA256SUMS"),
        "bad": latest in (read_state().get("bad") or []),
    }


def blockers(force=False):
    """Reasons this installation cannot be updated in place (empty list = fine)."""
    out = []
    if not os.access(APP_DIR, os.W_OK):
        out.append(f"the program directory {APP_DIR} is read-only here — redeploy instead")
    if (APP_DIR / ".git").exists() and not force:
        out.append("this is a git checkout — update it with `git pull` (or `git checkout vX.Y.Z`); "
                   "add --force to overwrite its files from the release archive anyway")
    try:
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        probe = UPDATE_DIR / ".probe"
        probe.write_text("x")
        probe.unlink()
    except OSError as e:
        out.append(f"cannot write to {UPDATE_DIR}: {e}")
    return out


# --------------------------------------------------------------------------- download + unpack
def _asset_url(asset):
    """The API URL when a token is set (private repos need it), the public download URL otherwise."""
    if config.UPDATE_TOKEN and asset.get("url"):
        return asset["url"]
    return asset.get("browser_download_url") or asset.get("url")


def _download(asset, dest, pct_from, pct_to):
    """Download a release asset to ``dest``; returns its sha256."""
    url, accept = _asset_url(asset), "application/octet-stream"
    if not url:
        raise UpdateError(f"asset {asset.get('name')} has no download URL")
    size = int(asset.get("size") or 0)
    if size > MAX_DOWNLOAD:
        raise UpdateError(f"{asset.get('name')} is {size // 1048576} MB — refusing (limit {MAX_DOWNLOAD // 1048576})")
    h, done = hashlib.sha256(), 0
    with _request(url, accept, timeout=60) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            done += len(chunk)
            if done > MAX_DOWNLOAD:
                raise UpdateError("download is larger than the allowed limit")
            h.update(chunk)
            f.write(chunk)
            if size:
                with _lock:
                    _live["pct"] = pct_from + int((pct_to - pct_from) * min(1.0, done / size))
    if size and done != size:
        raise UpdateError(f"download is incomplete ({done} of {size} bytes)")
    return h.hexdigest()


def checksum_for(sums_text, name):
    """Find ``name`` in a ``sha256sum`` listing (``<hex>  <name>`` or ``<hex> *<name>``)."""
    for line in sums_text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1].lstrip("*").strip() == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def safe_extract(archive, dest):
    """Unpack a release tarball into ``dest`` (its single top directory is stripped).

    Only regular files and directories are accepted; absolute paths, ``..``, links and special
    files make it fail, as do too many or too large members."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    total = count = 0
    try:
        tf = tarfile.open(archive, "r:gz")
    except (tarfile.TarError, OSError) as e:
        raise UpdateError(f"the archive cannot be read: {e}") from None
    with tf:
        for m in tf:
            parts = Path(m.name).parts
            if m.name.startswith(("/", "\\")) or ".." in parts or any(":" in p for p in parts[:1]):
                raise UpdateError(f"unsafe path in the archive: {m.name!r}")
            if len(parts) < 2:
                if m.isdir() or m.name in ("pax_global_header",):
                    continue
                raise UpdateError(f"unexpected file at the top of the archive: {m.name!r}")
            rel = Path(*parts[1:])
            if m.isdir():
                (dest / rel).mkdir(parents=True, exist_ok=True)
                continue
            if not m.isreg():
                raise UpdateError(f"the archive contains a link or special file: {m.name!r}")
            count += 1
            total += m.size
            if count > MAX_FILES or total > MAX_UNPACKED:
                raise UpdateError("the archive is unreasonably large")
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)


def check_tree(root, version):
    """The unpacked release must be a complete rconsole of exactly this version."""
    root = Path(root)
    if read_version(root) != version:
        raise UpdateError(f"the archive says version {read_version(root)}, the release says {version}")
    missing = [f for f in REQUIRED_FILES if not (root / f).is_file()]
    if missing:
        raise UpdateError("the archive is not a complete rconsole (missing " + ", ".join(missing) + ")")
    for p in sorted(root.rglob("*.py")):
        try:
            compile(p.read_bytes(), str(p), "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as e:
            raise UpdateError(f"{p.relative_to(root)} does not compile: {e}") from None


def _req_lines(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def needs_deps(root):
    """Did the release change the Python dependencies?"""
    return _req_lines(Path(root) / "requirements.txt") != _req_lines(APP_DIR / "requirements.txt")


def install_deps(root, target):
    """Install the release's requirements into ``target`` (not into the system's site-packages)."""
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
           "--upgrade", "--target", str(target), "-r", str(Path(root) / "requirements.txt")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DEPS_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpdateError(f"installing dependencies failed: {e}") from None
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        raise UpdateError("installing dependencies failed: " + " | ".join(tail))


def smoke_test(root, extra_path=None):
    """Import the new app in a separate process with a throw-away data directory."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("RCONSOLE_BACKUP", "RCONSOLE_UPDATE"))}
    tmp = tempfile.mkdtemp(prefix="rconsole-smoke-")
    paths = [str(root)] + ([str(extra_path)] if extra_path else [])
    active = deps_dir()
    if active and not extra_path:
        paths.append(str(active))
    env.update(RCONSOLE_DATA_DIR=tmp, RCONSOLE_SUPERVISOR="0", RCONSOLE_KEEPALIVE="0",
               PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=os.pathsep.join(paths))
    env.pop("RCONSOLE_PUBLIC_URL", None)
    code = ("import app, os, sys; print('OK', app.__version__, flush=True); os._exit(0)")
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(root), env=env,
                           capture_output=True, text=True, timeout=SMOKE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise UpdateError("the new version did not finish starting within the test limit") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode != 0 or "OK" not in r.stdout:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        raise UpdateError("the new version does not start: " + " | ".join(tail))


# --------------------------------------------------------------------------- swapping files
def release_files(root):
    """Relative paths of every regular file in a release tree that belongs to the program."""
    root = Path(root)
    skip = set(PROTECTED_TOP)
    try:
        skip.add(config.DATA_DIR.relative_to(APP_DIR).parts[0])
    except (ValueError, IndexError):
        pass
    if not (APP_DIR / "tests").is_dir():    # a Docker image ships without the test-suite: keep it that way
        skip.add("tests")
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.parts[0] in skip or "__pycache__" in rel.parts:
            continue
        out.append(rel.as_posix())
    return out


def _inside_app(rel):
    """``APP_DIR/rel``, after checking its *directory* really is inside the program directory (a symlinked
    folder cannot lead a write elsewhere; a symlinked *file* is simply replaced by the new file)."""
    path = APP_DIR / rel
    try:
        path.parent.resolve().relative_to(APP_DIR.resolve())
    except ValueError:
        raise UpdateError(f"refusing to write outside the program directory: {rel}") from None
    return path


def _place(src, dst):
    """Atomically put ``src`` at ``dst`` (a symlink at ``dst`` is replaced, not written through)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".rcnew")
    shutil.copyfile(src, tmp)
    os.chmod(tmp, os.stat(src).st_mode & 0o777)
    os.replace(tmp, dst)


def apply_tree(root, from_v, to_v):
    """Replace the program files with those in ``root``, keeping a rollback copy. Returns the list of files.

    On any failure everything already replaced is put back and :class:`UpdateError` is raised."""
    new_files = release_files(root)
    wanted = set(new_files)
    old_files = _read_json(MANIFEST_FILE, {}).get("files") or []
    stale = [f for f in old_files if f not in wanted]
    shutil.rmtree(PREVIOUS_DIR, ignore_errors=True)
    (PREVIOUS_DIR / "files").mkdir(parents=True)
    added, saved = [], []
    for rel in new_files + stale:
        target = _inside_app(rel)
        if target.is_file() and not target.is_symlink():
            dest = PREVIOUS_DIR / "files" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
            saved.append(rel)
        elif rel in wanted:
            added.append(rel)
    _write_json(PREVIOUS_DIR / "meta.json", {"version": from_v, "to": to_v, "saved": saved, "added": added,
                                             "manifest_before": old_files, "at": time.time()})
    try:
        for rel in new_files:
            _place(Path(root) / rel, _inside_app(rel))
        for rel in stale:
            try:
                _inside_app(rel).unlink()
            except FileNotFoundError:
                pass
    except Exception as e:
        restore_previous()
        raise UpdateError(f"replacing the program files failed ({e}); the previous version was restored") from None
    _write_json(MANIFEST_FILE, {"version": to_v, "files": new_files})
    return new_files


def restore_previous():
    """Put the files saved by the last :func:`apply_tree` back. Returns the version restored (or ``None``)."""
    meta = _read_json(PREVIOUS_DIR / "meta.json", None)
    if not isinstance(meta, dict):
        return None
    for rel in meta.get("saved") or []:
        _place(PREVIOUS_DIR / "files" / rel, _inside_app(rel))
    for rel in meta.get("added") or []:
        try:
            _inside_app(rel).unlink()
        except FileNotFoundError:
            pass
    _write_json(MANIFEST_FILE, {"version": meta.get("version"), "files": meta.get("manifest_before") or []})
    return meta.get("version")


def _sync_vmtools(root):
    """Best effort: refresh the ``docker`` / ``drun`` commands the image installed in /usr/local/bin."""
    for name in ("docker", "drun"):
        src, dst = Path(root) / "vmtools" / name, Path("/usr/local/bin") / name
        try:
            if src.is_file() and dst.is_file() and os.access(dst, os.W_OK):
                shutil.copyfile(src, dst)
        except OSError:
            pass


def _prune(state):
    """Delete releases and dependency folders nothing refers to any more."""
    active = state.get("active") or {}
    keep_rel, keep_deps = active.get("version"), active.get("deps")
    for d in (RELEASES_DIR.iterdir() if RELEASES_DIR.is_dir() else []):
        if d.name != keep_rel:
            shutil.rmtree(d, ignore_errors=True)
    for d in UPDATE_DIR.glob("site-*"):
        if d.name != keep_deps:
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- the update itself
def _lazy_modules():
    """Import everything the final steps need BEFORE any program file is replaced, so this process never
    mixes old modules in memory with new ones on disk."""
    import backup  # noqa: F401
    import cmd_reboot
    import lifecycle
    return lifecycle, cmd_reboot


def _reload_and_unlock_later(reload_fn):
    """Restart the server; if it somehow does not happen, unlock the console again after a grace period."""
    def go():
        time.sleep(RELOAD_DELAY)
        try:
            reload_fn()
        except Exception as e:
            _step("restart", 99, f"restart failed: {e}")
        time.sleep(RELOAD_GRACE)
        _step("restart", 99, "still running — run `sudo reboot --yes` to finish")
        _end()
    threading.Thread(target=go, name="rconsole-update-reload", daemon=True).start()


def start(info, force=False):
    """Begin updating to ``info`` (from :func:`check`). Returns immediately; the console is locked."""
    if not info.get("tar") or not info.get("sums"):
        raise UpdateError(f"release {info['tag']} has no {info['tar_name']} / SHA256SUMS to install")
    problems = blockers(force)
    if problems:
        raise UpdateError(problems[0])
    lifecycle, cmd_reboot = _lazy_modules()
    _begin("update", info["current"], info["latest"])
    threading.Thread(target=_run_update, args=(info, lifecycle, cmd_reboot._reload), daemon=False,
                     name="rconsole-update").start()


def _run_update(info, lifecycle, reload_fn):
    from_v, to_v = info["current"], info["latest"]
    work = WORK_DIR
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        _step("download", 5, f"downloading {info['tar_name']}")
        digest = _download(info["tar"], work / "release.tar.gz", 5, 40)
        _step("verify", 42, "checking the SHA-256 checksum against the release's SHA256SUMS")
        sums = _get_bytes(_asset_url(info["sums"]), "application/octet-stream").decode("utf-8", "replace")
        expected = checksum_for(sums, info["tar_name"])
        if not expected:
            raise UpdateError(f"SHA256SUMS has no entry for {info['tar_name']}")
        if digest != expected:
            raise UpdateError("checksum mismatch — the download is corrupt or has been tampered with")
        _step("unpack", 48, "unpacking")
        tree = work / "tree"
        safe_extract(work / "release.tar.gz", tree)
        check_tree(tree, to_v)
        new_deps = None
        if needs_deps(tree):
            _step("deps", 55, "dependencies changed — installing them")
            new_deps = work / "site"
            install_deps(tree, new_deps)
        _step("test", 72, "testing the new version in a separate process")
        smoke_test(tree, new_deps)
        old_dockerfile = APP_DIR / "Dockerfile"
        image_changed = old_dockerfile.is_file() and (tree / "Dockerfile").read_bytes() != old_dockerfile.read_bytes()
    except Exception as e:                  # nothing has been touched yet
        _fail(str(e) if isinstance(e, UpdateError) else f"{type(e).__name__}: {e}")
        shutil.rmtree(work, ignore_errors=True)
        return

    # ---- point of no return: from here the old process is going away
    applied = False
    try:
        _step("stop", 80, "stopping services, jobs and terminals and taking a final backup")
        lifecycle.begin_shutdown()
        _step("install", 90, "replacing the program files (a rollback copy is kept)")
        apply_tree(tree, from_v, to_v)
        applied = True
        state = read_state()
        deps_name = (state.get("active") or {}).get("deps")
        if new_deps is not None:
            final = UPDATE_DIR / f"site-{to_v}"
            shutil.rmtree(final, ignore_errors=True)
            shutil.move(str(new_deps), str(final))
            deps_name = final.name
        shutil.rmtree(RELEASES_DIR / to_v, ignore_errors=True)
        RELEASES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tree), str(RELEASES_DIR / to_v))
        state["previous_active"] = state.get("active")
        state["active"] = {"version": to_v, "deps": deps_name}
        state["pending"] = {"version": to_v, "boots": 0, "since": time.time()}
        state["bad"] = [v for v in state.get("bad") or [] if v != to_v]
        write_state(state)
        _sync_vmtools(RELEASES_DIR / to_v)
        _prune(state)
        note = " (the container image itself changed: redeploy to get its system packages)" if image_changed else ""
        _step("restart", 97, f"updated {from_v} -> {to_v}{note}; restarting")
        _record_last(True, f"updated {from_v} -> {to_v}{note}")
    except Exception as e:
        msg = str(e) if isinstance(e, UpdateError) else f"{type(e).__name__}: {e}"
        # before the swap (or a failed swap, which apply_tree undoes) the old files are in place;
        # after it the new ones are, and only the bookkeeping failed
        result = "restarting on the new version" if applied else "restarting the previous version"
        if applied:
            _record_last(True, f"updated {from_v} -> {to_v} (bookkeeping failed: {msg})")
        else:
            with _lock:
                _live["error"] = msg
            _record_last(False, msg)
        _step("failed" if not applied else "restart", 97, f"{msg}; {result}")
    shutil.rmtree(work, ignore_errors=True)
    _reload_and_unlock_later(reload_fn)


def start_rollback():
    """Restore the files saved by the last update. Returns immediately; the console is locked."""
    meta = _read_json(PREVIOUS_DIR / "meta.json", None)
    if not isinstance(meta, dict) or not meta.get("version"):
        raise UpdateError("there is no previous version to go back to (nothing has been updated yet)")
    problems = blockers(True)
    if problems:
        raise UpdateError(problems[0])
    lifecycle, cmd_reboot = _lazy_modules()
    _begin("rollback", installed_version(), meta["version"])
    threading.Thread(target=_run_rollback, args=(meta["version"], lifecycle, cmd_reboot._reload),
                     daemon=False, name="rconsole-rollback").start()


def _run_rollback(target, lifecycle, reload_fn):
    try:
        _step("stop", 40, "stopping services, jobs and terminals and taking a final backup")
        lifecycle.begin_shutdown()
        _step("install", 80, f"restoring rconsole {target}")
        broken = installed_version()
        restore_previous()
        state = read_state()
        state["bad"] = sorted(set((state.get("bad") or []) + [broken]))
        state["active"], state["previous_active"], state["pending"] = state.get("previous_active"), None, None
        write_state(state)
        _prune(state)
        _step("restart", 97, f"rolled back to {target}; restarting")
        _record_last(True, f"rolled back {broken} -> {target}")
    except Exception as e:
        msg = str(e) if isinstance(e, UpdateError) else f"{type(e).__name__}: {e}"
        with _lock:
            _live["error"] = msg
        _step("failed", 97, f"FAILED: {msg}")
        _record_last(False, msg)
    _reload_and_unlock_later(reload_fn)


# --------------------------------------------------------------------------- after a restart
def confirm_boot():
    """The new process stayed up: the update is good (called by the app a while after it started)."""
    state = read_state()
    if state.get("pending"):
        state["pending"] = None
        try:
            write_state(state)
        except OSError:
            pass


def schedule_confirm():
    """Called at import time by app.py: confirm a pending update once this process has proven stable."""
    if read_state().get("pending"):
        t = threading.Timer(CONFIRM_AFTER, confirm_boot)
        t.daemon = True
        t.start()


def sync(log=print):
    """Container start (docker-entrypoint.sh, before the server): keep the program files consistent.

    * a new version that keeps failing to start (``MAX_BOOTS``) is rolled back;
    * a verified release kept in the data directory is re-applied when the image holds an older one
      (a redeploy rebuilt the image from older code)."""
    state = read_state()
    if not state:
        return
    pending = state.get("pending")
    if pending:
        pending["boots"] = int(pending.get("boots") or 0) + 1
        write_state(state)
        if pending["boots"] > MAX_BOOTS:
            broken = installed_version()
            target = restore_previous()
            state["bad"] = sorted(set((state.get("bad") or []) + [broken]))
            state["active"], state["previous_active"], state["pending"] = state.get("previous_active"), None, None
            state["last"] = {"ok": False, "from": broken, "to": target, "mode": "rollback", "at": time.time(),
                             "message": f"{broken} failed to start {MAX_BOOTS} times; restored {target}"}
            write_state(state)
            log(f"[rconsole] {state['last']['message']}")
            return
    active = state.get("active")
    if not active:
        return
    want, have = parse_version(active.get("version")), parse_version(installed_version())
    tree = RELEASES_DIR / str(active.get("version"))
    if want and have and want > have and tree.is_dir() and read_version(tree) == active["version"]:
        try:
            apply_tree(tree, installed_version(), active["version"])
        except UpdateError as e:
            log(f"[rconsole] could not re-apply {active['version']}: {e}")
            return
        state = read_state()
        state["pending"] = {"version": active["version"], "boots": 0, "since": time.time()}
        write_state(state)
        _sync_vmtools(tree)
        log(f"[rconsole] re-applied rconsole {active['version']} over the image's {installed_version()}")
    elif want and have and want <= have:
        # the image is as new or newer than the saved release: the overlay has done its job
        if want < have:
            state["active"] = None
            write_state(state)
        _prune(read_state())


def restart_server():
    """Ask the server to restart itself: SIGHUP to the gunicorn master, else re-exec this process."""
    ppid = os.getppid()
    try:
        gunicorn = b"gunicorn" in Path(f"/proc/{ppid}/cmdline").read_bytes()
    except OSError:
        gunicorn = False
    if gunicorn:
        os.kill(ppid, signal.SIGHUP)
    else:
        os.execv(sys.executable, [sys.executable] + sys.argv)


def after_restore(log=print, restart=None):
    """Boot, right after a backup was restored onto a blank disk: if the restored update state names a
    newer release than the code this (older) image started with, apply it and restart once to load it.
    Returns True when a restart was requested."""
    before = installed_version()
    sync(log)
    if installed_version() == before:
        return False
    log(f"[rconsole] restoring the update to {installed_version()}: restarting once to load it")
    (restart or restart_server)()
    return True


# --------------------------------------------------------------------------- command line
def _cli(argv):
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "sync":
            sync(lambda m: print(m, file=sys.stderr, flush=True))
        elif cmd == "check":
            info = check()
            print(f"installed {info['current']}, latest {info['latest']}"
                  f"{' — update available' if info['newer'] else ' — up to date'}")
        elif cmd == "rollback":
            target = restore_previous()
            if not target:
                print("nothing to roll back to", file=sys.stderr)
                return 1
            state = read_state()
            state["active"], state["previous_active"], state["pending"] = state.get("previous_active"), None, None
            write_state(state)
            print(f"restored rconsole {target}; restart the server")
        else:
            print(json.dumps(status(True) | {"state": read_state()}, indent=2, default=str))
    except UpdateError as e:
        print(f"updater: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
