"""Repo storage: keep your files in the GitHub backup repo instead of on the host's disk.

Free hosts give you a small, throw-away disk. With ``RCONSOLE_STORAGE=github`` (or the dashboard switch) the
files you create live in the backup repository and the disk only *caches* them:

* what is stored: the **workspace** (the host shell's home) and each VM's ``/home``, ``/srv`` and ``/data``.
  System folders (``/root``, ``/etc``, ``/proc``, ``/app``, ``/usr`` ...) never leave the host: they take
  almost no room. ``node_modules``, virtualenvs and caches stay on the host as well (they are rebuilt).
* how: every file is cut into pieces of at most ``RCONSOLE_STORAGE_CHUNK_MB`` (default 50, hard maximum 95:
  GitHub refuses files over 100 MB), each piece is stored once under its content hash (so identical data is
  stored once, and appending to a log re-sends only the last piece), and a **manifest** maps each path to its
  pieces. The manifest and the pieces are encrypted (AES-256-GCM, key from the passphrase) unless you turn
  encryption off, in which case they are plain.
* the host disk: small files are simply there. Big files (``RCONSOLE_STORAGE_STUB_MB`` and up) that have been
  uploaded may be **evicted** when the cache is full (``RCONSOLE_STORAGE_CACHE_MB``): the file stays in its
  folder as a tiny *placeholder* that says where the real data is, and it is fetched back (all pieces, joined)
  the moment anything opens, downloads, previews, edits or names it in a command. A restart or a fresh
  deploy needs nothing but the repo: the folder tree comes back, small files at once, big ones as placeholders.
* how it talks to git: plain ``git`` (partial clones, one push per batch). No daemon, no FUSE, any git host.
  Each push replaces the branch with a single commit, so the repository never accumulates history.

If storage is ``host`` (the default), or the repository is not set up, **nothing here does anything** and
files behave exactly as before.

Files and folders have the same key in the manifest whatever created them: ``host/<path below the workspace>``
or ``vm/<user>/<system>/<guest path>``.
"""
import base64
import contextlib
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import stat as _stat
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import config

STUB_MAGIC = b"#RCONSOLE-REMOTE-FILE v1\n"
ENC_MAGIC = b"RCE1"
FRAME = 4 * 1024 * 1024
PBKDF2_ITER = 200_000
GIT_TIMEOUT = 180
BATCH_BYTES = 150 * 1024 * 1024         # most new data sent in one push (bounds the temporary disk use)
HOT_SECONDS = 2.0                        # a file written this recently is still being written: wait
VM_PREFIXES = ("home", "srv", "data")    # folders of a VM's root filesystem that are stored in the repo
SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".cache", ".npm", ".rustup", ".gradle", ".m2",
             "site-packages"}
SKIP_SUFFIX = (".rcpart", ".rcstub")


class StoreError(Exception):
    """A storage operation failed (message is user-readable)."""


_lock = threading.RLock()               # state and manifest
_sync_lock = threading.Lock()           # one sync at a time
_key_locks = {}
_wake = threading.Event()
_stop = threading.Event()
_thread = None
_booted = False
_hydrating_all = False
_blocked = ""                            # why syncing is paused (e.g. the repo could not be reached at boot)
_S = {"state": "idle", "message": "", "done": 0, "total": 0, "last_ok": None, "last_error": "", "last_time": None,
      "pushes": 0, "uploaded": 0, "hydrated": 0, "evicted": 0, "remote_changed": False}
STATE = {"head": None, "man": {}, "store": {}, "local": {}, "blobs": [], "pins": [], "synced_roots": []}
STUBS = {}                               # key -> real size, for placeholders on this disk
_last_up = {}


# ----------------------------------------------------------------------------- availability
def git_ok():
    return bool(shutil.which("git"))


def problems():
    out = []
    if not config.BACKUP_URL:
        out.append("no repository: set RCONSOLE_BACKUP_URL (a private git repo) and RCONSOLE_BACKUP_TOKEN")
    if config.STORAGE_ENCRYPT and not config.BACKUP_PASSPHRASE:
        out.append("encryption is on but there is no passphrase (RCONSOLE_BACKUP_PASSPHRASE); "
                   "set one or turn encryption off")
    if not git_ok():
        out.append("`git` is not installed on this server")
    if config.STORAGE_BRANCH == config.BACKUP_BRANCH:
        out.append("the storage branch must differ from the backup branch")
    return out


def wanted():
    return config.STORAGE == "github"


def active():
    """Is repo storage doing anything right now? (False = every hook is a no-op: files live on the host.)"""
    return (wanted() or _hydrating_all) and not problems()


def status():
    with _lock:
        man = STATE["man"]
        files = sum(1 for e in man.values() if e.get("t") == "f")
        size = sum(e.get("s", 0) for e in man.values() if e.get("t") == "f")
        pieces = len({c[0] for e in man.values() for c in e.get("c", ())})
        local_bytes = _cache_bytes()
        s = dict(_S)
        s.update(mode=config.STORAGE, wanted=wanted(), active=active(), problems=problems(), blocked=_blocked,
                 encrypted=config.STORAGE_ENCRYPT, files=files, bytes=size, pieces=pieces,
                 placeholders=len(STUBS), placeholder_bytes=sum(STUBS.values()), cache_bytes=local_bytes,
                 cache_limit=config.STORAGE_CACHE_MB * 1024 * 1024, chunk_mb=config.STORAGE_CHUNK_MB,
                 branch=config.STORAGE_BRANCH, repo=_scrub(config.BACKUP_URL), interval=config.STORAGE_INTERVAL,
                 running=bool(_thread and _thread.is_alive()), head=(STATE["head"] or "")[:10],
                 pinned=len(STATE["pins"]), hydrating_all=_hydrating_all)
    return s


def _scrub(text):
    text = str(text)
    for secret in (config.BACKUP_TOKEN, config.BACKUP_PASSPHRASE):
        if secret:
            text = text.replace(secret, "***")
    if config.BACKUP_TOKEN:
        b64 = base64.b64encode(f"{config.BACKUP_USER}:{config.BACKUP_TOKEN}".encode()).decode()
        text = text.replace(b64, "***")
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)


def _set(**kw):
    with _lock:
        _S.update(kw)


# ----------------------------------------------------------------------------- crypto
_key_cache = {}


def _derive(passphrase, salt, iterations):
    ck = (passphrase, salt, iterations)
    if ck not in _key_cache:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        raw = PBKDF2HMAC(algorithm=hashes.SHA256(), length=64, salt=salt,
                         iterations=iterations).derive(passphrase.encode("utf-8"))
        _key_cache.clear()
        _key_cache[ck] = (raw[:32], raw[32:])
    return _key_cache[ck]


