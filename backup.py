"""Encrypted backups so free, disk-less hosting can survive redeploys.

Free instances (Render's free tier, ...) have no persistent disk: every deploy
or restart gives you a blank machine. rconsole cannot change that, but it can
make the loss small. It periodically snapshots **the things that matter** and
pushes them, encrypted, to a place that does persist:

* a **private git repository** (GitHub, GitLab, Codeberg, ... — free), or
* any **folder** (a mounted disk, a synced directory).

and, when it boots on a *fresh* instance (no ``users.json``), pulls the snapshot
back before anything else starts. After that the VMs you had are reinstalled in
the background (and your ``vm setup`` toolchains re-applied), and your services
start again — see ``services.py``.

What is in a snapshot
---------------------
users and secrets, each user's saved state, the **workspace** (host-mode working
directory), exposures and services, **each VM's ``/root`` home** and the *list* of
VMs and setup profiles.
What is **not**: VM system files (``rootfs`` — re-downloaded, and large), caches,
logs, ``node_modules``/virtualenvs, files over 20 MB, and running processes.

Security
--------
Snapshots contain password hashes and API keys, so they are **always encrypted**
(Fernet/AES with a key derived from ``RCONSOLE_BACKUP_PASSPHRASE``) unless you
explicitly set ``RCONSOLE_BACKUP_ENCRYPT=0``. Keep the passphrase somewhere other
than the repo: without it a backup cannot be read, by anyone.

Git history never accumulates: each push replaces the branch with a single
commit (``--force``), so the repository stays as small as one snapshot.
"""
import base64
import hashlib
import os
import shutil
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import config

MAGIC_ENC = b"RCBK1\n"
MAGIC_PLAIN = b"RCBK0\n"
BLOB = "backup.rcbk"
CHUNK = 4 * 1024 * 1024
PBKDF2_ITERATIONS = 200_000
MAX_FILE_BYTES = 20 * 1024 * 1024
GIT_TIMEOUT = 120

INCLUDE_TOP = ("users.json", "secret.key", "pepper.key", "exposed.json", "services.json")
# Directory names never worth backing up (rebuildable caches / dependency trees).
SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".cache", ".npm", ".rustup",
             ".gradle", ".m2", "site-packages"}
SKIP_REL = (".local/share/uv", ".cargo/registry", ".cargo/git", "go/pkg",
            ".bun/install/cache", ".local/share/pnpm")


class BackupError(Exception):
    """A backup/restore could not be completed (message is user-readable)."""


_lock = threading.RLock()
_status = {"last_ok": None, "last_time": None, "last_error": "", "last_size": 0,
           "last_files": 0, "last_sig": None, "restored": None, "count": 0}
_thread = None
_stop = threading.Event()


# -------------------------------------------------------------------- config
def configured():
    return bool(config.BACKUP_URL or config.BACKUP_DIR)


def problems():
    """Reasons backups cannot work with the current configuration."""
    out = []
    if not configured():
        out.append("no destination: set RCONSOLE_BACKUP_URL (a private git repo) or RCONSOLE_BACKUP_DIR")
    if config.BACKUP_ENCRYPT and not config.BACKUP_PASSPHRASE:
        out.append("RCONSOLE_BACKUP_PASSPHRASE is not set (backups hold password hashes and API "
                   "keys, so they are encrypted; choose a long passphrase and keep a copy elsewhere)")
    if config.BACKUP_URL and not shutil.which("git"):
        out.append("`git` is not installed on this server")
    return out


def describe_target():
    if config.BACKUP_URL:
        return _scrub(config.BACKUP_URL) + f" (branch {config.BACKUP_BRANCH})"
    if config.BACKUP_DIR:
        return f"folder {config.BACKUP_DIR}"
    return "not configured"


def _scrub(text):
    """Remove secrets (token, passphrase, URL credentials) from a message."""
    text = str(text)
    for secret in (config.BACKUP_TOKEN, config.BACKUP_PASSPHRASE):
        if secret:
            text = text.replace(secret, "***")
    if config.BACKUP_TOKEN:
        b64 = base64.b64encode(f"{config.BACKUP_USER}:{config.BACKUP_TOKEN}".encode()).decode()
        text = text.replace(b64, "***")
    import re
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)


# --------------------------------------------------------------- what to save
def _skip_path(rel):
    """Is the directory ``rel`` (relative to the walked root) a rebuildable cache?"""
    return any(rel == p or rel.startswith(p + "/") for p in SKIP_REL)


