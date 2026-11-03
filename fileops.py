"""File-manager operations: compress, extract, properties, batch delete, background jobs.

app.py resolves every path through the sandbox (``_map``) before it gets here, so these
functions work on *host* paths and never decide what a user may touch. What they do
guarantee is that they stay inside the folder they are given:

* extraction creates every entry with ``dir_fd`` + ``O_NOFOLLOW``, so a symlink inside an
  archive can never be written *through* (the classic "link, then file under the link"
  trick), and ``..`` / absolute names are refused;
* archives are never created by following symlinks (they are stored as links);
* the external tools (``bsdtar``, 7-Zip, RAR) are only used where they are safe: confined
  users get ``bsdtar`` only (it refuses to write through symlinks by default), admins in
  the host shell may also use 7-Zip / RAR tools if installed.

Everything slow runs as a background :class:`Job` the browser polls, so a big archive
never ties up a request (and shows a progress bar).

Standard library only.
"""
from __future__ import annotations

import bz2
import gzip
import lzma
import mimetypes
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile

CHUNK = 256 * 1024
MAX_ENTRIES = 200_000            # refuse archives with more members than this (zip bombs)
MAX_SOURCES = 500                # items selected for one compress job
EXTERNAL_TIMEOUT = 3600          # seconds a bsdtar / 7z / rar child may run
JOB_KEEP_SECONDS = 15 * 60

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FileOpError(Exception):
    """A failure with a message that is fine to show to the user."""


class Cancelled(Exception):
    """The user cancelled the job."""


# ---------------------------------------------------------------------------
# Formats and tools
# ---------------------------------------------------------------------------
def _zstd():
    """Python 3.14+ has Zstandard in the standard library."""
    try:
        from compression import zstd  # type: ignore[import-not-found]
        return zstd
    except ImportError:
        return None


def _which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _tool_bsdtar():
    return _which("bsdtar")


def _tools_7z():
    return _which("7zz", "7z", "7za")


# id, label, extension, single-file-only
_CREATE = [
    ("zip", "ZIP", ".zip", False),
    ("tar", "TAR (no compression)", ".tar", False),
    ("tar.gz", "TAR + gzip", ".tar.gz", False),
    ("tar.bz2", "TAR + bzip2", ".tar.bz2", False),
    ("tar.xz", "TAR + xz", ".tar.xz", False),
    ("tar.zst", "TAR + Zstandard", ".tar.zst", False),
    ("7z", "7-Zip", ".7z", False),
    ("rar", "RAR", ".rar", False),
    ("gz", "gzip (one file)", ".gz", True),
    ("bz2", "bzip2 (one file)", ".bz2", True),
    ("xz", "xz (one file)", ".xz", True),
    ("zst", "Zstandard (one file)", ".zst", True),
]
_ZIP_LIKE = (".zip", ".jar", ".war", ".ear", ".apk", ".whl", ".egg")
_TAR_EXTS = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz")
_ZSTD_TAR = (".tar.zst", ".tzst")
_SINGLE_EXTS = (".gz", ".bz2", ".xz")
_EXTERNAL_EXTS = (".7z", ".rar", ".cab", ".iso", ".cpio", ".deb", ".rpm", ".lzh", ".lha", ".xar")


def can_create(fmt, trusted):
    """Is archive format ``fmt`` available for this user?"""
    if fmt in ("zip", "tar", "tar.gz", "tar.bz2", "tar.xz", "gz", "bz2", "xz"):
        return True
    if fmt in ("tar.zst", "zst"):
        return _zstd() is not None and (fmt == "zst" or hasattr(tarfile.TarFile, "zstopen"))
    if fmt == "7z":
        return bool(_tool_bsdtar() or (trusted and _tools_7z()))
    if fmt == "rar":
        return bool(trusted and _which("rar"))
    return False


_ALL_EXTS = _ZIP_LIKE + _TAR_EXTS + _ZSTD_TAR + _SINGLE_EXTS + (".zst",) + _EXTERNAL_EXTS


def extract_exts(trusted):
    """Archive extensions this user can actually extract right now (drives the Extract button)."""
    exts = list(_ZIP_LIKE) + list(_TAR_EXTS) + list(_SINGLE_EXTS)
    if _zstd() is not None:
        exts += list(_ZSTD_TAR) + [".zst"]
    if _external_extractors(trusted):
        exts += list(_EXTERNAL_EXTS)
    return exts


def capabilities(trusted):
    """What the file manager may offer this user (shown in the UI)."""
    return {
        "compress": [{"id": i, "label": label, "ext": ext, "single": single}
                     for i, label, ext, single in _CREATE if can_create(i, trusted)],
        "extract": extract_exts(trusted),
    }


def _ext_of(name, candidates):
    low = name.lower()
    best = ""
    for e in candidates:
        if low.endswith(e) and len(e) > len(best) and len(low) > len(e):
            best = e
    return best