def _aes(key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def _seal(key, aad, data):
    nonce = os.urandom(12)
    return nonce + _aes(key).encrypt(nonce, data, aad)


def _open(key, aad, blob):
    return _aes(key).decrypt(blob[:12], blob[12:], aad)


def _keys(create=False):
    """``(enc_key, mac_key)`` for the repo, verifying the passphrase against the repo's check value."""
    store = STATE["store"]
    if not config.BACKUP_PASSPHRASE:
        raise StoreError("a passphrase is needed (RCONSOLE_BACKUP_PASSPHRASE) to read or write encrypted files")
    if not store.get("salt"):
        if not create:
            raise StoreError("the repository has no encryption set up")
        store["salt"] = secrets.token_hex(16)
        store["iter"] = PBKDF2_ITER
        enc, mac = _derive(config.BACKUP_PASSPHRASE, bytes.fromhex(store["salt"]), store["iter"])
        store["check"] = _seal(enc, b"check", b"rconsole-storage").hex()
        return enc, mac
    enc, mac = _derive(config.BACKUP_PASSPHRASE, bytes.fromhex(store["salt"]), int(store.get("iter", PBKDF2_ITER)))
    try:
        if _open(enc, b"check", bytes.fromhex(store["check"])) != b"rconsole-storage":
            raise ValueError
    except Exception:
        raise StoreError("wrong passphrase for the files in the repository "
                         "(RCONSOLE_BACKUP_PASSPHRASE does not match the one they were stored with)") from None
    return enc, mac


def _hash_new():
    return hashlib.sha256()


def _chunk_id(h_or_mac):
    return h_or_mac.hexdigest()


def _encode_frames(key, aad, reader, out):
    out.write(ENC_MAGIC)
    while True:
        buf = reader(FRAME)
        if not buf:
            break
        sealed = _seal(key, aad, buf)
        out.write(struct.pack(">I", len(sealed)) + sealed)


def _decode_frames(key, aad, src, sink):
    from cryptography.exceptions import InvalidTag
    if src.read(4) != ENC_MAGIC:
        raise StoreError("a stored piece is not in the expected format")
    try:
        while True:
            raw = src.read(4)
            if not raw:
                return
            (n,) = struct.unpack(">I", raw)
            body = src.read(n)
            if len(body) != n:
                raise StoreError("a stored piece is truncated")
            sink(_open(key, aad, body))
    except InvalidTag:
        raise StoreError("a stored piece could not be decrypted (wrong passphrase or damaged data)") from None


# ----------------------------------------------------------------------------- git plumbing
def _git_env():
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", LC_ALL="C")
    for k in config.SECRET_ENV_VARS:
        env.pop(k, None)
    return env


def _git(args, cwd=None, timeout=GIT_TIMEOUT, input=None, stdout=subprocess.PIPE, text=True):
    cmd = ["git"]
    url = config.BACKUP_URL
    if config.BACKUP_TOKEN and url.startswith("https://"):
        auth = base64.b64encode(f"{config.BACKUP_USER}:{config.BACKUP_TOKEN}".encode()).decode()
        cmd += ["-c", f"http.extraheader=Authorization: Basic {auth}"]
    cmd += ["-c", "core.compression=0", "-c", "pack.compression=0", "-c", "gc.auto=0",
            "-c", "protocol.version=2", "-c", "advice.detachedHead=false"]
    cmd += args
    try:
        return subprocess.run(cmd, cwd=cwd, env=_git_env(), input=input, stdout=stdout, stderr=subprocess.PIPE,
                              text=text, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StoreError(f"git timed out after {timeout}s") from None
    except OSError as e:
        raise StoreError(f"cannot run git: {e}") from None


def _must(r, what):
    if r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode("utf-8", "replace")
        raise StoreError(f"{what} failed: " + _scrub(err.strip()[-400:]))
    return r


@contextlib.contextmanager
def _scratch(prefix="rc-st-"):
    base = config.STORAGE_DIR / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    d = tempfile.mkdtemp(prefix=prefix, dir=base)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class Reader:
    """A throw-away partial clone of the storage branch: trees only, file data fetched on demand."""

    def __init__(self, limit_bytes=None):
        self.limit = limit_bytes
        self.dir = None
        self.tip = None
        self._cm = None

    def __enter__(self):
        self._cm = _scratch()
        self.dir = self._cm.__enter__()
        try:
            self._open()
        except BaseException:
            self._cm.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)

    def _g(self, args, **kw):
        return _git(args, cwd=self.dir, **kw)

    def _open(self):
        _must(self._g(["init", "-q"]), "git init")
        self._g(["config", "core.repositoryformatversion", "1"])
        self._g(["config", "extensions.partialClone", "origin"])
        self._g(["config", "remote.origin.url", config.BACKUP_URL])
        self._g(["config", "remote.origin.promisor", "true"])
        spec = f"blob:limit={self.limit}" if self.limit else "blob:none"
        self._g(["config", "remote.origin.partialclonefilter", spec])
        ref = f"+refs/heads/{config.STORAGE_BRANCH}:refs/rconsole/tip"
        r = self._g(["fetch", "-q", "--no-tags", "--depth=1", f"--filter={spec}", "origin", ref])
        if r.returncode != 0:
            err = (r.stderr or "").lower()
            if "couldn't find remote ref" in err or "empty repository" in err or "not found in upstream" in err:
                return                                           # nothing stored yet
            raise StoreError("could not reach the repository: " + _scrub((r.stderr or "").strip()[-300:]))
        r = self._g(["rev-parse", "refs/rconsole/tip"])
        self.tip = r.stdout.strip() if r.returncode == 0 else None

    def read(self, path):
        """The bytes of a (small) file in the tip tree, or ``None``."""
        if not self.tip:
            return None
        r = self._g(["cat-file", "blob", f"{self.tip}:{path}"], text=False)
        return r.stdout if r.returncode == 0 else None

    def stream(self, path, sink):
        """Copy a file of the tip tree to ``sink(bytes)`` without holding it in memory."""
        if not self.tip:
            raise StoreError("the repository holds no stored files yet")
        cmd = ["git"]
        if config.BACKUP_TOKEN and config.BACKUP_URL.startswith("https://"):
            auth = base64.b64encode(f"{config.BACKUP_USER}:{config.BACKUP_TOKEN}".encode()).decode()
            cmd += ["-c", f"http.extraheader=Authorization: Basic {auth}"]
        cmd += ["cat-file", "blob", f"{self.tip}:{path}"]
        p = subprocess.Popen(cmd, cwd=self.dir, env=_git_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            while True:
                buf = p.stdout.read(1024 * 1024)
                if not buf:
                    break
                sink(buf)
        finally:
            err = b""
            try:
                p.stdout.close()
                err = p.stderr.read()
            finally:
                p.stderr.close()
                p.wait()
        if p.returncode != 0:
            raise StoreError("could not fetch a stored piece: " + _scrub(err.decode("utf-8", "replace").strip()[-300:]))

    def batch(self, paths):
        """``{path: bytes}`` for many small files with a single ``cat-file --batch``."""
        out = {}
        if not self.tip or not paths:
            return out
        cmd = ["git"]
        if config.BACKUP_TOKEN and config.BACKUP_URL.startswith("https://"):
            auth = base64.b64encode(f"{config.BACKUP_USER}:{config.BACKUP_TOKEN}".encode()).decode()
            cmd += ["-c", f"http.extraheader=Authorization: Basic {auth}"]
        cmd += ["cat-file", "--batch"]
        p = subprocess.Popen(cmd, cwd=self.dir, env=_git_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)

        def feed():
            try:
                for path in paths:
                    p.stdin.write(f"{self.tip}:{path}\n".encode())
                p.stdin.close()
            except OSError:
                pass
        t = threading.Thread(target=feed, daemon=True)
        t.start()
        try:
            for path in paths:
                head = p.stdout.readline().decode("utf-8", "replace").split()
                if len(head) == 3 and head[1] == "blob":
                    n = int(head[2])
                    data = b""
                    while len(data) < n:
                        part = p.stdout.read(n - len(data))
                        if not part:
                            break
                        data += part
                    p.stdout.read(1)
                    out[path] = data
        finally:
            t.join(5)
            p.stdout.close()
            p.wait()
        return out

    def ls(self):
        """All file paths of the tip tree."""
        if not self.tip:
            return []
        r = self._g(["ls-tree", "-r", "--name-only", "-z", self.tip])
        return [x for x in r.stdout.split("\0") if x] if r.returncode == 0 else []


def push_commit(add, remove, extra, message):
    """Replace the storage branch with ONE commit: its current tree + ``add`` (``{repo path: local file}``)
    + ``extra`` (``{repo path: bytes}``) - ``remove``. Local ``add`` files are consumed."""
    total = sum(os.path.getsize(p) for p in add.values()) if add else 0
    with _scratch("rc-push-") as d:
        def g(args, **kw):
            return _git(args, cwd=d, **kw)
        _must(g(["init", "-q"]), "git init")
        g(["config", "user.name", "rconsole"])
        g(["config", "user.email", "rconsole@localhost"])
        _must(g(["remote", "add", "origin", config.BACKUP_URL]), "git remote")
        g(["config", "core.repositoryformatversion", "1"])
        g(["config", "extensions.partialClone", "origin"])
        g(["config", "remote.origin.promisor", "true"])
        g(["config", "remote.origin.partialclonefilter", "blob:none"])
        r = g(["fetch", "-q", "--no-tags", "--depth=1", "--filter=blob:none", "origin",
               f"+refs/heads/{config.STORAGE_BRANCH}:refs/rconsole/tip"])
        have_tip = r.returncode == 0
        if not have_tip:
            err = (r.stderr or "").lower()
            if not ("couldn't find remote ref" in err or "empty repository" in err or "not found in upstream" in err):
                raise StoreError("could not reach the repository: " + _scrub((r.stderr or "").strip()[-300:]))
        else:
            _must(g(["read-tree", "refs/rconsole/tip"]), "git read-tree")
        for path, src in add.items():
            dst = Path(d) / path
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(src, dst)
            except OSError:
                shutil.move(src, dst)
        for path, data in (extra or {}).items():
            dst = Path(d) / path
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        paths = list(add) + list(extra or {})
        for i in range(0, len(paths), 400):
            _must(g(["add", "-f", "--"] + paths[i:i + 400]), "git add")
        for path in list(add) + list(extra or {}):
            with contextlib.suppress(OSError):
                os.unlink(Path(d) / path)
        if remove:
            for i in range(0, len(remove), 400):
                g(["rm", "--cached", "-q", "--ignore-unmatch", "--"] + list(remove[i:i + 400]))
        tree = _must(g(["write-tree"]), "git write-tree").stdout.strip()
        env_msg = ["-c", "user.name=rconsole", "-c", "user.email=rconsole@localhost"]
        commit = _must(g(env_msg + ["commit-tree", tree, "-m", message]), "git commit").stdout.strip()
        timeout = max(300, int(total / (1024 * 1024) * 4))
        _must(g(["push", "-q", "--force", "origin", f"{commit}:refs/heads/{config.STORAGE_BRANCH}"],
                timeout=timeout), "git push")
        return commit


# ----------------------------------------------------------------------------- state files
def _state_path():
    return config.STORAGE_DIR / "state.json"


def _load_state():
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    with _lock:
        for k in ("head", "man", "store", "local", "blobs", "pins", "synced_roots"):
            if k in raw:
                STATE[k] = raw[k]
        STUBS.clear()
        for key, e in STATE["local"].items():
            if e.get("stub"):
                STUBS[key] = e.get("s", 0)


def _save_state():
    try:
        config.ensure_private_dir(config.STORAGE_DIR)
        p = _state_path()
        tmp = p.with_name(f".{p.name}.{secrets.token_hex(4)}.tmp")
        with _lock:
            blob = json.dumps(STATE)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp, p)
    except OSError:
        pass


# ----------------------------------------------------------------------------- roots and keys
def roots():
    """``[(key_prefix, host_dir)]`` for everything that is stored in the repo and exists on this disk."""
    out = []
    ws = config.WORKSPACE_DIR
    out.append(("host", ws))
    vm = config.VM_DIR
    try:
        users = [u for u in vm.iterdir() if u.is_dir() and u.name not in ("bin", "cache", "tmp")]
    except OSError:
        users = []
    for u in sorted(users):
        try:
            systems = [v for v in u.iterdir() if v.is_dir()]
        except OSError:
            continue
        for v in sorted(systems):
            rootfs = v / "rootfs"
            if not (v / ".ready").exists() or not rootfs.is_dir():
                continue
            for pre in VM_PREFIXES:
                out.append((f"vm/{u.name}/{v.name}/{pre}", rootfs / pre))
    return out


def _real(p):
    return os.path.realpath(p)


def key_for(host_path):
    """The manifest key of ``host_path`` if it lies in a stored area, else ``None``."""
    try:
        parent, name = os.path.split(os.path.abspath(str(host_path)))
        real = os.path.join(_real(parent), name)
    except OSError:
        return None
    for prefix, hd in roots():
        base = _real(hd)
        if real == base:
            return prefix
        if real.startswith(base + os.sep):
            return prefix + "/" + real[len(base) + 1:].replace(os.sep, "/")
    return None


def _root_of(key):
    for prefix, hd in roots():
        if key == prefix or key.startswith(prefix + "/"):
            return prefix, hd
    return None, None


def _safe_target(key, create_dirs=True):
    """The host path for ``key``, refusing anything that would pass through a symlink or escape the root."""
    prefix, hd = _root_of(key)
    if prefix is None:
        raise StoreError(f"'{key}' is not in a stored folder on this server")
    rel = key[len(prefix):].strip("/")
    parts = [p for p in rel.split("/") if p] if rel else []
    if any(p in (".", "..") or "\0" in p for p in parts):
        raise StoreError(f"unsafe path in the repository: {key}")
    cur = Path(hd)
    if create_dirs:
        cur.mkdir(parents=True, exist_ok=True)
    for comp in parts[:-1]:
        cur = cur / comp
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if not create_dirs:
                return cur / parts[-1]
            os.mkdir(cur)
            continue
        if not _stat.S_ISDIR(st.st_mode):
            raise StoreError(f"refusing to write through a link or file: {cur}")
    return cur / parts[-1] if parts else cur


def _under(key, prefix):
    return key == prefix or key.startswith(prefix + "/")


# ----------------------------------------------------------------------------- manifest io
def _encode_manifest(man, store):
    raw = gzip.compress(json.dumps({"v": 1, "files": man}, separators=(",", ":")).encode())
    if config.STORAGE_ENCRYPT:
        enc, _mac = _keys(create=True)
        return "manifest.enc", _seal(enc, b"manifest", raw)
    return "manifest.gz", raw


def _decode_manifest(name, data, store):
    if name.endswith(".enc"):
        enc, _mac = _keys()
        data = _open(enc, b"manifest", data)
    return json.loads(gzip.decompress(data)).get("files", {})


def _load_remote(rd):
    """``(store, manifest)`` from an open :class:`Reader` (empty when nothing is stored yet)."""
    raw = rd.read("store.json")
    if raw is None:
        return {}, {}
    store = json.loads(raw)
    with _lock:
        STATE["store"] = store                  # the key check needs the repo's salt before the manifest opens
    name = store.get("manifest") or "manifest.gz"
    data = rd.read(name)
    if data is None:
        return store, {}
    try:
        return store, _decode_manifest(name, data, store)
    except StoreError:
        raise
    except Exception as e:
        raise StoreError(f"the stored file list is unreadable: {type(e).__name__}") from None


def _blob_path(cid):
    return f"b/{cid[:2]}/{cid}"


# ----------------------------------------------------------------------------- chunking
def _stub_text(key, entry):
    body = (f"This file is stored in your repository and is not on this disk right now.\n"
            f"Open it in the file manager, or run:  storage get {os.path.basename(key)}\n")
    return (STUB_MAGIC + f"key: {json.dumps(key)}\nhash: {entry.get('h', '')}\nsize: {entry.get('s', 0)}\n".encode()
            + body.encode())


def parse_stub(data):
    """``(key, hash, size)`` from placeholder bytes, or ``None`` if ``data`` is not one."""
    if not data.startswith(STUB_MAGIC):
        return None
    try:
        lines = data[len(STUB_MAGIC):].decode("utf-8", "replace").splitlines()
        key = json.loads(lines[0].split(": ", 1)[1])
        h = lines[1].split(": ", 1)[1]
        size = int(lines[2].split(": ", 1)[1])
        return key, h, size
    except (IndexError, ValueError):
        return None


def _encode_file(path, stage_dir, known_ids, encrypt):
    """Cut ``path`` into pieces. Returns ``(entry, staged)`` with staged = ``{id: stage file}`` for new pieces."""
    chunk = config.STORAGE_CHUNK_MB * 1024 * 1024
    keys = _keys(create=True) if encrypt else (None, None)
    enc, mac = keys
    st0 = os.stat(path)
    whole = _hash_new()
    pieces, staged = [], {}
    with open(path, "rb") as f:
        offset = 0
        while True:
            h = hmac.new(mac, digestmod=hashlib.sha256) if encrypt else _hash_new()
            n = 0
            while n < chunk:
                buf = f.read(min(1024 * 1024, chunk - n))
                if not buf:
                    break
                h.update(buf)
                whole.update(buf)
                n += len(buf)
            if n == 0 and offset > 0:
                break
            cid = h.hexdigest()
            pieces.append([cid, n])
            if cid not in known_ids and cid not in staged:
                sp = os.path.join(stage_dir, cid)
                with open(sp, "wb") as out, open(path, "rb") as src:
                    src.seek(offset)
                    left = n

                    def rd(k, src=src):
                        nonlocal left
                        take = min(k, left)
                        left -= take
                        return src.read(take) if take > 0 else b""
                    if encrypt:
                        _encode_frames(enc, cid.encode(), rd, out)
                    else:
                        while True:
                            b = rd(1024 * 1024)
                            if not b:
                                break
                            out.write(b)
                staged[cid] = sp
            offset += n
            if n < chunk:
                break
    st1 = os.stat(path)
    if (st1.st_size, st1.st_mtime_ns) != (st0.st_size, st0.st_mtime_ns):
        for p in staged.values():
            with contextlib.suppress(OSError):
                os.unlink(p)
        raise StoreError("changed while it was being read")
    entry = {"t": "f", "s": st1.st_size, "h": whole.hexdigest(), "c": pieces, "e": 1 if encrypt else 0,
             "mode": _stat.S_IMODE(st1.st_mode), "mt": st1.st_mtime_ns}
    return entry, staged


def _fetch_into(rd, entry, key, sink_path):
    """Download and join the pieces of ``entry`` into ``sink_path`` (verifying every piece and the whole)."""
    whole = _hash_new()
    enc = mac = None
    with open(sink_path, "wb") as out:
        for cid, n in entry.get("c", []):
            path = _blob_path(cid)
            if entry.get("e"):
                if enc is None:
                    enc, mac = _keys()
                h = hmac.new(mac, digestmod=hashlib.sha256)
                buf = _Spool()
                rd.stream(path, buf.write)
                buf.rewind()

                def sink(b, h=h):
                    h.update(b)
                    whole.update(b)
                    out.write(b)
                try:
                    _decode_frames(enc, cid.encode(), buf, sink)
                finally:
                    buf.close()
                if h.hexdigest() != cid:
                    raise StoreError(f"a stored piece of {os.path.basename(key)} does not match its checksum")
            else:
                h = _hash_new()

                def sink(b, h=h):
                    h.update(b)
                    whole.update(b)
                    out.write(b)
                rd.stream(path, sink)
                if h.hexdigest() != cid:
                    raise StoreError(f"a stored piece of {os.path.basename(key)} does not match its checksum")
    if whole.hexdigest() != entry.get("h"):
        raise StoreError(f"{os.path.basename(key)} did not reassemble correctly (checksum mismatch)")


class _Spool:
    """A temp file that holds one encrypted piece while it is being decrypted (bounded memory)."""

    def __init__(self):
        self.f = tempfile.TemporaryFile(dir=_tmpdir())

    def write(self, b):
        self.f.write(b)

    def rewind(self):
        self.f.flush()
        self.f.seek(0)

    def read(self, n):
        return self.f.read(n)

    def close(self):
        self.f.close()


def _tmpdir():
    d = config.STORAGE_DIR / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


# ----------------------------------------------------------------------------- scanning
def _is_hot(st):
    return time.time() - st.st_mtime < HOT_SECONDS


def _scan():
    """Walk every stored folder. Returns ``(puts, dels, seen_roots, hot)``.

    puts: list of ``(key, kind, path, extra)`` - kind in ``f``/``d``/``l``/``stubcopy``."""
    puts, hot = [], False
    seen_keys = set()
    seen_roots = []
    with _lock:
        local = dict(STATE["local"])
        man = STATE["man"]
    now = time.time()
    for prefix, hd in roots():
        hd = Path(hd)
        if not hd.is_dir():
            continue
        seen_roots.append(prefix)
        stack = [(hd, prefix)]
        while stack:
            d, dkey = stack.pop()
            try:
                it = list(os.scandir(d))
            except OSError:
                continue
            for e in it:
                name = e.name
                if name.endswith(SKIP_SUFFIX):
                    continue
                key = dkey + "/" + name
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                mode = st.st_mode
                if _stat.S_ISLNK(mode):
                    seen_keys.add(key)
                    try:
                        target = os.readlink(e.path)
                    except OSError:
                        continue
                    cur = man.get(key)
                    if not (cur and cur.get("t") == "l" and cur.get("to") == target):
                        puts.append((key, "l", e.path, {"to": target}))
                elif _stat.S_ISDIR(mode):
                    if name in SKIP_DIRS:
                        continue
                    seen_keys.add(key)
                    cur = man.get(key)
                    if not cur or cur.get("t") != "d":
                        puts.append((key, "d", e.path, {"mode": _stat.S_IMODE(mode)}))
                    stack.append((Path(e.path), key))
                elif _stat.S_ISREG(mode):
                    seen_keys.add(key)
                    rec = local.get(key)
                    if rec and rec.get("m") == st.st_mtime_ns and rec.get("disk") == st.st_size \
                            and (rec.get("ok") or rec.get("stub")):
                        continue
                    if _is_hot(st):
                        hot = True
                        continue
                    if st.st_size <= 1024:
                        try:
                            with open(e.path, "rb") as f:
                                head = f.read(1025)
                        except OSError:
                            continue
                        sp = parse_stub(head)
                        if sp:
                            puts.append((key, "stubcopy", e.path, {"from": sp[0], "hash": sp[1]}))
                            continue
                    cd = _last_up.get(key, 0)
                    if cd > now:
                        continue
                    puts.append((key, "f", e.path, {}))
    dels = []
    with _lock:
        for key, rec in local.items():
            if key in seen_keys:
                continue
            prefix, hd = _root_of(key)
            if prefix is None or prefix not in seen_roots:
                continue
            dels.append(key)
    return puts, dels, seen_roots, hot


def _cache_bytes():
    return sum(e.get("s", 0) for e in STATE["local"].values() if e.get("t") == "f" and not e.get("stub"))


# ----------------------------------------------------------------------------- the sync pass
def _lock_for(key):
    with _lock:
        return _key_locks.setdefault(key, threading.Lock())


def kick():
    """Ask the background thread to sync soon (after a file was changed through the console)."""
    if active() and _booted:
        _wake.set()


def sync_once(reason="scheduled", force=False):
    """Scan, upload what changed, evict if the cache is full. Returns ``(ok, message)``; never raises."""
    if not active():
        return True, "repo storage is off"
    if not _sync_lock.acquire(blocking=False):
        return True, "a sync is already running"
    try:
        if _blocked or not _booted:
            return False, _blocked or "repo storage is still starting"
        _set(state="scanning", message="looking for changes")
        puts, dels, seen_roots, hot = _scan()
        if not puts and not dels and not force:
            _evict_if_needed()
            _set(state="idle", message="", last_ok=True, last_error="", last_time=time.time())
            if hot:
                _wake.set()
            return True, "no changes"
        ok, msg = _apply(puts, dels, reason)
        _evict_if_needed()
        _set(state="idle", message="")
        if hot:
            _wake.set()
        return ok, msg
    except StoreError as e:
        msg = _scrub(str(e))
    except Exception as e:                                   # never let storage crash the server
        msg = _scrub(f"{type(e).__name__}: {e}")
    finally:
        _sync_lock.release()
    _set(state="idle", message="", last_ok=False, last_error=msg, last_time=time.time())
    return False, msg


def _apply(puts, dels, reason):
    t0 = time.time()
    encrypt = bool(config.STORAGE_ENCRYPT)
    with _lock:
        local = STATE["local"]
    with Reader() as rd:
        remote_store, remote_man = _load_remote(rd)
        tip = rd.tip
    with _lock:
        if tip and tip != STATE["head"] and STATE["head"]:
            _S["remote_changed"] = True               # somebody else pushed: build on what is there now
        if tip:
            man = dict(remote_man)
            STATE["store"] = remote_store
        else:
            man = dict(STATE["man"]) if not STATE["head"] else {}
        # entries we know and the remote forgot (a wiped repo): keep ours so nothing is lost
        for k, v in STATE["man"].items():
            if k not in man and k in local and not tip:
                man[k] = v
        blobs = set(STATE["blobs"]) | {c[0] for e in man.values() for c in e.get("c", ())}
    stage = Path(config.STORAGE_DIR / "stage")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    total = len(puts) + len(dels)
    _set(state="uploading", total=total, done=0, message="uploading changes")
    done, staged_all, staged_bytes, uploaded = 0, {}, 0, 0
    skipped = []
    pending_local = {}

    def flush(final=False):
        nonlocal staged_all, staged_bytes, man, blobs, pending_local
        if not staged_all and not final:
            return
        refs = {c[0] for e in man.values() for c in e.get("c", ())}
        orphans = [b for b in blobs if b not in refs and b not in staged_all]
        name, mdata = _encode_manifest(man, remote_store)
        store = dict(STATE["store"] or remote_store or {})
        store.update({"v": 1, "manifest": name, "created": store.get("created") or int(time.time()),
                      "updated": int(time.time())})
        if config.STORAGE_ENCRYPT and not store.get("salt"):
            _keys(create=True)
            store = dict(STATE["store"], **store)
        extra = {"store.json": json.dumps(store, indent=1).encode(), name: mdata}
        old = "manifest.gz" if name == "manifest.enc" else "manifest.enc"
        rm = [_blob_path(b) for b in orphans] + [old]
        add = {_blob_path(cid): p for cid, p in staged_all.items()}
        commit = push_commit(add, rm, extra, f"rconsole storage {time.strftime('%Y-%m-%d %H:%M:%S')}")
        with _lock:
            STATE["head"] = commit
            STATE["man"] = dict(man)
            STATE["store"] = store
            blobs |= set(staged_all)
            blobs -= set(orphans)
            STATE["blobs"] = sorted(blobs)
            for k, rec in pending_local.items():
                if rec is None:
                    STATE["local"].pop(k, None)
                    STUBS.pop(k, None)
                else:
                    STATE["local"][k] = rec
                    if rec.get("stub"):
                        STUBS[k] = rec.get("s", 0)
                    else:
                        STUBS.pop(k, None)
            _S["pushes"] += 1
        _save_state()
        staged_all, staged_bytes, pending_local = {}, 0, {}

    try:
        for key, kind, path, extra in puts:
            if _stop.is_set() and reason != "shutdown":
                break
            done += 1
            _set(done=done, message=f"uploading {os.path.basename(key)}")
            try:
                if kind == "d":
                    man[key] = {"t": "d", "mode": extra["mode"]}
                    pending_local[key] = {"t": "d"}
                elif kind == "l":
                    man[key] = {"t": "l", "to": extra["to"]}
                    pending_local[key] = {"t": "l"}
                elif kind == "stubcopy":
                    src = man.get(extra["from"])
                    if src and src.get("h") == extra["hash"]:
                        man[key] = dict(src)
                        st = os.lstat(path)
                        pending_local[key] = {"t": "f", "s": src["s"], "m": st.st_mtime_ns, "disk": st.st_size,
                                              "stub": True, "at": time.time()}
                    else:
                        skipped.append(key)
                else:
                    st = os.stat(path)
                    entry, staged = _encode_file(path, str(stage), blobs, encrypt)
                    cur = man.get(key)
                    unchanged = bool(cur and cur.get("h") == entry["h"] and cur.get("t") == "f")
                    if not unchanged:
                        man[key] = entry
                        staged_all.update(staged)
                        staged_bytes += sum(os.path.getsize(p) for p in staged.values())
                        uploaded += entry["s"]
                    st2 = os.stat(path)
                    pending_local[key] = {"t": "f", "s": entry["s"], "m": st2.st_mtime_ns, "disk": st2.st_size,
                                          "ok": True, "at": time.time()}
                    _last_up[key] = time.time() + min(900, max(config.STORAGE_INTERVAL * 3,
                                                               entry["s"] / (1024 * 1024) * 5))
            except StoreError as e:
                skipped.append(key)
                if "changed while" not in str(e):
                    _set(last_error=_scrub(str(e)))
            except OSError:
                skipped.append(key)
            if staged_bytes >= BATCH_BYTES:
                flush()
        for key in dels:
            man.pop(key, None)
            pending_local[key] = None
        flush(final=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    with _lock:
        _S["uploaded"] += uploaded
    _set(last_ok=True, last_error="", last_time=time.time())
    msg = (f"synced {len(puts) - len(skipped)} change(s), {len(dels)} removal(s), "
           f"{uploaded // 1024} KB uploaded in {time.time() - t0:.1f}s [{reason}]")
    if skipped:
        _wake.set()                                         # files that were busy: try again soon
    return True, msg


# ----------------------------------------------------------------------------- hydrate and evict
def _write_local(key, rec):
    with _lock:
        STATE["local"][key] = rec
        if rec.get("stub"):
            STUBS[key] = rec.get("s", 0)
        else:
            STUBS.pop(key, None)


def hydrate(keys, rd=None):
    """Bring the real data of the placeholder(s) ``keys`` back to disk. Returns the number restored."""
    n = 0
    own = rd is None
    ctx = Reader() if own else contextlib.nullcontext(rd)
    with ctx as reader:
        for key in keys:
            with _lock_for(key):
                with _lock:
                    rec = STATE["local"].get(key)
                    entry = STATE["man"].get(key)
                if not entry or entry.get("t") != "f":
                    raise StoreError(f"'{key}' is not in the repository")
                if not (rec and rec.get("stub")) and not _is_stub_on_disk(key):
                    continue
                path = _safe_target(key, create_dirs=True)
                part = str(path) + ".rcpart"
                try:
                    _fetch_into(reader, entry, key, part)
                    with contextlib.suppress(OSError):
                        os.chmod(part, entry.get("mode", 0o644))
                    mt = entry.get("mt")
                    if mt:
                        with contextlib.suppress(OSError):
                            os.utime(part, ns=(mt, mt))
                    os.replace(part, path)
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(part)
                st = os.stat(path)
                _write_local(key, {"t": "f", "s": entry["s"], "m": st.st_mtime_ns, "disk": st.st_size,
                                   "ok": True, "at": time.time()})
                n += 1
                _set(hydrated=_S["hydrated"] + 1)
    if n:
        _save_state()
    return n


def _is_stub_on_disk(key):
    try:
        p = _safe_target(key, create_dirs=False)
        if os.path.getsize(p) > 1024:
            return False
        with open(p, "rb") as f:
            return f.read(len(STUB_MAGIC)) == STUB_MAGIC
    except (OSError, StoreError):
        return False


def stub_size(host_path):
    """Real size of ``host_path`` if it is a placeholder on this disk, else ``None``."""
    if not STUBS:
        return None
    key = key_for(host_path)
    return STUBS.get(key) if key else None


def dir_stub_sizes(host_dir):
    """``{name: real size}`` of the placeholders directly inside ``host_dir``."""
    if not STUBS:
        return {}
    dk = key_for(host_dir)
    if not dk:
        return {}
    pre = dk + "/"
    return {k[len(pre):]: s for k, s in STUBS.items() if k.startswith(pre) and "/" not in k[len(pre):]}


def ensure_local(host_path, recursive=False):
    """Make sure ``host_path`` (a file, or with ``recursive`` every file below a folder) is really on disk.

    A no-op unless repo storage is on. Raises :class:`StoreError` if a needed file cannot be fetched."""
    if not STUBS or not (active() or _hydrating_all):
        return 0
    key = key_for(host_path)
    if not key:
        return 0
    if recursive:
        pre = key + "/"
        todo = [k for k in list(STUBS) if k == key or k.startswith(pre)]
    else:
        todo = [key] if key in STUBS else []
    if not todo:
        return 0
    return hydrate(todo)


def touch_access(host_path):
    key = key_for(host_path)
    if key:
        with _lock:
            rec = STATE["local"].get(key)
            if rec:
                rec["at"] = time.time()


def _resolve_token(tok, cwd, resolver):
    if not tok or tok.startswith("-") and len(tok) > 1 and "/" not in tok:
        return None
    if "=" in tok and not tok.startswith(("/", ".", "~")):
        tok = tok.split("=", 1)[1]
    if not tok:
        return None
    return resolver(tok)


def before_command(cmd, cwd, resolver):
    """Hydrate the placeholders a shell command names (``resolver`` maps a word to a host path or ``None``).

    Called for every host/VM command. Cheap: it returns at once when there are no placeholders."""
    if not STUBS or not active():
        return 0
    try:
        words = shlex.split(cmd, comments=False, posix=True)
    except ValueError:
        words = cmd.split()
    import glob as _glob
    todo = set()
    base = resolver(cwd) if cwd else None
    for w in words:
        for tok in {w, *re.split(r"[<>|;&=:,]", w)}:
            if not tok or tok in ("-", "--"):
                continue
            if tok.startswith("-") and "/" not in tok:
                continue
            p = _resolve_token(tok, cwd, resolver)
            if p is None:
                continue
            cands = [p]
            if any(c in tok for c in "*?["):
                with contextlib.suppress(Exception):
                    cands = [Path(x) for x in _glob.glob(str(p))][:500]
            for c in cands:
                k = key_for(c)
                if not k:
                    continue
                if k in STUBS:
                    todo.add(k)
                elif os.path.isdir(c):
                    pre = k + "/"
                    for sk in STUBS:
                        if sk.startswith(pre):
                            todo.add(sk)
    # a command run with no path argument often works on its own folder (`ls`, `du`, `python`, `make`)
    if base is not None and not todo:
        k = key_for(base)
        if k:
            pre = k + "/"
            for sk in STUBS:
                if sk.startswith(pre) and "/" not in sk[len(pre):] and len(todo) < 200:
                    pass                                        # listing a folder must not fetch its big files
    if not todo:
        return 0
    return hydrate(sorted(todo)[:2000])


def _evict_if_needed():
    limit = config.STORAGE_CACHE_MB * 1024 * 1024
    stub_min = config.STORAGE_STUB_MB * 1024 * 1024
    with _lock:
        used = _cache_bytes()
        if used <= limit:
            return 0
        pins = set(STATE["pins"])
        cands = [(rec.get("at", 0), key, rec) for key, rec in STATE["local"].items()
                 if rec.get("t") == "f" and not rec.get("stub") and rec.get("ok") and rec.get("s", 0) >= stub_min
                 and key not in pins and key in STATE["man"]]
    cands.sort(key=lambda x: x[0])
    freed = n = 0
    target = int(limit * 0.9)
    for _at, key, rec in cands:
        if used - freed <= target:
            break
        try:
            if evict(key):
                freed += rec.get("s", 0)
                n += 1
        except (OSError, StoreError):
            continue
    return n


def evict(key):
    """Replace a clean, uploaded file by a placeholder. Returns True if it was replaced."""
    with _lock_for(key):
        with _lock:
            rec = STATE["local"].get(key)
            entry = STATE["man"].get(key)
        if not rec or not entry or rec.get("stub") or not rec.get("ok") or key in STATE["pins"]:
            return False
        path = _safe_target(key, create_dirs=False)
        st = os.lstat(path)
        if not _stat.S_ISREG(st.st_mode) or st.st_mtime_ns != rec.get("m") or st.st_size != rec.get("disk"):
            return False                                    # changed since the last sync: not clean
        tmp = str(path) + ".rcstub"
        with open(tmp, "wb") as f:
            f.write(_stub_text(key, entry))
        with contextlib.suppress(OSError):
            os.chmod(tmp, _stat.S_IMODE(st.st_mode))
        os.replace(tmp, path)
        st2 = os.lstat(path)
        _write_local(key, {"t": "f", "s": entry["s"], "m": st2.st_mtime_ns, "disk": st2.st_size, "stub": True,
                           "at": rec.get("at", 0)})
        _set(evicted=_S["evicted"] + 1)
    _save_state()
    return True


# ----------------------------------------------------------------------------- restore (boot, new system, switch)
def materialize(prefix_filter=None, log=print):
    """Recreate the stored folders on this disk from the repo: folders and links, small files with their data,
    big files as placeholders. Existing files are never overwritten. Returns ``(restored, placeholders)``."""
    with Reader(limit_bytes=config.STORAGE_STUB_MB * 1024 * 1024) as rd:
        store, man = _load_remote(rd)
        if not rd.tip:
            return 0, 0
        stub_min = config.STORAGE_STUB_MB * 1024 * 1024
        with _lock:
            STATE["store"] = store
            STATE["head"] = rd.tip
            for k, v in STATE["man"].items():
                pass
            STATE["man"] = dict(man)
            STATE["blobs"] = sorted({c[0] for e in man.values() for c in e.get("c", ())})
        live = roots()
        wanted_prefix = [p for p, _h in live if prefix_filter is None or p.startswith(prefix_filter)]
        small = []
        for key, e in sorted(man.items()):
            pre, _hd = _root_of(key)
            if pre is None or pre not in wanted_prefix:
                continue
            small.append((key, e))
        restored = stubs = 0
        small_paths = {}
        for key, e in small:
            if e.get("t") == "f" and e.get("s", 0) < stub_min:
                for cid, _n in e.get("c", []):
                    small_paths[_blob_path(cid)] = cid
        blobs = rd.batch(list(small_paths)) if small_paths else {}
        enc = mac = None
        for key, e in small:
            try:
                path = _safe_target(key, create_dirs=True)
                t = e.get("t")
                if os.path.lexists(path):
                    if key not in STATE["local"] and t != "d":
                        pass
                    continue
                if t == "d":
                    os.mkdir(path)
                    with contextlib.suppress(OSError):
                        os.chmod(path, e.get("mode", 0o755))
                    _write_local(key, {"t": "d"})
                elif t == "l":
                    os.symlink(e.get("to", ""), path)
                    _write_local(key, {"t": "l"})
                elif t == "f":
                    if e.get("s", 0) >= stub_min:
                        with open(path, "wb") as f:
                            f.write(_stub_text(key, e))
                        with contextlib.suppress(OSError):
                            os.chmod(path, e.get("mode", 0o644))
                        st = os.lstat(path)
                        _write_local(key, {"t": "f", "s": e["s"], "m": st.st_mtime_ns, "disk": st.st_size,
                                           "stub": True, "at": 0})
                        stubs += 1
                    else:
                        whole = _hash_new()
                        part = str(path) + ".rcpart"
                        with open(part, "wb") as out:
                            for cid, _n in e.get("c", []):
                                data = blobs.get(_blob_path(cid))
                                if data is None:
                                    raise StoreError(f"piece of {os.path.basename(key)} is missing in the repo")
                                if e.get("e"):
                                    if enc is None:
                                        enc, mac = _keys()
                                    import io

                                    def sink(b):
                                        whole.update(b)
                                        out.write(b)
                                    _decode_frames(enc, cid.encode(), io.BytesIO(data), sink)
                                else:
                                    whole.update(data)
                                    out.write(data)
                        if whole.hexdigest() != e.get("h"):
                            os.unlink(part)
                            raise StoreError(f"{os.path.basename(key)} is damaged in the repo (checksum mismatch)")
                        with contextlib.suppress(OSError):
                            os.chmod(part, e.get("mode", 0o644))
                        if e.get("mt"):
                            with contextlib.suppress(OSError):
                                os.utime(part, ns=(e["mt"], e["mt"]))
                        os.replace(part, path)
                        st = os.stat(path)
                        _write_local(key, {"t": "f", "s": e["s"], "m": st.st_mtime_ns, "disk": st.st_size,
                                           "ok": True, "at": time.time()})
                restored += 1
            except (StoreError, OSError) as ex:
                log(f"[rconsole] repo storage: could not restore {key}: {_scrub(str(ex))}")
        with _lock:
            for pre in wanted_prefix:
                if pre not in STATE["synced_roots"]:
                    STATE["synced_roots"].append(pre)
    _save_state()
    return restored, stubs


def materialize_vm(user, vm, log=print):
    """Called when a system's root filesystem has just been (re)built: bring its stored folders back."""
    if not (active() and _booted):
        return 0
    try:
        with _sync_lock:
            r, s = materialize(prefix_filter=f"vm/{user}/{vm}/", log=log)
        if r or s:
            log(f"[rconsole] repo storage: restored {r} item(s) of {user}/{vm} ({s} as placeholders)")
        return r
    except Exception as e:
        log(f"[rconsole] repo storage: could not restore {user}/{vm}: {_scrub(str(e))}")
        return 0


def bootstrap(log=print):
    """Boot hook: load the local state, and on a fresh disk (or a disk that lost files) rebuild from the repo."""
    global _booted, _blocked
    if not active():
        _booted = True
        _load_state()
        return False
    _load_state()
    try:
        config.ensure_private_dir(config.STORAGE_DIR)
        shutil.rmtree(config.STORAGE_DIR / "tmp", ignore_errors=True)
        shutil.rmtree(config.STORAGE_DIR / "stage", ignore_errors=True)
        config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        _set(state="restoring", message="restoring files from the repository")
        with _sync_lock:
            restored, stubs = materialize(log=log)
        _blocked = ""
        _booted = True
        if restored or stubs:
            log(f"[rconsole] repo storage: restored {restored} item(s) in {time.time() - t0:.0f}s "
                f"({stubs} large file(s) as placeholders)")
        _set(state="idle", message="")
        return True
    except Exception as e:
        _blocked = ("could not read the repository at start-up (" + _scrub(str(e))[:200] +
                    "); syncing is paused so nothing is deleted - it retries by itself")
        _booted = True
        _set(state="idle", message="", last_ok=False, last_error=_blocked, last_time=time.time())
        log("[rconsole] repo storage: " + _blocked)
        return False


def _retry_boot():
    global _blocked
    try:
        with _sync_lock:
            materialize(log=lambda m: None)
        _blocked = ""
        return True
    except Exception as e:
        _set(last_error=_scrub(str(e)))
        return False


# ----------------------------------------------------------------------------- maintenance actions
def hydrate_all(log=print):
    """Pull every placeholder back to disk (used when you switch back to host storage)."""
    global _hydrating_all
    if not git_ok() or not config.BACKUP_URL:
        return 0
    _hydrating_all = True
    n = 0
    try:
        keys = sorted(STUBS)
        _set(state="hydrating", total=len(keys), done=0, message="bringing every file back to this disk")
        with _sync_lock:
            with Reader() as rd:
                for i, k in enumerate(keys, 1):
                    try:
                        n += hydrate([k], rd=rd)
                    except StoreError as e:
                        log(f"[rconsole] repo storage: could not bring back {k}: {_scrub(str(e))}")
                    _set(done=i)
    finally:
        _hydrating_all = False
        _set(state="idle", message="")
    return n


def reencode(log=print):
    """Rewrite every stored piece and the manifest in the current encryption mode (after the switch)."""
    if not active():
        return False, "repo storage is off"
    encrypt = bool(config.STORAGE_ENCRYPT)
    with _sync_lock:
        _set(state="re-encoding", message="rewriting stored files", done=0)
        try:
            with Reader() as rd:
                store, man = _load_remote(rd)
                todo = [k for k, e in man.items() if e.get("t") == "f" and bool(e.get("e")) != encrypt]
                _set(total=len(todo))
                stage = config.STORAGE_DIR / "stage"
                shutil.rmtree(stage, ignore_errors=True)
                stage.mkdir(parents=True, exist_ok=True)
                with _lock:
                    STATE["store"] = store
                    STATE["man"] = dict(man)
                add_total = 0
                batch = {}
                for i, key in enumerate(todo, 1):
                    entry = man[key]
                    tmp = str(stage / f"w-{i}")
                    _fetch_into(rd, entry, key, tmp)
                    ne, staged = _encode_file(tmp, str(stage), set(), encrypt)
                    ne.update(mode=entry.get("mode", 0o644), mt=entry.get("mt"))
                    man[key] = ne
                    batch.update(staged)
                    add_total += sum(os.path.getsize(p) for p in staged.values())
                    os.unlink(tmp)
                    _set(done=i)
                    if add_total > BATCH_BYTES:
                        _commit_manifest(man, batch)
                        batch, add_total = {}, 0
                _commit_manifest(man, batch)
            return True, f"re-encoded {len(todo)} file(s) as {'encrypted' if encrypt else 'plain'}"
        except (StoreError, OSError) as e:
            return False, _scrub(str(e))
        finally:
            shutil.rmtree(config.STORAGE_DIR / "stage", ignore_errors=True)
            _set(state="idle", message="")


def _commit_manifest(man, staged):
    name, mdata = _encode_manifest(man, STATE["store"])
    store = dict(STATE["store"] or {})
    store.update({"v": 1, "manifest": name, "updated": int(time.time())})
    refs = {c[0] for e in man.values() for c in e.get("c", ())}
    blobs = set(STATE["blobs"])
    orphans = [b for b in blobs if b not in refs and b not in staged]
    add = {_blob_path(cid): p for cid, p in staged.items()}
    rm = [_blob_path(b) for b in orphans] + ["manifest.gz" if name == "manifest.enc" else "manifest.enc"]
    commit = push_commit(add, rm, {"store.json": json.dumps(store, indent=1).encode(), name: mdata},
                         f"rconsole storage {time.strftime('%Y-%m-%d %H:%M:%S')}")
    with _lock:
        STATE["head"], STATE["man"], STATE["store"] = commit, dict(man), store
        STATE["blobs"] = sorted((blobs | set(staged)) - set(orphans))
    _save_state()


def pin(host_path, on=True):
    key = key_for(host_path)
    if not key:
        raise StoreError("that path is not in a stored folder")
    with _lock:
        pins = set(STATE["pins"])
        (pins.add if on else pins.discard)(key)
        STATE["pins"] = sorted(pins)
    if on:
        ensure_local(host_path)
    _save_state()
    return key


def settings_changed(_value=None):
    """Hook for the dashboard: something in the storage settings changed."""
    global _hydrating_all
    if not git_ok():
        return
    if wanted() and not problems():
        if not _booted or not STATE["synced_roots"]:
            threading.Thread(target=_enable, name="rconsole-storage-enable", daemon=True).start()
        start_background()
        _wake.set()
    elif not wanted() and STUBS:
        threading.Thread(target=hydrate_all, name="rconsole-storage-hydrate", daemon=True).start()


def _enable():
    """Switched to github storage from the dashboard: bring remote files down, then upload what is local."""
    global _booted
    if not _booted:
        bootstrap()
    else:
        with _sync_lock:
            try:
                materialize(log=lambda m: None)
            except Exception as e:
                _set(last_error=_scrub(str(e)))
    _wake.set()


# ----------------------------------------------------------------------------- background thread
def _loop():
    global _blocked
    while not _stop.is_set():
        _wake.wait(config.STORAGE_INTERVAL)
        _wake.clear()
        if _stop.is_set():
            return
        try:
            if _blocked:
                _retry_boot()
            if active():
                sync_once("scheduled")
        except Exception:
            pass


def start_background():
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return True
        if not wanted():
            return False
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="rconsole-storage", daemon=True)
        _thread.start()
        return True


def final(timeout=20):
    """Last-chance sync at shutdown."""
    if not active() or not _booted or _blocked:
        return None
    out = []
    t = threading.Thread(target=lambda: out.append(sync_once("shutdown")), daemon=True)
    t.start()
    t.join(timeout)
    return out[0] if out else (False, f"timed out after {timeout}s")


def forget_everything():
    """Tests / `reset`: drop the in-memory and on-disk bookkeeping (the repo is untouched)."""
    global _booted, _blocked
    with _lock:
        STATE.update({"head": None, "man": {}, "store": {}, "local": {}, "blobs": [], "pins": [], "synced_roots": []})
        STUBS.clear()
        _last_up.clear()
        _key_locks.clear()
    _booted = False
    _blocked = ""
    with contextlib.suppress(OSError):
        _state_path().unlink()