def iter_files():
    """Yield ``(archive_name, path)`` for everything that belongs in a snapshot."""
    d = config.DATA_DIR
    for name in INCLUDE_TOP:
        p = d / name
        if p.is_file():
            yield name, p
    for sub in ("state", "workspace"):
        yield from _walk(d / sub, sub)
    vm = d / "vm"
    try:
        users = [u for u in vm.iterdir() if u.is_dir() and u.name not in ("bin", "cache", "tmp")]
    except OSError:
        users = []
    for u in users:
        for v in (x for x in u.iterdir() if x.is_dir()):
            base = f"vm/{u.name}/{v.name}"
            for meta in ("distro.json", "setup.json"):
                if (v / meta).is_file():
                    yield f"{base}/{meta}", v / meta
            yield from _walk(v / "home", f"{base}/home")


def _walk(root, arc_root):
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(n for n in dirnames
                             if n not in SKIP_DIRS and not _skip_path(prefix + n))
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            try:
                if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            yield f"{arc_root}/{rel}", p


def signature():
    """Cheap fingerprint of the snapshot contents (to skip no-change backups)."""
    h = hashlib.sha256()
    n = 0
    for arc, p in sorted(iter_files()):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{arc}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
        n += 1
    return h.hexdigest(), n


# ----------------------------------------------------------------- crypto
def _fernet(passphrase, salt):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8"))))


def encrypt_file(src, dst, passphrase):
    """Chunked Fernet encryption (constant memory even for big snapshots)."""
    salt = os.urandom(16)
    f = _fernet(passphrase, salt)
    with open(src, "rb") as i, open(dst, "wb") as o:
        o.write(MAGIC_ENC + salt)
        for chunk in iter(lambda: i.read(CHUNK), b""):
            tok = f.encrypt(chunk)
            o.write(struct.pack(">I", len(tok)) + tok)


def decrypt_file(src, dst, passphrase):
    from cryptography.fernet import InvalidToken
    with open(src, "rb") as i, open(dst, "wb") as o:
        head = i.read(len(MAGIC_ENC))
        if head == MAGIC_PLAIN:
            shutil.copyfileobj(i, o)
            return
        if head != MAGIC_ENC:
            raise BackupError("not an rconsole backup (unknown file format)")
        if not passphrase:
            raise BackupError("this backup is encrypted: set RCONSOLE_BACKUP_PASSPHRASE")
        f = _fernet(passphrase, i.read(16))
        try:
            while True:
                raw = i.read(4)
                if not raw:
                    break
                (n,) = struct.unpack(">I", raw)
                o.write(f.decrypt(i.read(n)))
        except (InvalidToken, struct.error):
            raise BackupError("cannot decrypt the backup: wrong RCONSOLE_BACKUP_PASSPHRASE "
                              "or a damaged file") from None


# ---------------------------------------------------------------- backends
class DirBackend:
    def __init__(self, path):
        self.path = Path(path)

    def push(self, blob):
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.path / (BLOB + ".part")
        shutil.copyfile(blob, tmp)
        os.replace(tmp, self.path / BLOB)

    def pull(self, workdir):
        src = self.path / BLOB
        if not src.is_file():
            return None
        dst = Path(workdir) / BLOB
        shutil.copyfile(src, dst)
        return dst