def archive_ext(name):
    """The archive extension ``name`` ends with (whether or not a tool for it is installed), or ``""``."""
    return _ext_of(name, _ALL_EXTS)


def stem_of(name):
    ext = archive_ext(name)
    stem = name[:-len(ext)] if ext else name
    return stem.strip(" .") or "extracted"


def unique_name(directory, name, ext=""):
    """``name + ext``, or ``name (2) + ext`` ... when that already exists in ``directory``."""
    cand = name + ext
    n = 2
    while os.path.lexists(os.path.join(directory, cand)):
        cand = f"{name} ({n}){ext}"
        n += 1
    return cand


def valid_name(name):
    """Reject names with separators / NUL / dot-dot (they must be a single path segment)."""
    return bool(name) and name not in (".", "..") and not any(c in name for c in "/\\\0") and len(name) <= 200


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, user, op):
        self.id = uuid.uuid4().hex[:12]
        self.user = user
        self.op = op
        self.state = "running"          # running | done | error | cancelled
        self.done = 0
        self.total = 0
        self.note = ""
        self.error = ""
        self.result = None
        self.started = time.time()
        self.finished = None
        self._cancel = threading.Event()

    def check(self):
        if self._cancel.is_set():
            raise Cancelled()

    def add(self, n):
        self.done += n

    def as_dict(self):
        return {"id": self.id, "op": self.op, "state": self.state, "done": self.done, "total": self.total,
                "note": self.note, "error": self.error, "result": self.result,
                "elapsed": round((self.finished or time.time()) - self.started, 1)}


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


class JobBusy(Exception):
    pass


def _prune():
    now = time.time()
    for jid in [j for j, job in _JOBS.items() if job.finished and now - job.finished > JOB_KEEP_SECONDS]:
        del _JOBS[jid]


def start_job(user, op, work):
    """Run ``work(job)`` in a daemon thread; one running job per user."""
    with _JOBS_LOCK:
        _prune()
        if any(j.user == user and j.state == "running" for j in _JOBS.values()):
            raise JobBusy("another file operation is still running")
        job = Job(user, op)
        _JOBS[job.id] = job

    def runner():
        try:
            job.result = work(job)
            job.state = "done"
        except Cancelled:
            job.state = "cancelled"
            job.error = "cancelled"
        except FileOpError as e:
            job.state = "error"
            job.error = str(e)
        except Exception as e:  # noqa: BLE001 - never leave a job "running" forever
            job.state = "error"
            job.error = f"unexpected error: {e}"
        finally:
            job.finished = time.time()

    threading.Thread(target=runner, name=f"fileop-{job.id}", daemon=True).start()
    return job


