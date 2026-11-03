"""fileops.py: compress / extract / properties / batch delete / jobs.

The 7-Zip and RAR checks use real tools (``bsdtar``, ``7z``) and are skipped when they are not
installed (CI installs ``libarchive-tools`` and ``p7zip-full``).
"""
import gzip
import io
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (puts the project on sys.path)
import fileops
from fileops import Cancelled, FileOpError, Job

FIXTURES = Path(__file__).parent / "fixtures"
HAVE_BSDTAR = shutil.which("bsdtar") is not None
HAVE_7Z = any(shutil.which(t) for t in ("7zz", "7z", "7za"))
LIMIT = 512 * 1024 * 1024


def make_job():
    return Job("tester", "test")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fileops-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.work = os.path.join(self.tmp, "work")
        os.mkdir(self.work)

    def p(self, *parts):
        return os.path.join(self.work, *parts)

    def write(self, rel, data=b"x", mode=None):
        path = self.p(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        if mode is not None:
            os.chmod(path, mode)
        return path

    def sample_tree(self, name="proj"):
        self.write(f"{name}/readme.txt", "hello\n")
        self.write(f"{name}/src/main.py", "print(1)\n")
        self.write(f"{name}/bin/run.sh", "#!/bin/sh\n", mode=0o755)
        os.makedirs(self.p(name, "empty"))
        os.symlink("readme.txt", self.p(name, "link"))
        return self.p(name)

    def tree(self, root):
        """{relative path: ('dir',) | ('file', bytes, exec?) | ('link', target)} for comparisons."""
        out = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames + filenames:
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, root)
                if os.path.islink(full):
                    out[rel] = ("link", os.readlink(full))
                elif os.path.isdir(full):
                    out[rel] = ("dir",)
                else:
                    with open(full, "rb") as f:
                        out[rel] = ("file", f.read(), bool(os.stat(full).st_mode & 0o100))
        return out

    def do_compress(self, sources, name, fmt, trusted=True):
        return fileops.compress(make_job(), sources, self.work, name, fmt, trusted)

    def do_extract(self, archive, mode="folder", trusted=True, limit=LIMIT, job=None):
        return fileops.extract(job or make_job(), archive, os.path.dirname(archive), mode, trusted, limit)


