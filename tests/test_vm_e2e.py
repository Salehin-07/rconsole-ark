"""End-to-end tests against a REAL proot VM built from a local busybox rootfs.

Skipped unless both ``proot`` (PATH or RCONSOLE_VM_PROOT) and a static
``busybox`` (PATH or RCONSOLE_TEST_BUSYBOX) are available. The rootfs is served
from a local HTTP server, so the download/verify/extract pipeline is exercised
without touching the internet.
"""
import hashlib
import http.server
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

import helpers  # noqa: F401

import auth
import config
import sandbox

PROOT = os.environ.get("RCONSOLE_VM_PROOT") or shutil.which("proot")
BUSYBOX = os.environ.get("RCONSOLE_TEST_BUSYBOX") or shutil.which("busybox")
APPLETS = ("sh ls cat id echo pwd env mkdir touch rm sleep tr wc uname ln cp mv "
           "printf test head tail grep sed").split()


def build_rootfs_tarball(dest):
    with tarfile.open(dest, "w:gz") as tf:
        for d in ("bin", "etc", "proc", "dev", "tmp", "root", "usr"):
            ti = tarfile.TarInfo(d)
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o555 if d == "usr" else 0o755   # read-only dir: must be fixed up
            tf.addfile(ti)
        tf.add(BUSYBOX, "bin/busybox")
        for a in APPLETS:
            ti = tarfile.TarInfo(f"bin/{a}")
            ti.type, ti.linkname, ti.mode = tarfile.SYMTYPE, "busybox", 0o777
            tf.addfile(ti)
        data = b'PRETTY_NAME="TestLinux 1"\n'
        ti = tarfile.TarInfo("etc/os-release")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
        ti = tarfile.TarInfo("etc/abs-link")          # absolute symlink inside the VM
        ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/os-release"
        tf.addfile(ti)
        ti = tarfile.TarInfo("etc/escape")            # tries to point at the host
        ti.type, ti.linkname = tarfile.SYMTYPE, "../../../../../../etc/hostname"
        tf.addfile(ti)