def get_job(user, job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    return job if job and job.user == user else None


def cancel_job(user, job_id):
    job = get_job(user, job_id)
    if job and job.state == "running":
        job._cancel.set()
    return job


def cancel_all():
    """Shutdown hook: stop whatever is running."""
    with _JOBS_LOCK:
        for job in _JOBS.values():
            job._cancel.set()


# ---------------------------------------------------------------------------
# Walking a selection (no symlink is ever followed)
# ---------------------------------------------------------------------------
def _zip_time(ts):
    t = time.localtime(max(ts, 315532800))      # zip cannot store dates before 1980
    return t[:6]


def _walk_sources(sources):
    """Yield ``(kind, arcname, st, opener, target)`` for every item below the selected paths.

    ``kind`` is ``dir`` / ``file`` / ``link``. For a file, ``opener()`` returns a read-only fd
    opened with ``O_NOFOLLOW`` relative to its parent directory fd; it must be used before the
    generator advances. Sockets, FIFOs and devices are skipped.
    """
    nofollow = os.O_NOFOLLOW
    for src in sources:
        parent, name = os.path.split(os.fspath(src))
        pfd = os.open(parent or ".", os.O_RDONLY | os.O_DIRECTORY)
        try:
            st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                yield "link", name, st, None, os.readlink(name, dir_fd=pfd)
            elif stat.S_ISREG(st.st_mode):
                yield "file", name, st, (lambda n=name, d=pfd: os.open(n, os.O_RDONLY | nofollow | os.O_NONBLOCK, dir_fd=d)), None
            elif stat.S_ISDIR(st.st_mode):
                for dirpath, dirnames, filenames, dirfd in os.fwalk(name, dir_fd=pfd, follow_symlinks=False):
                    arc = dirpath.replace(os.sep, "/").rstrip("/")
                    dst = os.fstat(dirfd)
                    yield "dir", arc, dst, None, None
                    for dn in list(dirnames):
                        try:
                            lst = os.stat(dn, dir_fd=dirfd, follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISLNK(lst.st_mode):
                            yield "link", arc + "/" + dn, lst, None, os.readlink(dn, dir_fd=dirfd)
                    for fn in filenames:
                        try:
                            fst = os.stat(fn, dir_fd=dirfd, follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISLNK(fst.st_mode):
                            yield "link", arc + "/" + fn, fst, None, os.readlink(fn, dir_fd=dirfd)
                        elif stat.S_ISREG(fst.st_mode):
                            yield ("file", arc + "/" + fn, fst,
                                   (lambda n=fn, d=dirfd: os.open(n, os.O_RDONLY | nofollow | os.O_NONBLOCK, dir_fd=d)), None)
            else:
                raise FileOpError(f"{name}: not a regular file or folder")
        finally:
            os.close(pfd)


def _scan(sources, job):
    """Pre-count bytes (for the progress bar) and refuse nothing; symlinks count as 0 bytes."""
    total = 0
    count = 0
    for kind, _arc, st, _op, _t in _walk_sources(sources):
        job.check()
        count += 1
        if kind == "file":
            total += st.st_size
        if count > MAX_ENTRIES * 5:
            raise FileOpError("too many files to compress in one go")
    return total, count


def _has_symlink(sources):
    return any(kind == "link" for kind, _a, _s, _o, _t in _walk_sources(sources))


class _ProgressReader:
    """File-like wrapper that reports bytes read and honours cancel."""

    def __init__(self, fobj, job):
        self.f = fobj
        self.job = job

    def read(self, n=-1):
        self.job.check()
        data = self.f.read(n)
        self.job.add(len(data))
        return data


def _tmp_name(directory, final_name):
    ext = ""
    for _i, _l, e, _s in sorted(_CREATE, key=lambda c: -len(c[2])):
        if final_name.lower().endswith(e):
            ext = e
            break
    return os.path.join(directory, f".{final_name[:len(final_name) - len(ext)]}.{uuid.uuid4().hex[:8]}.tmp{ext}")


# ---------------------------------------------------------------------------
# Compress
# ---------------------------------------------------------------------------
def _write_zip(tmp, sources, job):
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        for kind, arc, st, opener, target in _walk_sources(sources):
            job.check()
            if kind == "dir":
                zi = zipfile.ZipInfo(arc + "/", _zip_time(st.st_mtime))
                zi.external_attr = ((0o40000 | stat.S_IMODE(st.st_mode)) << 16) | 0x10
                zf.writestr(zi, b"")
            elif kind == "link":
                zi = zipfile.ZipInfo(arc, _zip_time(st.st_mtime))
                zi.external_attr = 0o120777 << 16
                zi.compress_type = zipfile.ZIP_STORED
                zf.writestr(zi, target)
            else:
                try:
                    fd = opener()
                except OSError:
                    continue
                try:
                    fst = os.fstat(fd)
                    if not stat.S_ISREG(fst.st_mode):
                        continue
                    zi = zipfile.ZipInfo(arc, _zip_time(fst.st_mtime))
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zi.external_attr = (0o100000 | stat.S_IMODE(fst.st_mode)) << 16
                    zi.file_size = fst.st_size
                    with zf.open(zi, "w", force_zip64=fst.st_size > 0x7FFFFFFF) as dest:
                        while True:
                            job.check()
                            chunk = os.read(fd, CHUNK)
                            if not chunk:
                                break
                            dest.write(chunk)
                            job.add(len(chunk))
                finally:
                    os.close(fd)


def _write_tar(tmp, mode, sources, job):
    kwargs = {"compresslevel": 6} if mode in ("w:gz", "w:bz2") else {}
    with tarfile.open(tmp, mode, format=tarfile.PAX_FORMAT, **kwargs) as tf:
        for kind, arc, st, opener, target in _walk_sources(sources):
            job.check()
            ti = tarfile.TarInfo(arc)
            ti.mtime = int(st.st_mtime)
            ti.mode = stat.S_IMODE(st.st_mode)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif kind == "link":
                ti.type = tarfile.SYMTYPE
                ti.linkname = target
                tf.addfile(ti)
            else:
                try:
                    fd = opener()
                except OSError:
                    continue
                try:
                    fst = os.fstat(fd)
                    if not stat.S_ISREG(fst.st_mode):
                        continue
                    ti.size = fst.st_size
                    ti.mtime = int(fst.st_mtime)
                    with os.fdopen(fd, "rb", closefd=False) as fobj:
                        tf.addfile(ti, _ProgressReader(fobj, job))
                except tarfile.TarError as e:
                    raise FileOpError(f"{arc}: {e}") from e
                finally:
                    os.close(fd)


def _write_single(tmp, fmt, source, job):
    """gzip / bzip2 / xz / zstd of exactly one regular file."""
    parent, name = os.path.split(os.fspath(source))
    pfd = os.open(parent or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=pfd)
        except OSError as e:
            raise FileOpError(f"{name}: cannot open ({e.strerror or e})") from e
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise FileOpError("one-file compression needs a regular file")
            job.total = st.st_size
            with os.fdopen(fd, "rb", closefd=False) as src, open(tmp, "wb") as raw:
                if fmt == "gz":
                    out = gzip.GzipFile(filename=name, mode="wb", fileobj=raw, compresslevel=6, mtime=int(st.st_mtime))
                elif fmt == "bz2":
                    out = bz2.BZ2File(raw, "wb", compresslevel=9)
                elif fmt == "xz":
                    out = lzma.LZMAFile(raw, "wb", preset=6)
                else:
                    z = _zstd()
                    if z is None:
                        raise FileOpError("Zstandard needs Python 3.14 or newer")
                    out = z.ZstdFile(raw, "wb")
                with out:
                    while True:
                        job.check()
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        job.add(len(chunk))
        finally:
            os.close(fd)
    finally:
        os.close(pfd)


def _run_external(cmd, cwd, job, tmp_path=None, limit=None):
    """Run a helper process, polling for cancel and (optionally) output growth. Returns stderr text."""
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                            start_new_session=True)
    deadline = time.time() + EXTERNAL_TIMEOUT
    tick = 0
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            tick += 1
            if job._cancel.is_set():
                raise Cancelled()
            if time.time() > deadline:
                raise FileOpError("the helper tool took too long and was stopped")
            if tmp_path is not None and tick % 2 == 0:
                try:
                    size = _tree_size(tmp_path) if os.path.isdir(tmp_path) else os.path.getsize(tmp_path)
                except OSError:
                    size = 0
                job.done = size
                if limit is not None and size > limit:
                    raise FileOpError(f"the result would be larger than the limit ({limit // 1048576} MB)")
    except BaseException:
        try:
            os.killpg(proc.pid, 9)
        except (OSError, ProcessLookupError):
            proc.kill()
        proc.wait()
        raise
    err.seek(0)
    text = err.read().decode("utf-8", "replace").strip()
    err.close()
    if proc.returncode not in (0,):
        msg = " ".join(text.split())[-300:] or f"exit status {proc.returncode}"
        raise FileOpError(msg)
    return text


def _tree_size(path):
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def compress(job, sources, out_dir, name, fmt, trusted):
    """Create ``out_dir/name+ext`` from ``sources`` (host paths inside ``out_dir``)."""
    if not sources:
        raise FileOpError("nothing selected")
    info = next((c for c in _CREATE if c[0] == fmt), None)
    if info is None or not can_create(fmt, trusted):
        raise FileOpError(f"{fmt} is not available on this server")
    ext, single = info[2], info[3]
    if not valid_name(name):
        raise FileOpError("invalid archive name")
    if single and (len(sources) != 1 or not os.path.isfile(sources[0]) or os.path.islink(sources[0])):
        raise FileOpError(f"{ext} compresses exactly one regular file; pick ZIP or TAR for folders")
    out_dir = os.fspath(out_dir)
    tmp = _tmp_name(out_dir, name + ext)
    job.note = "preparing"
    try:
        free = shutil.disk_usage(out_dir).free
        if not single:
            job.total, _count = _scan(sources, job)
        if job.total and job.total > free:
            raise FileOpError("not enough free disk space for this archive")
        job.note = "compressing"
        if fmt == "zip":
            _write_zip(tmp, sources, job)
        elif fmt.startswith("tar"):
            mode = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz", "tar.zst": "w:zst"}[fmt]
            _write_tar(tmp, mode, sources, job)
        elif single:
            _write_single(tmp, fmt, sources[0], job)
        else:
            _compress_external(tmp, fmt, sources, out_dir, job, trusted)
        job.check()
        final = unique_name(out_dir, name, ext)
        os.replace(tmp, os.path.join(out_dir, final))
        return {"path": os.path.join(out_dir, final), "name": final, "size": os.path.getsize(os.path.join(out_dir, final))}
    except OSError as e:
        raise FileOpError(e.strerror or str(e)) from e
    finally:
        if os.path.lexists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _compress_external(tmp, fmt, sources, out_dir, job, trusted):
    names = [os.path.basename(os.fspath(s)) for s in sources]
    job.total = 0                       # size of the result is unknown: the UI shows a busy bar
    bsd = _tool_bsdtar()
    if fmt == "7z" and bsd:
        # bsdtar stores symlinks as links and never follows them, so this is safe for confined users.
        _run_external([bsd, "--format=7zip", "-cf", tmp, "--"] + names, out_dir, job, tmp_path=tmp)
        return
    # 7-Zip / rar may *follow* symlinks and read outside the user's folder, so: trusted users only,
    # and only for trees without a single symlink.
    if not trusted:
        raise FileOpError(f"{fmt} is not available on this server")
    if _has_symlink(sources):
        raise FileOpError(f"{fmt} cannot store symbolic links safely; use ZIP or TAR for this selection")
    if fmt == "7z":
        tool = _tools_7z()
        _run_external([tool, "a", "-t7z", "-bd", "-y", "-mx=5", tmp, "--"] + names, out_dir, job, tmp_path=tmp)
    else:
        rar = _which("rar")
        _run_external([rar, "a", "-idq", "-r", "-y", tmp, "--"] + names, out_dir, job, tmp_path=tmp)


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------
def _safe_rel(name, winsep=False):
    """Normalised relative path from an archive member name, ``""`` for the root, or raise."""
    if "\0" in name:
        raise FileOpError("unsafe name in archive")
    if winsep:
        name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FileOpError(f"unsafe path in archive: {name!r}")
    if parts and len(parts[0]) == 2 and parts[0][1] == ":" and parts[0][0].isalpha():
        raise FileOpError(f"unsafe path in archive: {name!r}")
    return "/".join(parts)


class _Dest:
    """Creates archive members under ``root`` without ever following a symlink."""

    def __init__(self, root, job, limit):
        self.root = os.fspath(root)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.job = job
        self.limit = limit
        self.written = 0
        self.entries = 0
        self.files = 0
        self.skipped = 0            # already existed, or a type we do not extract (links, devices)
        self._free_checked = 0
        self._free = shutil.disk_usage(self.root).free

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _dirfd(self, parts):
        fd = os.dup(self.fd)
        try:
            for part in parts:
                try:
                    nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    os.mkdir(part, 0o755, dir_fd=fd)
                    nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nfd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _count(self):
        self.entries += 1
        if self.entries > MAX_ENTRIES:
            raise FileOpError(f"the archive has more than {MAX_ENTRIES} entries")

    def mkdir(self, rel):
        self._count()
        if not rel:
            return
        try:
            os.close(self._dirfd(rel.split("/")))
        except OSError:
            self.skipped += 1

    def symlink(self, rel, target):
        self._count()
        if not rel or not target or "\0" in target or len(target) > 4096:
            self.skipped += 1
            return
        parts = rel.split("/")
        try:
            pfd = self._dirfd(parts[:-1])
        except OSError:
            self.skipped += 1
            return
        try:
            os.symlink(target, parts[-1], dir_fd=pfd)
        except OSError:
            self.skipped += 1
        finally:
            os.close(pfd)

    def add_file(self, rel, mode, mtime, reader):
        self._count()
        if not rel:
            self.skipped += 1
            return
        parts = rel.split("/")
        try:
            pfd = self._dirfd(parts[:-1])
        except OSError:
            self.skipped += 1
            return
        try:
            try:
                fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=pfd)
            except OSError:
                self.skipped += 1                       # exists (we never overwrite) or cannot be created
                return
            try:
                while True:
                    self.job.check()
                    chunk = reader.read(CHUNK)
                    if not chunk:
                        break
                    self.written += len(chunk)
                    if self.written > self.limit:
                        raise FileOpError(f"the archive expands to more than {self.limit // 1048576} MB "
                                          "(raise RCONSOLE_MAX_EXTRACT_MB to allow more)")
                    if self.written - self._free_checked > 64 * 1048576:
                        self._free_checked = self.written
                        if shutil.disk_usage(self.root).free < 64 * 1048576:
                            raise FileOpError("the disk is full")
                    self._write_all(fd, chunk)
                    if getattr(reader, "_counts_output", True):
                        self.job.add(len(chunk))
                try:
                    os.fchmod(fd, ((mode or 0o644) & 0o777) | 0o600)
                    if mtime:
                        os.utime(fd, (mtime, mtime))
                except OSError:
                    pass
                self.files += 1
            finally:
                os.close(fd)
        finally:
            os.close(pfd)

    @staticmethod
    def _write_all(fd, data):
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]


