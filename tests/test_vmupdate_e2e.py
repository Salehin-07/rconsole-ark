"""`vm update` against a REAL proot VM (busybox rootfs with a fake apt-get/dpkg).

Skipped without proot and a static busybox. The fake package manager really runs inside
the VM, so this covers the actual launch path, output streaming, timeouts, the lock and
the rollback on real files.
"""
import hashlib
import http.server
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401

import auth
import cmd_vm
import sandbox
import state
import vmupdate

PROOT = os.environ.get("RCONSOLE_VM_PROOT") or shutil.which("proot")
BUSYBOX = os.environ.get("RCONSOLE_TEST_BUSYBOX") or shutil.which("busybox")

APT_GET = """#!/bin/sh
case "$*" in
  update) echo "Hit:1 http://repo stable InRelease"; exit 0 ;;
  "-s upgrade")
    if [ -f /var/lib/newer ]; then echo "Inst foo [1] (2 repo)"; echo "Inst bar [1] (2 repo)"; fi
    exit 0 ;;
esac
case "$*" in *upgrade*)
  echo "Unpacking foo ..."
  echo "${APT_CONFIG:-unset}:$DEBIAN_FRONTEND" > /var/lib/seen-env
  [ -f /var/lib/slow ] && sleep 8
  cp /usr/bin/tool /usr/bin/tool.new && echo v2 > /usr/bin/tool.new && mv /usr/bin/tool.new /usr/bin/tool
  echo "changed" >> /etc/config
  rm -f /var/lib/newer
  [ -f /var/lib/fail-upgrade ] && { echo "E: dpkg returned an error code (1)"; exit 100; }
  echo "Processing triggers ..."; exit 0 ;;
esac
exit 2
"""
APK = """#!/bin/sh
case "$1" in
  update) echo "fetch repositories"; exit 0 ;;
  info) exit 0 ;;
  upgrade)
    case "$*" in *simulate*) exit 0 ;; esac
    br=$(sed -n 's#.*/alpine/v\\([0-9.]*\\)/main#\\1#p' /etc/apk/repositories | head -1)
    echo "upgrading to $br"
    [ -f /var/lib/fail-$br ] && { echo "ERROR: $br is broken"; exit 99; }
    echo "$br.1" > /etc/alpine-release
    echo "(1/1) Upgrading musl"; exit 0 ;;
esac
exit 2
"""
DPKG = """#!/bin/sh
[ "$1" = "--audit" ] && [ -f /var/lib/broken ] && echo "The following packages are only half configured: foo"
exit 0
"""


