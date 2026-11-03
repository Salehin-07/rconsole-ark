"""File manager API: properties, batch delete, compress / extract jobs.

The confined-user checks build a real ``sandbox.FsView`` (a VM-style root plus a bind for /root)
instead of needing proot, so the path-confinement code under test is the production code.
"""
import io
import os
import shutil
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

import helpers  # noqa: F401  (puts the project on sys.path)
import fileops
import sandbox
from test_app import Base, rapp


class FileManagerBase(Base):
    def setUp(self):
        super().setUp()
        self.login()
        self.root = os.path.join(os.environ["RCONSOLE_DATA_DIR"], "fm")
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def write(self, rel, data=b"x"):
        path = self.p(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        return path

    def wait(self, job_id, client=None, timeout=15):
        client = client or self.c
        end = time.time() + timeout
        while time.time() < end:
            j = client.get(f"/api/archive/{job_id}").get_json()
            if j["state"] != "running":
                return j
            time.sleep(0.02)
        self.fail("job did not finish")

    def start(self, body, client=None, expect=200):
        r = (client or self.c).post("/api/archive", json=body)
        self.assertEqual(r.status_code, expect, r.data)
        return r.get_json()


class AuthAndCapabilityTests(FileManagerBase):
    def test_everything_needs_a_login(self):
        anon = rapp.app.test_client()
        self.assertEqual(anon.get("/api/props", query_string={"path": self.root}).status_code, 401)
        self.assertEqual(anon.post("/api/archive", json={"op": "compress", "items": [self.root]}).status_code, 401)
        self.assertEqual(anon.get("/api/archive/abc").status_code, 401)
        self.assertEqual(anon.post("/api/archive/abc/cancel").status_code, 401)
        self.assertEqual(anon.post("/api/fs", json={"action": "delete_many", "paths": [self.root]}).status_code, 401)

    def test_dir_listing_advertises_the_archive_formats(self):
        arc = self.c.get("/api/dir", query_string={"path": self.root}).get_json()["archives"]
        ids = {c["id"] for c in arc["compress"]}
        self.assertTrue({"zip", "tar.gz", "gz"} <= ids)
        self.assertIn(".zip", arc["extract"])
        self.assertTrue(next(c for c in arc["compress"] if c["id"] == "gz")["single"])

    def test_archive_posts_are_csrf_protected(self):
        r = self.c.post("/api/archive", json={"op": "compress", "items": [self.root]},
                        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/fs", json={"action": "delete_many", "paths": [self.root]},
                        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)


class PropsTests(FileManagerBase):
    def test_file_and_folder(self):
        f = self.write("a.txt", "12345")
        r = self.c.get("/api/props", query_string={"path": f}).get_json()
        self.assertEqual((r["name"], r["kind"], r["size"], r["path"]), ("a.txt", "file", 5, f))
        self.assertEqual(r["mime"], "text/plain")
        self.write("d/x", "1")
        self.write("d/s/y", "22")
        r = self.c.get("/api/props", query_string={"path": self.p("d")}).get_json()
        self.assertEqual(r["kind"], "folder")
        self.assertEqual((r["contents"]["files"], r["contents"]["folders"], r["size"]), (2, 1, 3))

    def test_symlink_is_described_not_followed(self):
        os.symlink("/definitely/missing", self.p("l"))
        r = self.c.get("/api/props", query_string={"path": self.p("l")}).get_json()
        self.assertEqual((r["kind"], r["target"], r["target_exists"]), ("link", "/definitely/missing", False))

    def test_errors(self):
        self.assertEqual(self.c.get("/api/props").status_code, 400)
        self.assertEqual(self.c.get("/api/props", query_string={"path": self.p("nope")}).status_code, 404)


class DeleteManyTests(FileManagerBase):
    def test_deletes_files_folders_and_links_without_following(self):
        a = self.write("a.txt")
        self.write("d/deep/f.txt")
        keep = self.write("keep/precious.txt", "keep")
        os.symlink(self.p("keep"), self.p("lnk"))
        r = self.c.post("/api/fs", json={"action": "delete_many", "paths": [a, self.p("d"), self.p("lnk"), self.p("ghost")]})
        j = r.get_json()
        self.assertEqual(j["deleted"], 3)
        self.assertFalse(j["ok"])
        self.assertEqual([f["path"] for f in j["failed"]], [self.p("ghost")])
        self.assertEqual(sorted(os.listdir(self.root)), ["keep"])
        self.assertTrue(os.path.exists(keep))

    def test_bad_input_and_roots(self):
        for body in ({"paths": []}, {"paths": "x"}, {"paths": [""]}, {}):
            r = self.c.post("/api/fs", json=dict(body, action="delete_many"))
            self.assertEqual(r.status_code, 400, body)
        r = self.c.post("/api/fs", json={"action": "delete_many", "paths": ["/"]})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(os.path.exists("/etc"))


class ArchiveJobTests(FileManagerBase):
    def test_compress_then_extract_round_trip(self):
        self.write("proj/a.txt", "AAA")
        self.write("proj/sub/b.txt", "BBB")
        self.write("solo.txt", "S")
        j = self.start({"op": "compress", "items": [self.p("proj"), self.p("solo.txt")], "format": "zip", "name": "bundle"})
        done = self.wait(j["job"])
        self.assertEqual(done["state"], "done", done)
        self.assertEqual(done["result"]["name"], "bundle.zip")
        self.assertEqual(done["result"]["path"], self.p("bundle.zip"))
        with zipfile.ZipFile(self.p("bundle.zip")) as zf:
            self.assertEqual(sorted(n for n in zf.namelist() if not n.endswith("/")), ["proj/a.txt", "proj/sub/b.txt", "solo.txt"])
        # the same name again never overwrites
        j2 = self.wait(self.start({"op": "compress", "items": [self.p("solo.txt")], "format": "zip", "name": "bundle.zip"})["job"])
        self.assertEqual(j2["result"]["name"], "bundle (2).zip")
        shutil.rmtree(self.p("proj"))
        ex = self.wait(self.start({"op": "extract", "items": [self.p("bundle.zip")], "mode": "folder"})["job"])
        self.assertEqual(ex["state"], "done", ex)
        self.assertEqual(ex["result"]["files"], 3)
        self.assertEqual(open(os.path.join(ex["result"]["path"], "proj", "a.txt")).read(), "AAA")

    def test_extract_here_and_several_archives(self):
        for n in ("one", "two"):
            with zipfile.ZipFile(self.p(f"{n}.zip"), "w") as zf:
                zf.writestr(f"{n}.txt", n)
        ex = self.wait(self.start({"op": "extract", "items": [self.p("one.zip"), self.p("two.zip")], "mode": "here"})["job"])
        self.assertEqual(ex["state"], "done", ex)
        self.assertEqual(ex["result"]["files"], 2)
        self.assertEqual(open(self.p("one.txt")).read(), "one")
        self.assertEqual(open(self.p("two.txt")).read(), "two")

    def test_single_file_compression_and_default_name(self):
        f = self.write("log.txt", "line\n" * 100)
        done = self.wait(self.start({"op": "compress", "items": [f], "format": "gz"})["job"])
        self.assertEqual(done["result"]["name"], "log.txt.gz")      # no name given: the item name (so extracting gives log.txt back)
        bad = self.wait(self.start({"op": "compress", "items": [self.p("log.txt"), self.write("b.txt")], "format": "gz"})["job"])
        self.assertEqual(bad["state"], "error")
        self.assertIn("one regular file", bad["error"])

    def test_errors_surface_in_the_job(self):
        self.write("fake.zip", "not a zip")
        done = self.wait(self.start({"op": "extract", "items": [self.p("fake.zip")]})["job"])
        self.assertEqual(done["state"], "error")
        self.assertIn("ZIP", done["error"])
        self.assertEqual(os.listdir(self.root), ["fake.zip"])      # nothing left behind

    def test_validation(self):
        a = self.write("a.txt")
        self.start({"op": "zap", "items": [a]}, expect=400)
        self.start({"op": "compress", "items": []}, expect=400)
        self.start({"op": "compress", "items": [a], "format": "bogus"}, expect=400)
        self.start({"op": "compress", "items": [a], "format": "zip", "name": "a/b"}, expect=400)
        self.start({"op": "compress", "items": [self.p("missing")], "format": "zip"}, expect=404)
        b = self.write("sub/b.txt")
        self.start({"op": "compress", "items": [a, b], "format": "zip"}, expect=400)      # different folders
        self.start({"op": "compress", "items": ["/"], "format": "zip"}, expect=400)

    def test_one_job_at_a_time_status_cancel_and_isolation(self):
        a = self.write("a.txt")
        gate = threading.Event()

        def slow(job, *args, **kwargs):
            while not gate.is_set():
                job.check()
                time.sleep(0.01)
            return {"path": a, "name": "a.txt"}

        with mock.patch.object(fileops, "compress", slow):
            first = self.start({"op": "compress", "items": [a], "format": "zip"})
            busy = self.start({"op": "compress", "items": [a], "format": "zip"}, expect=409)
            self.assertIn("still running", busy["error"])
            self.assertEqual(self.c.get(f"/api/archive/{first['job']}").get_json()["state"], "running")
            # another account cannot see or cancel it
            other = rapp.app.test_client()
            self.login("alice", "longenough1", other)
            self.assertEqual(other.get(f"/api/archive/{first['job']}").status_code, 404)
            self.assertEqual(other.post(f"/api/archive/{first['job']}/cancel").status_code, 404)
            self.assertTrue(self.c.post(f"/api/archive/{first['job']}/cancel").get_json()["ok"])
            self.assertEqual(self.wait(first["job"])["state"], "cancelled")
        self.assertEqual(self.c.get("/api/archive/nope").status_code, 404)
        gate.set()

    def test_extract_size_limit_comes_from_the_setting(self):
        with zipfile.ZipFile(self.p("big.zip"), "w") as zf:
            zf.writestr("z.bin", b"\0" * (20 * 1024 * 1024))
        with mock.patch.object(rapp.config, "MAX_EXTRACT_MB", 16):
            done = self.wait(self.start({"op": "extract", "items": [self.p("big.zip")]})["job"])
        self.assertEqual(done["state"], "error")
        self.assertIn("limit", done["error"])


class ConfinedUserTests(FileManagerBase):
    """A confined (VM) account: the same endpoints, but every path stays inside the VM root."""

    def setUp(self):
        super().setUp()
        self.vm = tempfile.mkdtemp(prefix="vmroot-")
        self.addCleanup(shutil.rmtree, self.vm, ignore_errors=True)
        self.home = os.path.join(self.vm, "_home")
        os.makedirs(os.path.join(self.vm, "etc"))
        os.makedirs(self.home)
        open(os.path.join(self.vm, "etc", "hostname"), "w").write("vm-host\n")
        self.outside = os.path.join(self.root, "host-only")          # a host folder the VM must never touch
        os.makedirs(self.outside)
        view = sandbox.FsView(self.vm, {"/root": self.home})
        patcher = mock.patch.object(sandbox, "fs", lambda sid, forced=None: view)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.uc = rapp.app.test_client()
        self.login("alice", "longenough1", self.uc)

    def tarball(self, members, name="in.tar"):
        path = os.path.join(self.home, name)
        with tarfile.open(path, "w") as tf:
            for m in members:
                ti = tarfile.TarInfo(m["name"])
                ti.type = m.get("type", tarfile.REGTYPE)
                data = m.get("data", b"")
                ti.size = len(data) if ti.type == tarfile.REGTYPE else 0
                ti.linkname = m.get("link", "")
                tf.addfile(ti, io.BytesIO(data) if ti.type == tarfile.REGTYPE else None)
        return "/root/" + name

    def test_compress_and_extract_inside_the_vm(self):
        open(os.path.join(self.home, "a.txt"), "w").write("A")
        j = self.wait(self.start({"op": "compress", "items": ["/root/a.txt"], "format": "tar.gz", "name": "pack"}, self.uc)["job"], self.uc)
        self.assertEqual((j["state"], j["result"]["path"]), ("done", "/root/pack.tar.gz"))
        self.assertTrue(os.path.exists(os.path.join(self.home, "pack.tar.gz")))
        ex = self.wait(self.start({"op": "extract", "items": ["/root/pack.tar.gz"], "mode": "here"}, self.uc)["job"], self.uc)
        self.assertEqual(ex["state"], "done", ex)

    def test_archive_tricks_cannot_reach_the_host(self):
        # 1) ../ names are refused
        arc = self.tarball([{"name": "../../escape.txt", "data": b"x"}])
        j = self.wait(self.start({"op": "extract", "items": [arc], "mode": "here"}, self.uc)["job"], self.uc)
        self.assertEqual(j["state"], "error")
        # 2) "link -> a host folder, then a file below the link" writes nothing there
        arc = self.tarball([{"name": "ln", "type": tarfile.SYMTYPE, "link": self.outside},
                            {"name": "ln/pwned.txt", "data": b"x"}], "two.tar")
        j = self.wait(self.start({"op": "extract", "items": [arc], "mode": "here"}, self.uc)["job"], self.uc)
        self.assertEqual(j["state"], "done")
        self.assertEqual(os.listdir(self.outside), [])
        self.assertFalse(os.path.exists(os.path.join(self.vm, "escape.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "escape.txt")))

    def test_symlink_out_of_the_vm_is_never_read_into_an_archive(self):
        secret = self.write("host-only/secret.txt", "TOP SECRET")
        os.makedirs(os.path.join(self.home, "d"))
        os.symlink(secret, os.path.join(self.home, "d", "leak"))
        os.symlink(self.outside, os.path.join(self.home, "d", "leakdir"))
        for fmt in ("zip", "tar.gz"):
            j = self.wait(self.start({"op": "compress", "items": ["/root/d"], "format": fmt, "name": "d-" + fmt}, self.uc)["job"], self.uc)
            self.assertEqual(j["state"], "done", j)
            blob = open(os.path.join(self.home, j["result"]["name"]), "rb").read()
            if fmt == "tar.gz":
                import gzip
                blob = gzip.decompress(blob)
            self.assertNotIn(b"TOP SECRET", blob)

    def test_paths_outside_the_vm_resolve_inside_it(self):
        marker = self.write("host-only/marker", "keep")
        rel = os.path.relpath(marker, "/")
        r = self.uc.post("/api/fs", json={"action": "delete_many", "paths": ["/../../../" + rel, "/" + rel]})
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(r.get_json()["deleted"], 0)
        self.assertEqual(self.uc.get("/api/props", query_string={"path": "/../../../" + rel}).status_code, 404)
        self.assertEqual(self.uc.post("/api/fs", json={"action": "delete_many", "paths": ["/"]}).status_code, 400)

    def test_properties_do_not_name_host_users_and_list_virtual_paths(self):
        open(os.path.join(self.home, "p.txt"), "w").write("hi")
        r = self.uc.get("/api/props", query_string={"path": "/root/p.txt"}).get_json()
        self.assertEqual(r["path"], "/root/p.txt")
        self.assertIsNone(r["owner"]["user"])
        os.symlink("/etc/hostname", os.path.join(self.home, "ln"))
        r = self.uc.get("/api/props", query_string={"path": "/root/ln"}).get_json()
        self.assertEqual((r["kind"], r["path"], r["target"]), ("link", "/root/ln", "/etc/hostname"))

    def test_confined_users_do_not_get_admin_only_formats(self):
        arc = self.uc.get("/api/dir", query_string={"path": "/root"}).get_json()["archives"]
        self.assertNotIn("rar", {c["id"] for c in arc["compress"]})
        self.start({"op": "compress", "items": ["/etc/hostname"], "format": "rar"}, self.uc, expect=400)


if __name__ == "__main__":
    unittest.main()