class _CountingFile:
    """Wraps the archive file so a tar stream can report compressed bytes consumed."""

    def __init__(self, fobj, job):
        self.f = fobj
        self.job = job

    def read(self, n=-1):
        data = self.f.read(n)
        self.job.done += len(data)
        return data

    def close(self):
        self.f.close()


class _Member:
    """A tar member: progress is counted from the compressed input (``_CountingFile``), not here."""
    _counts_output = False

    def __init__(self, f):
        self.f = f

    def read(self, n=-1):
        return self.f.read(n)


class _Unsupported(FileOpError):
    """The native reader cannot handle this archive; an external tool might."""


def _extract_zip(archive, dest, job, winsep=True):
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as e:
        raise FileOpError("not a valid ZIP file") from e
    with zf:
        infos = zf.infolist()
        job.total = sum(i.file_size for i in infos)
        if job.total > dest.limit:
            raise FileOpError(f"the archive expands to {job.total // 1048576} MB, above the "
                              f"{dest.limit // 1048576} MB limit (RCONSOLE_MAX_EXTRACT_MB)")
        for info in infos:
            job.check()
            if info.flag_bits & 0x1:
                raise FileOpError("password-protected ZIP files are not supported")
            rel = _safe_rel(info.filename, winsep=winsep)
            mode = info.external_attr >> 16
            if info.is_dir():
                dest.mkdir(rel)
            elif stat.S_ISLNK(mode):
                if info.file_size > 4096:
                    dest._count()
                    dest.skipped += 1
                    continue
                dest.symlink(rel, zf.read(info).decode("utf-8", "replace"))
            elif stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                dest._count()
                dest.skipped += 1               # an explicit device / FIFO / socket entry: ignore
                continue
            else:
                try:
                    with zf.open(info) as src:
                        ts = time.mktime(info.date_time + (0, 0, -1)) if info.date_time else 0
                        dest.add_file(rel, mode & 0o777 if mode else 0o644, ts, src)
                except NotImplementedError as e:
                    raise _Unsupported("this ZIP uses a compression method Python cannot read") from e
                except (zipfile.BadZipFile, EOFError) as e:
                    raise FileOpError(f"{info.filename}: damaged data ({e})") from e