class GitBackend:
    """Push/pull the blob as the single commit of one branch of a git remote."""

    def __init__(self, url, token="", user="x-access-token", branch="rconsole-backup"):
        self.url, self.token, self.user, self.branch = url, token, user, branch

    def _git(self, args, cwd=None, timeout=GIT_TIMEOUT):
        cmd = ["git"]
        if self.token and self.url.startswith("https://"):
            auth = base64.b64encode(f"{self.user}:{self.token}".encode()).decode()
            cmd += ["-c", f"http.extraheader=Authorization: Basic {auth}"]
        cmd += args
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true")
        try:
            return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BackupError(f"git timed out after {timeout}s") from None
        except OSError as e:
            raise BackupError(f"cannot run git: {e}") from None

    def push(self, blob):
        with tempfile.TemporaryDirectory(prefix="rc-bk-") as tmp:
            self._must(self._git(["init", "-q", "-b", self.branch], cwd=tmp), "git init")
            shutil.copyfile(blob, Path(tmp) / BLOB)
            (Path(tmp) / "README.md").write_text(
                "# rconsole backup\n\nEncrypted snapshot written by rconsole. "
                "Do not edit; restore with `sudo backup restore --yes`.\n")
            self._must(self._git(["add", "-A"], cwd=tmp), "git add")
            self._must(self._git(["-c", "user.name=rconsole", "-c", "user.email=rconsole@localhost",
                                  "commit", "-q", "-m", f"backup {time.strftime('%Y-%m-%d %H:%M:%S')}"],
                                 cwd=tmp), "git commit")
            self._must(self._git(["push", "-q", "--force", self.url,
                                  f"HEAD:refs/heads/{self.branch}"], cwd=tmp), "git push")

    def pull(self, workdir, timeout=GIT_TIMEOUT):
        repo = Path(workdir) / "repo"
        r = self._git(["clone", "-q", "--depth", "1", "--branch", self.branch, self.url, str(repo)],
                      timeout=timeout)
        if r.returncode != 0:
            err = (r.stderr or "").lower()
            if "remote branch" in err or "couldn't find remote ref" in err or "empty repository" in err:
                return None                                  # nothing backed up yet
            raise BackupError("git clone failed: " + _scrub((r.stderr or "").strip()[:300]))
        blob = repo / BLOB
        return blob if blob.is_file() else None

    @staticmethod
    def _must(r, what):
        if r.returncode != 0:
            raise BackupError(f"{what} failed: " + _scrub((r.stderr or r.stdout or "").strip()[:300]))


def backend():
    if config.BACKUP_URL:
        return GitBackend(config.BACKUP_URL, config.BACKUP_TOKEN, config.BACKUP_USER,
                          config.BACKUP_BRANCH)
    if config.BACKUP_DIR:
        return DirBackend(config.BACKUP_DIR)
    raise BackupError("backups are not configured")


# ----------------------------------------------------------------- backup
def make_archive(dest):
    """Write the snapshot tarball to ``dest``. Returns ``(files, bytes_uncompressed)``."""
    files = list(iter_files())
    total = 0
    with tarfile.open(dest, "w:gz", compresslevel=6) as tf:
        for arc, p in files:
            try:
                tf.add(p, arcname=arc, recursive=False)
                total += p.stat().st_size
            except OSError:
                continue
    return len(files), total


def backup_now(force=False, reason="manual"):
    """Snapshot and upload. Returns ``(ok, message)`` — never raises."""
    with _lock:
        bad = problems()
        if bad:
            return False, "; ".join(bad)
        try:
            sig, nfiles = signature()
            if not force and sig == _status["last_sig"]:
                return True, "no changes since the last backup"
            t0 = time.time()
            with tempfile.TemporaryDirectory(prefix="rc-bk-") as tmp:
                tar = Path(tmp) / "snapshot.tar.gz"
                n, raw = make_archive(tar)
                if tar.stat().st_size > config.BACKUP_MAX_MB * 1024 * 1024:
                    raise BackupError(
                        f"the snapshot is {tar.stat().st_size // (1024 * 1024)} MB, over the "
                        f"{config.BACKUP_MAX_MB} MB limit (RCONSOLE_BACKUP_MAX_MB). "
                        f"Largest: {_biggest()}. Move big files out of /root and the workspace.")
                blob = Path(tmp) / BLOB
                if config.BACKUP_ENCRYPT:
                    encrypt_file(tar, blob, config.BACKUP_PASSPHRASE)
                else:
                    with open(blob, "wb") as o, open(tar, "rb") as i:
                        o.write(MAGIC_PLAIN)
                        shutil.copyfileobj(i, o)
                size = blob.stat().st_size
                backend().push(blob)
            _status.update(last_ok=True, last_time=time.time(), last_error="", last_size=size,
                           last_files=n, last_sig=sig, count=_status["count"] + 1)
            return True, (f"backed up {n} files ({size // 1024} KB, encrypted) to "
                          f"{describe_target()} in {time.time() - t0:.1f}s [{reason}]")
        except BackupError as e:
            msg = _scrub(str(e))
        except Exception as e:                      # never let a backup crash the server
            msg = _scrub(f"{type(e).__name__}: {e}")
        _status.update(last_ok=False, last_time=time.time(), last_error=msg)
        return False, msg