@unittest.skipUnless(PROOT and BUSYBOX, "needs proot and busybox")
class VmEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["RCONSOLE_VM_PROOT"] = PROOT
        cls.srv_dir = Path(tempfile.mkdtemp(prefix="rootfs-srv-"))
        tarball = cls.srv_dir / "testroot.tar.gz"
        build_rootfs_tarball(tarball)
        cls.sha = hashlib.sha256(tarball.read_bytes()).hexdigest()
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=str(cls.srv_dir), **k)
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        port = cls.httpd.server_address[1]
        os.environ["RCONSOLE_VM_ROOTFS_URL"] = f"http://127.0.0.1:{port}/testroot.tar.gz"
        os.environ["RCONSOLE_VM_ROOTFS_SHA256"] = cls.sha

        helpers.wipe_vms()
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()
        auth.create_user("alice", "longenough1")
        ok, msg = sandbox.install("alice", wait=True)
        assert ok, msg

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        helpers.wipe_vms()
        shutil.rmtree(cls.srv_dir, ignore_errors=True)
        for k in ("RCONSOLE_VM_ROOTFS_URL", "RCONSOLE_VM_ROOTFS_SHA256"):
            os.environ.pop(k, None)

    def run_in_vm(self, cmd, sid="alice", cwd="/root"):
        sp = sandbox.spec(sid, cmd, cwd)
        return subprocess.run(sp.argv, cwd=sp.cwd, env=sp.env, capture_output=True,
                              text=True, timeout=30)

    def test_installed_and_described(self):
        self.assertTrue(sandbox.installed("alice"))
        d = sandbox.describe("alice")
        self.assertEqual(d["state"], "ready")
        self.assertEqual(d["distro"], "TestLinux 1")

    def test_user_is_root_in_vm(self):
        p = self.run_in_vm("id")
        self.assertIn("uid=0", p.stdout, p.stderr)

    def test_host_filesystem_is_invisible(self):
        p = self.run_in_vm("ls /home; cat /etc/passwd")
        self.assertNotIn("claude", p.stdout)            # host user dirs not mounted
        self.assertFalse((Path("/etc/passwd")).read_text() in p.stdout)

    def test_home_is_persistent_bind(self):
        self.run_in_vm("echo persisted > /root/keep.txt")
        self.assertEqual((sandbox.home_path("alice") / "keep.txt").read_text().strip(),
                         "persisted")

    def test_pipes_and_redirects_survive(self):
        p = self.run_in_vm("echo hello | tr a-z A-Z")
        self.assertEqual(p.stdout.strip(), "HELLO")

    def test_secrets_not_in_vm_environment(self):
        os.environ["RCONSOLE_SECRET"] = "topsecret"
        try:
            p = self.run_in_vm("env")
            self.assertNotIn("topsecret", p.stdout)
            self.assertIn("HOME=/root", p.stdout)
        finally:
            os.environ.pop("RCONSOLE_SECRET")

    def test_read_only_dirs_from_tarball_are_writable(self):
        self.assertTrue(os.access(sandbox.rootfs_path("alice") / "usr", os.W_OK))

    def test_fs_view_maps_home_and_rootfs(self):
        view = sandbox.fs("alice")
        p = view.host("/root/new.txt")
        self.assertEqual(p.parent, sandbox.home_path("alice").resolve())
        self.assertEqual(view.virt(p), "/root/new.txt")
        self.assertEqual(view.virt(view.host("/etc/os-release")), "/etc/os-release")

    def test_fs_view_blocks_host_escape(self):
        view = sandbox.fs("alice")
        host_hostname = Path("/etc/hostname")
        p = view.host("/etc/escape")
        self.assertNotEqual(p.resolve(), host_hostname.resolve())
        self.assertTrue(str(p).startswith(str(sandbox.rootfs_path("alice").resolve())))
        self.assertEqual(view.host("/etc/abs-link").name, "os-release")

    def test_vm_cwd_is_honoured_and_validated(self):
        self.assertEqual(self.run_in_vm("pwd", cwd="/etc").stdout.strip(), "/etc")
        self.assertEqual(self.run_in_vm("pwd", cwd="/nope").stdout.strip(), "/root")

    def test_forced_host_blocked_for_user(self):
        with self.assertRaises(sandbox.PermissionDenied):
            sandbox.spec("alice", "@host id", "/")

    def test_admin_can_use_vm_and_switch_modes(self):
        ok, msg = sandbox.install("ark", wait=True)   # cached tarball
        self.assertTrue(ok, msg)
        self.assertIn("uid=0", self.run_in_vm("@vm id", sid="ark").stdout)
        self.assertTrue(sandbox.set_mode("ark", "vm")[0])
        self.assertEqual(sandbox.effective_mode("ark"), sandbox.VM)
        self.assertEqual(sandbox.default_cwd("ark"), "/root")
        self.assertTrue(sandbox.set_mode("ark", "host")[0])
        self.assertEqual(sandbox.spec("ark", "id", "/tmp").mode, sandbox.HOST)

    def test_bad_checksum_rejected(self):
        os.environ["RCONSOLE_VM_ROOTFS_SHA256"] = "0" * 64
        try:
            cached = config.VM_DIR / "cache" / "testroot.tar.gz"
            cached.unlink(missing_ok=True)
            with self.assertRaises(sandbox.SandboxError):
                sandbox._fetch_rootfs()
        finally:
            os.environ["RCONSOLE_VM_ROOTFS_SHA256"] = self.sha

    def test_reset_keeps_home_remove_deletes_it(self):
        auth.create_user("carol", "longenough1")
        self.assertTrue(sandbox.install("carol", wait=True)[0])
        (sandbox.home_path("carol") / "f").write_text("x")
        sandbox.reset("carol", keep_home=True)
        self.assertFalse(sandbox.installed("carol"))
        self.assertTrue((sandbox.home_path("carol") / "f").exists())
        sandbox.reset("carol", keep_home=False)
        self.assertFalse(sandbox.vm_base("carol").exists())


if __name__ == "__main__":
    unittest.main()