def _extract_tar(archive, dest, job):
    job.total = os.path.getsize(archive)
    raw = open(archive, "rb")
    try:
        try:
            tf = tarfile.open(fileobj=_CountingFile(raw, job), mode="r|*")
        except (tarfile.TarError, EOFError, OSError) as e:
            raise FileOpError("not a valid TAR archive") from e
        with tf:
            try:
                for m in tf:
                    job.check()
                    rel = _safe_rel(m.name)
                    if m.isdir():
                        dest.mkdir(rel)
                    elif m.isreg():
                        src = tf.extractfile(m)
                        dest.add_file(rel, m.mode, m.mtime, _Member(src))
                    elif m.issym():
                        dest.symlink(rel, m.linkname)
                    else:
                        dest._count()
                        dest.skipped += 1       # hard links, devices, FIFOs
            except (tarfile.TarError, EOFError, OSError, lzma.LZMAError) as e:
                if isinstance(e, FileOpError):
                    raise
                raise FileOpError(f"damaged or unsupported archive ({e})") from e
    finally:
        raw.close()


def _open_single(archive, ext):
    if ext == ".gz":
        return gzip.open(archive, "rb")
    if ext == ".bz2":
        return bz2.BZ2File(archive, "rb")
    if ext == ".xz":
        return lzma.open(archive, "rb")
    z = _zstd()
    if z is None:
        raise FileOpError("Zstandard needs Python 3.14 or newer")
    return z.open(archive, "rb")