class RoundTripTests(Base):
    def test_every_native_format_round_trips_a_tree(self):
        src = self.sample_tree()
        want = self.tree(src)
        for fmt, ext in (("zip", ".zip"), ("tar", ".tar"), ("tar.gz", ".tar.gz"),
                         ("tar.bz2", ".tar.bz2"), ("tar.xz", ".tar.xz")):
            with self.subTest(fmt=fmt):
                res = self.do_compress([src], "pack-" + fmt, fmt)
                self.assertEqual(res["name"], "pack-" + fmt + ext)
                self.assertTrue(os.path.isfile(res["path"]))
                out = self.do_extract(res["path"])
                # one top folder inside the archive: that folder is used as it is (no proj/proj)
                self.assertEqual(os.path.basename(out["path"]), "proj" if not os.path.exists(self.p("proj (2)")) else "proj (2)")
                self.assertEqual(self.tree(out["path"]), want)
                shutil.rmtree(out["path"])

    def test_zstd_when_python_has_it(self):
        if not fileops._zstd():
            self.skipTest("needs Python 3.14 (compression.zstd)")
        src = self.sample_tree()
        res = self.do_compress([src], "z", "tar.zst")
        out = self.do_extract(res["path"])
        self.assertEqual(self.tree(out["path"]), self.tree(src))
        single = self.write("one.txt", "zzz" * 100)
        r = self.do_compress([single], "one", "zst")
        os.remove(single)
        self.assertEqual(open(self.do_extract(r["path"])["path"], "rb").read(), b"zzz" * 100)

    def test_several_items_become_top_level_entries(self):
        a = self.write("a.txt", "A")
        b = self.write("b.txt", "B")
        d = self.sample_tree("d")
        res = self.do_compress([a, b, d], "mix", "zip")
        with zipfile.ZipFile(res["path"]) as zf:
            names = set(zf.namelist())
        self.assertTrue({"a.txt", "b.txt", "d/", "d/readme.txt", "d/empty/"} <= names, names)
        out = self.do_extract(res["path"])
        # several top-level entries: a folder named after the archive
        self.assertEqual(out["name"], "mix")
        self.assertEqual(self.tree(out["path"])["d/readme.txt"][1], b"hello\n")

    def test_names_never_overwrite(self):
        f = self.write("a.txt", "A")
        r1 = self.do_compress([f], "a", "zip")
        r2 = self.do_compress([f], "a", "zip")
        r3 = self.do_compress([f], "a", "tar.gz")
        r4 = self.do_compress([f], "a", "tar.gz")
        self.assertEqual([r["name"] for r in (r1, r2, r3, r4)], ["a.zip", "a (2).zip", "a.tar.gz", "a (2).tar.gz"])

    def test_one_file_formats(self):
        f = self.write("notes.txt", "data " * 1000)
        for fmt, ext in (("gz", ".gz"), ("bz2", ".bz2"), ("xz", ".xz")):
            with self.subTest(fmt=fmt):
                r = self.do_compress([f], "notes.txt", fmt)
                self.assertEqual(r["name"], "notes.txt" + ext)
                moved = self.p("moved.txt")
                os.replace(f, moved)
                out = self.do_extract(r["path"])
                self.assertEqual(out["name"], "notes.txt")
                self.assertEqual(open(out["path"], "rb").read(), b"data " * 1000)
                os.replace(out["path"], f)
                os.remove(moved)

    def test_gzip_header_does_not_leak_the_temp_name(self):
        f = self.write("n.txt", "hi")
        r = self.do_compress([f], "n.txt", "gz")
        raw = open(r["path"], "rb").read()
        self.assertIn(b"n.txt", raw[:40])
        self.assertNotIn(b".tmp", raw[:60])

    def test_one_file_formats_refuse_folders_and_several_files(self):
        d = self.sample_tree()
        a = self.write("a", "a")
        b = self.write("b", "b")
        for srcs in ([d], [a, b]):
            with self.assertRaises(FileOpError):
                self.do_compress(srcs, "x", "gz")

    def test_no_temp_files_are_left_behind(self):
        src = self.sample_tree()
        self.do_compress([src], "x", "tar.gz")
        self.do_extract(self.p("x.tar.gz"))
        leftovers = [n for n in os.listdir(self.work) if ".tmp" in n or n.startswith(".rconsole-extract")]
        self.assertEqual(leftovers, [])

    def test_symlinks_are_stored_as_links_not_followed(self):
        outside = os.path.join(self.tmp, "secret.txt")
        open(outside, "w").write("TOP SECRET")
        os.makedirs(self.p("d"))
        os.symlink(outside, self.p("d", "evil"))
        for fmt in ("zip", "tar.gz"):
            r = self.do_compress([self.p("d")], "d-" + fmt, fmt)
            if fmt == "zip":
                with zipfile.ZipFile(r["path"]) as zf:
                    self.assertEqual(zf.read("d/evil"), outside.encode())
                    self.assertTrue(stat.S_ISLNK(zf.getinfo("d/evil").external_attr >> 16))
            else:
                with tarfile.open(r["path"]) as tf:
                    self.assertTrue(tf.getmember("d/evil").issym())
            self.assertNotIn(b"TOP SECRET", open(r["path"], "rb").read() if fmt == "zip" else b"".join(
                tarfile.open(r["path"]).extractfile(m).read() for m in tarfile.open(r["path"]) if m.isreg()))

    def test_compress_validates_names_and_formats(self):
        f = self.write("a", "a")
        for bad in ("", ".", "..", "a/b", "a\\b", "x" * 300):
            with self.assertRaises(FileOpError):
                self.do_compress([f], bad, "zip")
        with self.assertRaises(FileOpError):
            self.do_compress([f], "x", "bogus")
        with self.assertRaises(FileOpError):
            self.do_compress([], "x", "zip")