def _biggest(n=3):
    sizes = {}
    for arc, p in iter_files():
        top = "/".join(arc.split("/")[:4 if arc.startswith("vm/") else 2])
        try:
            sizes[top] = sizes.get(top, 0) + p.stat().st_size
        except OSError:
            pass
    top = sorted(sizes.items(), key=lambda kv: -kv[1])[:n]
    return ", ".join(f"{k} ({v // (1024 * 1024)} MB)" for k, v in top) or "n/a"


# ---------------------------------------------------------------- restore
_ALLOWED_TOP = set(INCLUDE_TOP)


def _allowed(name):
    parts = name.split("/")
    if name.startswith("/") or ".." in parts or "" in parts[:-1]:
        return False
    if name in _ALLOWED_TOP:
        return True
    if parts[0] in ("state", "workspace") and len(parts) >= 2:
        return True
    if parts[0] == "vm" and len(parts) >= 4:
        return parts[3] in ("distro.json", "setup.json", "home")
    return False


def restore(force=False, timeout=GIT_TIMEOUT):
    """Download the latest snapshot and unpack it into the data dir.

    ``force=False`` (boot): never overwrite an existing file.
    ``force=True``  (``backup restore --yes``): overwrite.
    Returns ``(ok, message)`` — never raises.
    """
    with _lock:
        bad = problems()
        if bad:
            return False, "; ".join(bad)
        try:
            with tempfile.TemporaryDirectory(prefix="rc-rs-") as tmp:
                be = backend()
                blob = be.pull(tmp, timeout=timeout) if isinstance(be, GitBackend) else be.pull(tmp)
                if blob is None:
                    return False, "no backup found yet at " + describe_target()
                tar = Path(tmp) / "snapshot.tar.gz"
                decrypt_file(blob, tar, config.BACKUP_PASSPHRASE)
                n = skipped = 0
                config.ensure_private_dir(config.DATA_DIR)
                with tarfile.open(tar, "r:gz") as tf:
                    for m in tf:
                        if not m.isfile() or not _allowed(m.name):
                            continue
                        dest = config.DATA_DIR / m.name
                        if dest.exists() and not force:
                            skipped += 1
                            continue
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        src = tf.extractfile(m)
                        part = dest.with_name(dest.name + ".rcpart")
                        with open(part, "wb") as o:
                            shutil.copyfileobj(src, o)
                        os.chmod(part, 0o600 if m.name.endswith((".key", "users.json")) else 0o644)
                        os.replace(part, dest)
                        n += 1
            _status.update(restored=time.time())
            return True, f"restored {n} files from {describe_target()}" + \
                (f" ({skipped} already present, kept)" if skipped else "")
        except BackupError as e:
            return False, _scrub(str(e))
        except (OSError, tarfile.TarError) as e:
            return False, _scrub(f"restore failed: {e}")
        except Exception as e:
            return False, _scrub(f"{type(e).__name__}: {e}")


def restore_if_fresh(log=print):
    """Boot hook: on a blank instance (no users file) pull the last snapshot back."""
    if not configured() or (config.DATA_DIR / "users.json").exists():
        return None
    log("[rconsole] fresh instance: restoring the last backup…")
    ok, msg = restore(force=False, timeout=90)
    log(f"[rconsole] restore: {msg}")
    return ok


# ------------------------------------------------------------ periodic + exit
def _loop():
    if _stop.wait(min(120, config.BACKUP_INTERVAL)):
        return
    while not _stop.is_set():
        backup_now(reason="scheduled")
        if _stop.wait(config.BACKUP_INTERVAL):
            return


def start_background():
    """Start the periodic backup thread (idempotent)."""
    global _thread
    with _lock:
        if not configured() or (_thread and _thread.is_alive()):
            return bool(_thread and _thread.is_alive())
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="rconsole-backup", daemon=True)
        _thread.start()
        return True


def final(timeout=25):
    """Last-chance backup when the server is shutting down (deploys send SIGTERM)."""
    if not configured() or problems():
        return None
    result = []
    t = threading.Thread(target=lambda: result.append(backup_now(reason="shutdown")), daemon=True)
    t.start()
    t.join(timeout)
    return result[0] if result else (False, f"timed out after {timeout}s")


def status():
    with _lock:
        s = dict(_status)
    s.update(configured=configured(), target=describe_target(), problems=problems(),
             interval=config.BACKUP_INTERVAL, encrypted=config.BACKUP_ENCRYPT,
             running=bool(_thread and _thread.is_alive()))
    return s