def _extract_single(archive, out_dir, job, limit, ext):
    name = stem_of(os.path.basename(archive))
    out_dir = os.fspath(out_dir)
    final = unique_name(out_dir, name)
    tmp = os.path.join(out_dir, f".{final}.{uuid.uuid4().hex[:8]}.tmp")
    job.total = os.path.getsize(archive)
    written = 0
    try:
        with _open_single(archive, ext) as src, open(tmp, "wb") as out:
            raw = getattr(src, "fileobj", None) or getattr(src, "_fp", None)
            while True:
                job.check()
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise FileOpError(f"the file expands to more than {limit // 1048576} MB "
                                      "(raise RCONSOLE_MAX_EXTRACT_MB to allow more)")
                out.write(chunk)
                try:
                    job.done = min(job.total, raw.tell()) if raw else written
                except (OSError, ValueError, AttributeError):
                    job.done = written
        os.replace(tmp, os.path.join(out_dir, final))
        return final
    except (OSError, EOFError, lzma.LZMAError) as e:
        raise FileOpError(f"damaged or unsupported file ({e})") from e
    finally:
        if os.path.lexists(tmp):
            os.remove(tmp)


def _external_extractors(trusted):
    """Commands able to unpack the non-native formats, safest first."""
    tools = []
    b = _tool_bsdtar()
    if b:
        tools.append(("bsdtar", b))
    if trusted:
        for label, names in (("7z", ("7zz", "7z", "7za")), ("unrar", ("unrar",)), ("unar", ("unar",))):
            p = _which(*names)
            if p:
                tools.append((label, p))
    return tools