class SafeExtractTests(Base):
    def zip_with(self, entries, name="evil.zip"):
        path = self.p(name)
        with zipfile.ZipFile(path, "w") as zf:
            for spec in entries:
                if isinstance(spec, tuple) and spec[0] == "link":
                    zi = zipfile.ZipInfo(spec[1])
                    zi.external_attr = 0o120777 << 16
                    zf.writestr(zi, spec[2])
                else:
                    zf.writestr(spec[0], spec[1])
        return path

    def tar_with(self, members, name="evil.tar"):
        path = self.p(name)
        with tarfile.open(path, "w") as tf:
            for m in members:
                ti = tarfile.TarInfo(m["name"])
                ti.type = m.get("type", tarfile.REGTYPE)
                data = m.get("data", b"")
                ti.size = len(data) if ti.type == tarfile.REGTYPE else 0
                ti.linkname = m.get("link", "")
                ti.mode = m.get("mode", 0o644)
                tf.addfile(ti, io.BytesIO(data) if ti.type == tarfile.REGTYPE else None)
        return path

    def test_dotdot_is_refused_and_nothing_escapes(self):
        victim = os.path.join(self.tmp, "evil.txt")
        for maker, name in ((self.zip_with, "../../evil.txt"), (self.tar_with, "../../evil.txt")):
            arch = maker([("../../evil.txt", b"pwn")] if maker == self.zip_with else [{"name": name, "data": b"pwn"}])
            with self.assertRaises(FileOpError):
                self.do_extract(arch, "here")
            self.assertFalse(os.path.exists(victim))
            self.assertFalse(os.path.exists(os.path.join(self.work, "evil.txt")))

    def test_absolute_names_land_inside_not_at_the_real_path(self):
        target = os.path.join(self.tmp, "abs-target")
        os.makedirs(target)
        arch = self.zip_with([(target + "/abs.txt", b"inside")])
        out = self.do_extract(arch, "folder")
        self.assertFalse(os.path.exists(os.path.join(target, "abs.txt")))
        found = [os.path.join(d, f) for d, _s, fs in os.walk(out["path"]) for f in fs]
        self.assertEqual(len(found), 1)
        self.assertTrue(os.path.realpath(found[0]).startswith(os.path.realpath(self.work)))

    def test_windows_drive_names_are_refused(self):
        with self.assertRaises(FileOpError):
            self.do_extract(self.zip_with([("C:/Windows/x.txt", b"x")]), "here")

    def test_cannot_write_through_a_symlink_zip(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        arch = self.zip_with([("link", ) if False else ("link", "link", outside), ("link/pwned.txt", b"pwn")])
        out = self.do_extract(arch, "here")
        self.assertEqual(os.listdir(outside), [])
        self.assertGreaterEqual(out["skipped"], 1)

    def test_cannot_write_through_a_symlink_tar(self):
        outside = os.path.join(self.tmp, "outside2")
        os.makedirs(outside)
        arch = self.tar_with([{"name": "link", "type": tarfile.SYMTYPE, "link": outside},
                              {"name": "link/pwned.txt", "data": b"pwn"}])
        out = self.do_extract(arch, "here")
        self.assertEqual(os.listdir(outside), [])
        self.assertGreaterEqual(out["skipped"], 1)

    def test_existing_symlinked_folder_is_not_followed(self):
        outside = os.path.join(self.tmp, "outside3")
        os.makedirs(outside)
        os.symlink(outside, self.p("dir"))                     # a pre-existing link in the destination
        arch = self.tar_with([{"name": "dir/pwned.txt", "data": b"pwn"}])
        self.do_extract(arch, "here")
        self.assertEqual(os.listdir(outside), [])

    def test_devices_hardlinks_and_fifos_are_skipped(self):
        arch = self.tar_with([{"name": "ok.txt", "data": b"ok"},
                              {"name": "hard", "type": tarfile.LNKTYPE, "link": "ok.txt"},
                              {"name": "pipe", "type": tarfile.FIFOTYPE},
                              {"name": "dev", "type": tarfile.CHRTYPE}])
        out = self.do_extract(arch, "folder")
        self.assertEqual(sorted(os.listdir(out["path"])), ["ok.txt"])
        self.assertEqual(out["skipped"], 3)

    def test_permissions_cannot_make_files_setuid_or_unreadable(self):
        arch = self.tar_with([{"name": "x", "data": b"x", "mode": 0o4000}])      # setuid, no permission bits
        out = self.do_extract(arch, "folder")
        mode = os.stat(os.path.join(out["path"], "x")).st_mode
        self.assertFalse(mode & stat.S_ISUID)
        self.assertTrue(mode & 0o600 == 0o600)

    def test_here_never_overwrites(self):
        self.write("same.txt", "KEEP")
        arch = self.zip_with([("same.txt", b"NEW"), ("fresh.txt", b"F")])
        out = self.do_extract(arch, "here")
        self.assertEqual(open(self.p("same.txt")).read(), "KEEP")
        self.assertEqual(open(self.p("fresh.txt")).read(), "F")
        self.assertEqual(out["skipped"], 1)

    def test_zip_bomb_is_stopped_by_the_declared_size(self):
        arch = self.zip_with([("big.bin", b"\0" * (4 * 1024 * 1024))])
        with self.assertRaises(FileOpError) as cm:
            self.do_extract(arch, "folder", limit=1024 * 1024)
        self.assertIn("MB", str(cm.exception))
        self.assertEqual([n for n in os.listdir(self.work) if n.startswith(".rconsole-extract")], [])

    def test_tar_bomb_is_stopped_while_writing(self):
        path = self.p("bomb.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            ti = tarfile.TarInfo("big.bin")
            ti.size = 4 * 1024 * 1024
            tf.addfile(ti, io.BytesIO(b"\0" * ti.size))
        with self.assertRaises(FileOpError):
            self.do_extract(path, "folder", limit=1024 * 1024)
        self.assertEqual([n for n in os.listdir(self.work) if n.startswith(".rconsole-extract") or n == "bomb"], [])

    def test_gzip_bomb_single_file(self):
        path = self.p("z.gz")
        with gzip.open(path, "wb") as f:
            f.write(b"\0" * (4 * 1024 * 1024))
        with self.assertRaises(FileOpError):
            self.do_extract(path, limit=1024 * 1024)
        self.assertEqual(os.listdir(self.work), ["z.gz"])

    def test_too_many_entries(self):
        arch = self.zip_with([(f"f{i}.txt", b"x") for i in range(20)])
        with mock.patch.object(fileops, "MAX_ENTRIES", 5):
            with self.assertRaises(FileOpError):
                self.do_extract(arch, "folder")

    def test_corrupt_and_wrong_files(self):
        bad = self.write("bad.zip", b"this is not a zip")
        with self.assertRaises(FileOpError):
            self.do_extract(bad)
        badtar = self.write("bad.tar.gz", b"\x1f\x8b" + b"garbage" * 10)
        with self.assertRaises(FileOpError):
            self.do_extract(badtar)
        notarc = self.write("plain.txt", "hello")
        with self.assertRaises(FileOpError):
            self.do_extract(notarc)

    def test_cancel_cleans_up(self):
        src = self.write("d/big.bin", os.urandom(2 * 1024 * 1024))
        r = self.do_compress([self.p("d")], "d", "zip")
        job = make_job()
        job._cancel.set()
        with self.assertRaises(Cancelled):
            self.do_extract(r["path"], "folder", job=job)
        with self.assertRaises(Cancelled):
            fileops.compress(job, [self.p("d")], self.work, "again", "tar.gz", True)
        self.assertEqual(sorted(os.listdir(self.work)), ["d", "d.zip"])
        self.assertTrue(os.path.exists(src))

    def test_password_protected_zip_is_reported(self):
        if not HAVE_7Z:
            self.skipTest("needs 7z to make an encrypted zip")
        self.write("src/a.txt", "secret")
        exe = next(shutil.which(t) for t in ("7zz", "7z", "7za") if shutil.which(t))
        import subprocess
        subprocess.run([exe, "a", "-tzip", "-pPASS", "-mem=ZipCrypto", self.p("enc.zip"), self.p("src")],
                       check=True, capture_output=True)
        with self.assertRaises(FileOpError) as cm:
            self.do_extract(self.p("enc.zip"), "here")
        self.assertIn("password", str(cm.exception).lower())


class ProgressTests(Base):
    def test_progress_reaches_the_total(self):
        self.write("d/a.bin", os.urandom(300 * 1024))
        self.write("d/b.bin", os.urandom(200 * 1024))
        job = make_job()
        res = fileops.compress(job, [self.p("d")], self.work, "d", "zip", True)
        self.assertEqual(job.total, 500 * 1024)
        self.assertEqual(job.done, 500 * 1024)
        job2 = make_job()
        self.do_extract(res["path"], job=job2)
        self.assertEqual(job2.done, job2.total)


@unittest.skipUnless(HAVE_BSDTAR, "bsdtar (libarchive-tools) not installed")
class ExternalToolTests(Base):
    def test_rar_fixtures_extract_through_bsdtar(self):
        cases = {
            "rar5_stored.rar": ("helloworld.txt", b"hello libarchive test suite!\n"),
            "rar3.rar": ("test.txt", b"test text document\r\n"),
        }
        for fx, (inner, content) in cases.items():
            with self.subTest(fixture=fx):
                shutil.copy(FIXTURES / fx, self.p(fx))
                out = self.do_extract(self.p(fx), "folder", trusted=False)
                self.assertEqual(open(os.path.join(out["path"], inner), "rb").read(), content)
        shutil.copy(FIXTURES / "rar5_compressed.rar", self.p("c.rar"))
        out = self.do_extract(self.p("c.rar"), "here", trusted=False)
        self.assertEqual(os.path.getsize(self.p("test.bin")), 1200)
        self.assertEqual(out["skipped"], 0)

    def test_7z_round_trip_with_bsdtar_for_a_confined_user(self):
        src = self.sample_tree()
        res = self.do_compress([src], "pack", "7z", trusted=False)
        self.assertEqual(res["name"], "pack.7z")
        out = self.do_extract(res["path"], trusted=False)
        got = self.tree(out["path"])
        want = self.tree(src)
        self.assertEqual({k: v for k, v in got.items() if v[0] == "file"}, {k: v for k, v in want.items() if v[0] == "file"})
        self.assertEqual(got["link"], ("link", "readme.txt"))

    def test_confined_user_cannot_use_other_tools(self):
        # only bsdtar is allowed for confined users: with it hidden, 7z / rar are simply unavailable
        with mock.patch.object(fileops, "_tool_bsdtar", return_value=None):
            self.assertFalse(fileops.can_create("7z", False))
            self.assertNotIn(".7z", fileops.extract_exts(False))
            f = self.write("a", "a")
            with self.assertRaises(FileOpError):
                self.do_compress([f], "a", "7z", trusted=False)
            shutil.copy(FIXTURES / "rar5_stored.rar", self.p("x.rar"))
            with self.assertRaises(FileOpError) as cm:
                self.do_extract(self.p("x.rar"), trusted=False)
            self.assertIn("bsdtar", str(cm.exception))

    def test_external_extraction_respects_the_size_limit(self):
        src = self.write("big/z.bin", b"\0" * (3 * 1024 * 1024))
        res = self.do_compress([self.p("big")], "big", "7z", trusted=False)
        os.remove(src)
        with self.assertRaises(FileOpError):
            self.do_extract(res["path"], limit=1024 * 1024, trusted=False)
        self.assertEqual([n for n in os.listdir(self.work) if n.startswith(".rconsole-extract")], [])

    def test_external_here_merges_without_overwriting(self):
        self.write("keep.txt", "KEEP")
        src = self.write("pk/keep.txt", "NEW")
        self.write("pk/new.txt", "N")
        res = self.do_compress([self.p("pk")], "pk", "7z", trusted=False)
        shutil.rmtree(self.p("pk"))
        os.makedirs(self.p("pk"))
        self.write("pk/keep.txt", "OLD")
        out = self.do_extract(res["path"], "here", trusted=False)
        self.assertEqual(open(self.p("pk", "keep.txt")).read(), "OLD")
        self.assertEqual(open(self.p("pk", "new.txt")).read(), "N")
        self.assertEqual(out["skipped"], 1)
        self.assertTrue(os.path.exists(src))


@unittest.skipUnless(HAVE_7Z, "7z / p7zip not installed")
class SevenZipToolTests(Base):
    def test_trusted_7z_create_and_extract(self):
        with mock.patch.object(fileops, "_tool_bsdtar", return_value=None):     # force the 7z tool
            f = self.write("pk/a.txt", "AAA")
            res = self.do_compress([self.p("pk")], "pk", "7z", trusted=True)
            shutil.rmtree(self.p("pk"))
            out = self.do_extract(res["path"], trusted=True)
            self.assertEqual(open(os.path.join(out["path"], "a.txt")).read(), "AAA")
            self.assertTrue(f)

    def test_7z_refuses_trees_with_symlinks(self):
        with mock.patch.object(fileops, "_tool_bsdtar", return_value=None):
            os.makedirs(self.p("pk"))
            os.symlink("/etc/passwd", self.p("pk", "l"))
            with self.assertRaises(FileOpError) as cm:
                self.do_compress([self.p("pk")], "pk", "7z", trusted=True)
            self.assertIn("symbolic", str(cm.exception))


class CapabilityTests(unittest.TestCase):
    def test_native_formats_are_always_there(self):
        caps = fileops.capabilities(False)
        ids = {c["id"] for c in caps["compress"]}
        self.assertTrue({"zip", "tar", "tar.gz", "tar.bz2", "tar.xz", "gz", "bz2", "xz"} <= ids)
        self.assertTrue({".zip", ".tar.gz", ".tgz", ".gz"} <= set(caps["extract"]))
        self.assertTrue(all("single" in c and c["ext"].startswith(".") for c in caps["compress"]))

    def test_rar_creation_needs_the_rar_tool_and_a_trusted_user(self):
        with mock.patch.object(fileops, "_which", side_effect=lambda *n: "/usr/bin/rar" if "rar" in n else None):
            self.assertTrue(fileops.can_create("rar", True))
            self.assertFalse(fileops.can_create("rar", False))
        with mock.patch.object(fileops, "_which", return_value=None):
            self.assertFalse(fileops.can_create("rar", True))
            self.assertFalse(fileops.can_create("7z", True))

    def test_name_helpers(self):
        self.assertEqual(fileops.archive_ext("a.tar.gz"), ".tar.gz")
        self.assertEqual(fileops.archive_ext("A.TGZ"), ".tgz")
        self.assertEqual(fileops.archive_ext("a.gz"), ".gz")
        self.assertEqual(fileops.archive_ext(".zip"), "")
        self.assertEqual(fileops.archive_ext("notes.txt"), "")
        self.assertEqual(fileops.stem_of("proj-1.0.tar.xz"), "proj-1.0")
        self.assertEqual(fileops.stem_of("x"), "x")
        self.assertTrue(fileops.valid_name("ok name.zip"))
        self.assertFalse(fileops.valid_name("a/b"))


class PropertiesAndDeleteTests(Base):
    def test_file_properties(self):
        f = self.write("a.zip", b"x")
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("one.txt", "11111")
            zf.writestr("two.txt", "222")
        os.chmod(f, 0o640)
        props = fileops.properties(f, "/virtual/a.zip", unrestricted=False)
        self.assertEqual(props["kind"], "file")
        self.assertEqual(props["path"], "/virtual/a.zip")
        self.assertEqual(props["mode"], "0640")
        self.assertEqual(props["mode_text"], "-rw-r-----")
        self.assertEqual(props["mime"], "application/zip")
        self.assertEqual(props["archive"], {"entries": 2, "uncompressed": 8})
        self.assertIsNone(props["owner"]["user"])               # names only for the host admin
        self.assertIn("mtime", props)
        self.assertTrue(props["readable"])

    def test_owner_names_only_in_host_mode(self):
        f = self.write("a.txt", "x")
        props = fileops.properties(f, "/a.txt", unrestricted=True)
        self.assertEqual(props["owner"]["uid"], os.stat(f).st_uid)
        if sys.platform != "win32":
            import pwd
            try:
                expect = pwd.getpwuid(os.stat(f).st_uid).pw_name
            except KeyError:
                expect = None
            self.assertEqual(props["owner"]["user"], expect)

    def test_folder_totals_do_not_follow_links(self):
        self.write("d/a", "12345")
        self.write("d/sub/b", "123")
        os.makedirs(self.p("d", "emptydir"))
        outside = os.path.join(self.tmp, "big")
        os.makedirs(outside)
        open(os.path.join(outside, "huge"), "wb").write(b"0" * 100000)
        os.symlink(outside, self.p("d", "link"))
        props = fileops.properties(self.p("d"), "/d", False)
        c = props["contents"]
        self.assertEqual((c["files"], c["folders"]), (3, 2))     # a, b, link counted as a file; sub, emptydir
        self.assertLess(props["size"], 1000)
        self.assertFalse(c["partial"])

    def test_big_folder_reports_partial(self):
        for i in range(30):
            self.write(f"d/f{i}", "x")
        with mock.patch.object(fileops.time, "time", side_effect=[0.0] + [10.0] * 200):
            props = fileops.properties(self.p("d"), "/d", False)
        self.assertTrue(props["contents"]["partial"])

    def test_symlink_properties(self):
        os.symlink("nowhere", self.p("dangling"))
        props = fileops.properties(self.p("dangling"), "/dangling", False)
        self.assertEqual(props["kind"], "link")
        self.assertEqual(props["target"], "nowhere")
        self.assertFalse(props["target_exists"])

    def test_delete_many(self):
        self.write("a.txt")
        self.write("d/x/y.txt")
        outside = os.path.join(self.tmp, "keepme")
        os.makedirs(outside)
        open(os.path.join(outside, "f"), "w").write("keep")
        os.symlink(outside, self.p("lnk"))
        deleted, failed = fileops.delete_paths([self.p("a.txt"), self.p("d"), self.p("lnk"), self.p("ghost")])
        self.assertEqual(deleted, 3)
        self.assertEqual([f["path"] for f in failed], [self.p("ghost")])
        self.assertEqual(os.listdir(self.work), [])
        self.assertTrue(os.path.exists(os.path.join(outside, "f")))     # the link target survives


class JobTests(unittest.TestCase):
    def test_one_running_job_per_user_and_isolation(self):
        gate = threading.Event()

        def slow(job):
            gate.wait(5)
            return {"ok": 1}

        j1 = fileops.start_job("alice", "t", slow)
        with self.assertRaises(fileops.JobBusy):
            fileops.start_job("alice", "t", slow)
        j_other = fileops.start_job("bob", "t", lambda job: {"b": 1})        # other users are unaffected
        self.assertIsNotNone(fileops.get_job("alice", j1.id))
        self.assertIsNone(fileops.get_job("bob", j1.id))                       # not visible to others
        gate.set()
        for _ in range(100):
            if j1.state != "running" and j_other.state != "running":
                break
            time.sleep(0.02)
        self.assertEqual(j1.state, "done")
        self.assertEqual(j1.result, {"ok": 1})
        fileops.start_job("alice", "t", lambda job: None)                       # free again once finished

    def test_errors_and_cancel_end_in_a_state(self):
        def boom(job):
            raise FileOpError("nope")

        j = fileops.start_job("carol", "t", boom)
        for _ in range(100):
            if j.state != "running":
                break
            time.sleep(0.02)
        self.assertEqual((j.state, j.error), ("error", "nope"))

        def waits(job):
            while True:
                job.check()
                time.sleep(0.01)

        j2 = fileops.start_job("carol", "t", waits)
        fileops.cancel_job("carol", j2.id)
        for _ in range(100):
            if j2.state != "running":
                break
            time.sleep(0.02)
        self.assertEqual(j2.state, "cancelled")

        def crash(job):
            raise RuntimeError("bug")

        j3 = fileops.start_job("carol", "t", crash)
        for _ in range(100):
            if j3.state != "running":
                break
            time.sleep(0.02)
        self.assertEqual(j3.state, "error")
        self.assertIn("bug", j3.error)

    def test_finished_jobs_are_pruned(self):
        j = fileops.start_job("dave", "t", lambda job: None)
        for _ in range(100):
            if j.state != "running":
                break
            time.sleep(0.02)
        j.finished = time.time() - fileops.JOB_KEEP_SECONDS - 1
        fileops.start_job("dave", "t", lambda job: None)
        self.assertIsNone(fileops.get_job("dave", j.id))


if __name__ == "__main__":
    unittest.main()