@unittest.skipUnless(PROOT and BUSYBOX, "needs proot and busybox")
class UpdateE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_vm_e2e as e2e
        os.environ["RCONSOLE_VM_PROOT"] = PROOT
        cls.srv = Path(tempfile.mkdtemp(prefix="upd-srv-"))
        tar = cls.srv / "root.tar.gz"
        e2e.build_rootfs_tarball(tar)
        os.environ["RCONSOLE_VM_ROOTFS_SHA256"] = hashlib.sha256(tar.read_bytes()).hexdigest()
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=str(cls.srv), **k)
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        os.environ["RCONSOLE_VM_ROOTFS_URL"] = f"http://127.0.0.1:{cls.httpd.server_address[1]}/root.tar.gz"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        helpers.wipe_vms()
        shutil.rmtree(cls.srv, ignore_errors=True)
        for k in ("RCONSOLE_VM_ROOTFS_URL", "RCONSOLE_VM_ROOTFS_SHA256"):
            os.environ.pop(k, None)

    def setUp(self):
        helpers.wipe_vms()
        vmupdate.reset()
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()
        auth.create_user("alice", "longenough1")
        state.STATE.pop("alice", None)
        state.new_session("alice")
        state.set_vm("alice", None)
        state.set_mode("alice", None)
        ok, msg = sandbox.install("alice", "debian", wait=True)    # a "debian-family" VM
        self.assertTrue(ok, msg)
        self.root = sandbox.rootfs_path("alice", "debian")
        (self.root / "usr/bin").mkdir(parents=True, exist_ok=True)
        (self.root / "var/lib").mkdir(parents=True, exist_ok=True)
        for name, text in (("apt-get", APT_GET), ("dpkg", DPKG)):
            f = self.root / "usr/bin" / name
            f.write_text(text)
            f.chmod(0o755)
        (self.root / "usr/bin/tool").write_text("v1\n")
        (self.root / "etc/config").write_text("original\n")
        (self.root / "var/lib/newer").write_text("")
        (sandbox.home_path("alice", "debian") / "mywork.txt").write_text("keep me\n")

    def tearDown(self):
        helpers.wipe_vms()

    def run_in(self, cmd):
        import subprocess
        sp = sandbox.spec("alice", f"@vm:debian {cmd}", "/root")
        return subprocess.run(sp.argv, cwd=sp.cwd, env=sp.env, capture_output=True, text=True,
                              timeout=30).stdout.strip()

    def finish(self, timeout=60):
        end = time.time() + timeout
        while sandbox.install_state("alice", "debian")["state"] == "updating" and time.time() < end:
            time.sleep(0.1)
        self.assertEqual(sandbox.install_state("alice", "debian")["state"], "ready")
        return vmupdate.read_meta("alice", "debian")

    def test_a_real_update_upgrades_the_system_and_keeps_root(self):
        out = cmd_vm.cmd_vm({"sid": "alice", "cols": 80}, ["update"])
        self.assertIn("Updating the packages of debian", out)
        meta = self.finish()
        self.assertTrue(meta["ok"], meta)
        self.assertEqual(self.run_in("cat /usr/bin/tool"), "v2")
        self.assertIn("changed", self.run_in("cat /etc/config"))
        self.assertEqual(self.run_in("cat /root/mywork.txt"), "keep me")      # /root untouched
        self.assertFalse((sandbox.vm_dir("alice", "debian") / vmupdate.SNAP).exists())
        self.assertIn("Unpacking foo", vmupdate.log_tail("alice", "debian"))   # real output was streamed
        out = cmd_vm.cmd_vm({"sid": "alice", "cols": 80}, ["update"])           # nothing left to do
        self.finish()
        self.assertEqual(vmupdate.read_meta("alice", "debian")["message"], "already up to date")

    def test_a_failing_upgrade_inside_the_vm_restores_the_old_system(self):
        (self.root / "var/lib/fail-upgrade").write_text("")
        cmd_vm.cmd_vm({"sid": "alice", "cols": 80}, ["update"])
        meta = self.finish()
        self.assertFalse(meta["ok"])
        self.assertEqual(meta["phase"], "rolled-back")
        self.assertEqual(self.run_in("cat /usr/bin/tool"), "v1")               # the file the failed run replaced
        self.assertEqual(self.run_in("cat /etc/config"), "original")
        self.assertTrue((self.root / "var/lib/newer").exists())                # the "repo has updates" flag is back
        self.assertEqual(self.run_in("echo still-works"), "still-works")

    def test_a_broken_package_database_after_the_upgrade_rolls_back(self):
        (self.root / "usr/bin/apt-get").write_text(
            APT_GET.replace("echo \"Processing triggers ...\"; exit 0",
                            "touch /var/lib/broken; exit 0"))
        cmd_vm.cmd_vm({"sid": "alice", "cols": 80}, ["update"])
        meta = self.finish()
        self.assertEqual(meta["phase"], "rolled-back")
        self.assertIn("inconsistent", meta["message"])
        self.assertFalse((self.root / "var/lib/broken").exists())
        self.assertEqual(self.run_in("cat /usr/bin/tool"), "v1")

    def test_a_hung_upgrade_is_killed_after_the_timeout_and_rolled_back(self):
        (self.root / "var/lib/slow").write_text("")
        old = vmupdate.TIMEOUTS["upgrade"]
        vmupdate.TIMEOUTS["upgrade"] = 1
        try:
            cmd_vm.cmd_vm({"sid": "alice", "cols": 80}, ["update"])
            meta = self.finish()
        finally:
            vmupdate.TIMEOUTS["upgrade"] = old
        self.assertEqual(meta["phase"], "rolled-back")
        self.assertIn("timed out", vmupdate.log_tail("alice", "debian"))
        self.assertEqual(self.run_in("cat /usr/bin/tool"), "v1")

    def test_the_vm_is_really_locked_while_the_real_upgrade_runs(self):
        (self.root / "var/lib/slow").write_text("")
        cmd_vm.cmd_vm({"sid": "alice", "cols": 80}, ["update"])
        end = time.time() + 20
        refused = None
        while time.time() < end and refused is None:
            if "Unpacking" in vmupdate.log_tail("alice", "debian"):
                with self.assertRaises(sandbox.SandboxError) as cm:
                    sandbox.spec("alice", "echo hi", "/root")
                refused = str(cm.exception)
            time.sleep(0.1)
        self.assertIsNotNone(refused, "never saw the upgrade running")
        self.assertIn("being updated", refused)
        self.finish()
        self.assertEqual(self.run_in("echo back"), "back")                     # usable again

    def make_alpine(self):
        import unittest.mock as mock

        import distros
        ok, msg = sandbox.install("alice", "alpine", wait=True)
        self.assertTrue(ok, msg)
        root = sandbox.rootfs_path("alice", "alpine")
        (root / "etc/apk").mkdir(parents=True, exist_ok=True)
        (root / "var/lib").mkdir(parents=True, exist_ok=True)
        (root / "usr/bin").mkdir(parents=True, exist_ok=True)
        (root / "etc/alpine-release").write_text("3.12.12\n")
        self.repos = ("https://dl-cdn.alpinelinux.org/alpine/v3.12/main\n"
                      "https://dl-cdn.alpinelinux.org/alpine/v3.12/community\n")
        (root / "etc/apk/repositories").write_text(self.repos)
        apk = root / "usr/bin/apk"
        apk.write_text(APK)
        apk.chmod(0o755)
        patch = mock.patch.object(distros, "alpine_latest_version", return_value="3.14.2")
        patch.start()
        self.addCleanup(patch.stop)
        return root

    def test_alpine_really_walks_to_the_latest_release_in_a_real_vm(self):
        root = self.make_alpine()
        out = cmd_vm.cmd_vm({"sid": "alice", "cols": 80}, ["update", "alpine"])
        self.assertIn("from Alpine 3.12.12 to 3.14.2", out)
        self.assertIn("(3.13, 3.14)", out)
        end = time.time() + 60
        while sandbox.install_state("alice", "alpine")["state"] == "updating" and time.time() < end:
            time.sleep(0.1)
        meta = vmupdate.read_meta("alice", "alpine")
        self.assertTrue(meta["ok"], meta)
        self.assertEqual((root / "etc/alpine-release").read_text().strip(), "3.14.1")
        self.assertIn("v3.14/main", (root / "etc/apk/repositories").read_text())
        log = vmupdate.log_tail("alice", "alpine", 60)
        self.assertIn("upgrading to 3.13", log)
        self.assertIn("upgrading to 3.14", log)
        self.assertEqual(sandbox.distro_info("alice", "alpine")["release"], "latest")   # installed as plain `alpine`

    def test_alpine_failing_step_in_a_real_vm_restores_release_and_repositories(self):
        root = self.make_alpine()
        (root / "var/lib/fail-3.14").write_text("")
        cmd_vm.cmd_vm({"sid": "alice", "cols": 80}, ["update", "alpine"])
        end = time.time() + 60
        while sandbox.install_state("alice", "alpine")["state"] == "updating" and time.time() < end:
            time.sleep(0.1)
        meta = vmupdate.read_meta("alice", "alpine")
        self.assertEqual(meta["phase"], "rolled-back", meta)
        self.assertEqual((root / "etc/alpine-release").read_text().strip(), "3.12.12")
        self.assertEqual((root / "etc/apk/repositories").read_text(), self.repos)

    def test_the_users_own_environment_cannot_leak_into_the_update(self):
        state.set_env("alice", "APT_CONFIG", "/nonexistent")
        state.set_env("alice", "DEBIAN_FRONTEND", "dialog")
        cmd_vm.cmd_vm({"sid": "alice", "cols": 80}, ["update"])
        meta = self.finish()
        self.assertTrue(meta["ok"], meta)
        self.assertEqual((self.root / "var/lib/seen-env").read_text().strip(), "unset:noninteractive")


if __name__ == "__main__":
    unittest.main()