def _extract_external(archive, tmp_root, job, trusted, limit, ext):
    tools = _external_extractors(trusted)
    if not tools:
        raise FileOpError(f"{ext} archives need bsdtar (libarchive-tools) installed on the server")
    last = None
    for label, path in tools:
        if label in ("unrar",) and ext != ".rar":
            continue
        if os.listdir(tmp_root):
            shutil.rmtree(tmp_root)
            os.mkdir(tmp_root, 0o700)
        if label == "bsdtar":
            cmd = [path, "-xf", archive, "--no-same-owner", "--no-same-permissions"]
        elif label == "7z":
            cmd = [path, "x", "-y", "-bd", f"-o{tmp_root}", "-aoa", archive]
        elif label == "unrar":
            cmd = [path, "x", "-y", "-o+", "-idq", archive, tmp_root + "/"]
        else:
            cmd = [path, "-q", "-D", "-o", tmp_root, archive]
        blocks = max(1, limit // 512)
        wrapped = ["sh", "-c", 'ulimit -f "$1" 2>/dev/null; shift; exec "$@"', "sh", str(blocks)] + cmd
        try:
            _run_external(wrapped, tmp_root, job, tmp_path=tmp_root, limit=limit)
            _sanitize_tree(tmp_root, limit)
            return
        except FileOpError as e:
            last = e
            continue
    raise last or FileOpError("could not extract this archive")


def _sanitize_tree(root, limit):
    """After a helper tool ran: drop device nodes / FIFOs / sockets, enforce the entry and size caps."""
    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in list(dirnames) + list(filenames):
            count += 1
            if count > MAX_ENTRIES:
                raise FileOpError(f"the archive has more than {MAX_ENTRIES} entries")
            p = os.path.join(dirpath, n)
            st = os.lstat(p)
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
            elif not (stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                os.remove(p)
        if total > limit:
            raise FileOpError(f"the archive expands to more than {limit // 1048576} MB "
                              "(raise RCONSOLE_MAX_EXTRACT_MB to allow more)")


def _merge_tree(src, dst):
    """Move everything from ``src`` into ``dst``; names that already exist are left alone (counted)."""
    skipped = 0
    for entry in os.scandir(src):
        target = os.path.join(dst, entry.name)
        if not os.path.lexists(target):
            os.rename(entry.path, target)
        elif entry.is_dir(follow_symlinks=False) and os.path.isdir(target) and not os.path.islink(target):
            skipped += _merge_tree(entry.path, target)
        else:
            skipped += 1
    return skipped


def extract(job, archive, dest_dir, mode, trusted, limit):
    """Extract ``archive`` (host path) next to itself.

    ``mode`` ``folder``: into a new folder (the archive's own single top folder if it has one,
    otherwise a folder named after the archive); ``here``: straight into ``dest_dir``, never
    overwriting what is already there.
    """
    archive = os.path.abspath(os.fspath(archive))
    dest_dir = os.path.abspath(os.fspath(dest_dir))
    base = os.path.basename(archive)
    if not os.path.isfile(archive):
        raise FileOpError(f"{base}: not a file")
    ext = archive_ext(base)
    if not ext:
        raise FileOpError(f"{base}: not a supported archive type")
    if ext in _ZSTD_TAR + (".zst",) and _zstd() is None:
        raise FileOpError("Zstandard archives need Python 3.14 or newer on the server")
    if ext in _EXTERNAL_EXTS and not _external_extractors(trusted):
        raise FileOpError(f"{ext} archives need bsdtar (package libarchive-tools) installed on the server")
    job.note = "extracting"
    if ext in _SINGLE_EXTS or (ext == ".zst" and _zstd() is not None):
        name = _extract_single(archive, dest_dir, job, limit, ext)
        return {"path": os.path.join(dest_dir, name), "name": name, "skipped": 0, "files": 1}

    tmp_root = os.path.join(dest_dir, f".rconsole-extract-{uuid.uuid4().hex[:8]}")
    os.mkdir(tmp_root, 0o700)
    skipped = files = 0
    try:
        used_external = False
        target = dest_dir if mode == "here" else tmp_root
        if ext in _EXTERNAL_EXTS:
            used_external = True
        else:
            dest = _Dest(target, job, limit)
            try:
                try:
                    if ext in _ZIP_LIKE:
                        _extract_zip(archive, dest, job)
                    else:
                        _extract_tar(archive, dest, job)
                except _Unsupported:
                    if not _external_extractors(trusted):
                        raise
                    used_external = True
                    job.done = 0
                skipped, files = dest.skipped, dest.files
            finally:
                dest.close()
        if used_external:
            ext_root = tmp_root if mode != "here" else os.path.join(tmp_root, "x")
            if mode == "here":
                os.mkdir(ext_root, 0o700)
            _extract_external(archive, ext_root, job, trusted, limit, ext)
            files = sum(len(f) for _d, _s, f in os.walk(ext_root))
            if mode == "here":
                skipped = _merge_tree(ext_root, dest_dir)
                return {"path": dest_dir, "name": os.path.basename(dest_dir) or "/", "skipped": skipped, "files": files}
        if mode == "here":
            return {"path": dest_dir, "name": os.path.basename(dest_dir) or "/", "skipped": skipped, "files": files}
        children = os.listdir(tmp_root)
        top = os.path.join(tmp_root, children[0]) if len(children) == 1 else None
        if top and os.path.isdir(top) and not os.path.islink(top):
            final = unique_name(dest_dir, children[0])
            os.rename(top, os.path.join(dest_dir, final))
        else:
            final = unique_name(dest_dir, stem_of(base))
            os.rename(tmp_root, os.path.join(dest_dir, final))
            os.chmod(os.path.join(dest_dir, final), 0o755)
        return {"path": os.path.join(dest_dir, final), "name": final, "skipped": skipped, "files": files}
    except OSError as e:
        raise FileOpError(e.strerror or str(e)) from e
    finally:
        if os.path.lexists(tmp_root):
            shutil.rmtree(tmp_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------
def _mode_string(st):
    kinds = {stat.S_IFDIR: "d", stat.S_IFLNK: "l", stat.S_IFREG: "-", stat.S_IFCHR: "c", stat.S_IFBLK: "b",
             stat.S_IFIFO: "p", stat.S_IFSOCK: "s"}
    s = kinds.get(stat.S_IFMT(st.st_mode), "?")
    for who, shift in (("r", 6), ("w", 6), ("x", 6), ("r", 3), ("w", 3), ("x", 3), ("r", 0), ("w", 0), ("x", 0)):
        bit = {"r": 4, "w": 2, "x": 1}[who] << shift
        s += who if st.st_mode & bit else "-"
    return s


def _owner(uid, gid):
    out = {"uid": uid, "gid": gid, "user": None, "group": None}
    try:
        import grp
        import pwd
        try:
            out["user"] = pwd.getpwuid(uid).pw_name
        except KeyError:
            pass
        try:
            out["group"] = grp.getgrgid(gid).gr_name
        except KeyError:
            pass
    except ImportError:
        pass
    return out


def _tree_totals(path, budget=3.0, cap=500_000):
    files = folders = size = disk = 0
    deadline = time.time() + budget
    partial = False
    stack = [path]
    seen = 0
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    seen += 1
                    if seen > cap or time.time() > deadline:
                        partial = True
                        stack.clear()
                        break
                    try:
                        st = e.stat(follow_symlinks=False)
                        if e.is_dir(follow_symlinks=False):
                            folders += 1
                            stack.append(e.path)
                        else:
                            files += 1
                            size += st.st_size
                        disk += getattr(st, "st_blocks", 0) * 512
                    except OSError:
                        pass
        except OSError:
            pass
    return {"files": files, "folders": folders, "size": size, "disk": disk, "partial": partial}


def _archive_info(path, ext):
    try:
        if ext in _ZIP_LIKE:
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
                return {"entries": len(infos), "uncompressed": sum(i.file_size for i in infos)}
        if ext in _TAR_EXTS and os.path.getsize(path) <= 64 * 1048576:
            n = total = 0
            with tarfile.open(path, "r|*") as tf:
                for m in tf:
                    n += 1
                    total += m.size if m.isreg() else 0
                    if n > MAX_ENTRIES:
                        break
            return {"entries": n, "uncompressed": total}
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, lzma.LZMAError):
        return None
    return None


def properties(host_path, virtual_path, unrestricted):
    """Everything the *Properties* dialog shows for one file or folder."""
    p = os.fspath(host_path)
    st = os.lstat(p)
    kind = ("folder" if stat.S_ISDIR(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode)
            else "file" if stat.S_ISREG(st.st_mode) else "special")
    name = os.path.basename(p.rstrip("/")) or "/"
    out = {
        "name": name, "path": virtual_path, "kind": kind, "size": 0 if kind == "folder" else st.st_size,
        "disk": getattr(st, "st_blocks", 0) * 512, "mtime": int(st.st_mtime), "atime": int(st.st_atime),
        "ctime": int(st.st_ctime), "mode": format(stat.S_IMODE(st.st_mode), "04o"), "mode_text": _mode_string(st),
        "links": st.st_nlink, "inode": st.st_ino,
        "readable": os.access(p, os.R_OK), "writable": os.access(p, os.W_OK),
    }
    if unrestricted:
        out["owner"] = _owner(st.st_uid, st.st_gid)
    else:
        out["owner"] = {"uid": st.st_uid, "gid": st.st_gid, "user": None, "group": None}
    if kind == "link":
        try:
            out["target"] = os.readlink(p)
            out["target_exists"] = os.path.exists(p)
        except OSError:
            pass
    elif kind == "folder":
        out["contents"] = _tree_totals(p)
        out["size"] = out["contents"]["size"]
    else:
        mt = mimetypes.guess_type(name)[0]
        if mt:
            out["mime"] = mt
        ext = archive_ext(name)
        if ext and kind == "file":
            info = _archive_info(p, ext)
            if info:
                out["archive"] = info
    return out


# ---------------------------------------------------------------------------
# Batch delete
# ---------------------------------------------------------------------------
def delete_paths(paths):
    """Delete files, links and whole folders (never following links). ``(deleted, failed)``."""
    deleted = 0
    failed = []
    for p in paths:
        s = os.fspath(p)
        try:
            st = os.lstat(s)
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(s)
            else:
                os.remove(s)
            deleted += 1
        except FileNotFoundError:
            failed.append({"path": s, "error": "no such file"})
        except OSError as e:
            failed.append({"path": s, "error": e.strerror or str(e)})
    return deleted, failed
