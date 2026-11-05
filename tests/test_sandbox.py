import os
import unittest
from pathlib import Path

import helpers  # noqa: F401

import auth
import config
import sandbox


class ResolveInRootTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="rootfs-"))
        self.root = self.tmp / "root"
        (self.root / "etc").mkdir(parents=True)
        (self.root / "usr/lib").mkdir(parents=True)
        (self.root / "etc/passwd").write_text("root:x:0:0")
        (self.tmp / "secret.txt").write_text("host secret")
        os.symlink("/etc/passwd", self.root / "link_abs")          # inside the VM
        os.symlink("../../../secret.txt", self.root / "etc/escape")  # tries to leave
        os.symlink("/", self.root / "rootlink")
        os.symlink("loop2", self.root / "loop1")
        os.symlink("loop1", self.root / "loop2")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def res(self, p, **kw):
        return sandbox.resolve_in_root(self.root, p, **kw)

    def test_plain_and_relative(self):
        host, virt = self.res("/etc/passwd")
        self.assertEqual(host, self.root.resolve() / "etc/passwd")
        self.assertEqual(virt, "/etc/passwd")
        self.assertEqual(self.res("/etc/../usr/./lib")[1], "/usr/lib")

    def test_dotdot_cannot_climb_above_root(self):
        self.assertEqual(self.res("/../../../etc/passwd")[1], "/etc/passwd")
        host, _ = self.res("/etc/../../..")
        self.assertEqual(host, self.root.resolve())

    def test_absolute_symlink_stays_inside(self):
        host, virt = self.res("/link_abs")
        self.assertEqual(host, self.root.resolve() / "etc/passwd")  # NOT host /etc/passwd
        self.assertEqual(self.res("/rootlink/etc/passwd")[1], "/etc/passwd")

    def test_relative_symlink_escape_is_clamped(self):
        host, _ = self.res("/etc/escape")
        self.assertTrue(str(host).startswith(str(self.root.resolve())))
        self.assertFalse(host.exists())   # the host's secret.txt is unreachable

    def test_symlink_loop_raises(self):
        with self.assertRaises(OSError):
            self.res("/loop1")

    def test_nofollow_last(self):
        host, _ = self.res("/link_abs", follow_last=False)
        self.assertEqual(host, self.root.resolve() / "link_abs")

    def test_fsview(self):
        view = sandbox.FsView(self.root)
        p = view.host("etc/passwd", "/")
        self.assertEqual(view.virt(p), "/etc/passwd")
        self.assertEqual(view.host("../../x", "/etc"), self.root.resolve() / "x")
        self.assertEqual(view.child_virt("/etc", "a"), "/etc/a")


class TargetTests(unittest.TestCase):
    def setUp(self):
        helpers.wipe_vms()
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()
        auth.create_user("alice", "longenough1")

    def test_parse_target(self):
        self.assertEqual(sandbox.parse_target("@vm"), ("vm", ""))
        self.assertEqual(sandbox.parse_target("@vm ls -la"), ("vm", "ls -la"))
        self.assertEqual(sandbox.parse_target("@host id"), ("host", "id"))
        self.assertEqual(sandbox.parse_target("@vm:debian ls -la"), ("vm:debian", "ls -la"))
        self.assertEqual(sandbox.parse_target("@vm:kali"), ("vm:kali", ""))
        self.assertEqual(sandbox.parse_target("@vm:Bad/Name x"), (None, "@vm:Bad/Name x"))
        self.assertEqual(sandbox.parse_target("@vmx"), (None, "@vmx"))
        self.assertEqual(sandbox.parse_target("echo @vm"), (None, "echo @vm"))

    def test_non_admin_never_reaches_host(self):
        self.assertEqual(sandbox.effective_mode("alice"), sandbox.VM)
        with self.assertRaises(sandbox.PermissionDenied):
            sandbox.resolve_target("alice", sandbox.HOST)
        with self.assertRaises(sandbox.PermissionDenied):
            sandbox.spec("alice", "@host cat /etc/passwd", "/")
        # With no VM installed the default path asks for one — never the host.
        with self.assertRaises(sandbox.NeedVmChoice) as cm:
            sandbox.spec("alice", "cat /etc/passwd", "/")
        self.assertTrue(cm.exception.empty)

    def test_non_admin_cannot_pick_host_mode(self):
        ok, msg = sandbox.set_mode("alice", "host")
        self.assertFalse(ok)
        self.assertIn("admins", msg)

    def test_admin_defaults_to_host(self):
        self.assertEqual(sandbox.effective_mode("ark"), sandbox.HOST)
        sp = sandbox.spec("ark", "echo hi", str(config.HERE))
        self.assertEqual(sp.mode, sandbox.HOST)
        self.assertEqual(sp.argv[-1], "echo hi")

    def test_server_secrets_scrubbed_from_env(self):
        os.environ["RCONSOLE_SECRET"] = "s3cret"
        try:
            sp = sandbox.spec("ark", "env", str(config.HERE))
            self.assertNotIn("RCONSOLE_SECRET", sp.env)
        finally:
            os.environ.pop("RCONSOLE_SECRET")

    def test_proot_flag_detection_for_old_builds(self):
        """Regression: Ubuntu's proot 5.1.0 rejects --kill-on-exit."""
        import stat
        import tempfile
        d = Path(tempfile.mkdtemp())
        old, new = d / "proot-old", d / "proot-new"
        old.write_text("#!/bin/sh\necho 'usage: proot [-r path] [-b path] [-0]'\n")
        new.write_text("#!/bin/sh\necho 'usage: proot ... --kill-on-exit'\n")
        for p in (old, new):
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.assertFalse(sandbox.proot_supports(str(old), "--kill-on-exit"))
        self.assertTrue(sandbox.proot_supports(str(new), "--kill-on-exit"))
        self.assertFalse(sandbox.proot_supports(str(d / "missing"), "--kill-on-exit"))

    def test_alpine_index_parser(self):
        text = """---
-
  title: "Mini root filesystem"
  file: alpine-minirootfs-3.22.2-x86_64.tar.gz
  sha256: aaa111
  flavor: alpine-minirootfs
-
  title: "Standard"
  file: alpine-standard-3.22.2-x86_64.iso
  sha256: bbb222
  flavor: alpine-standard
"""
        self.assertEqual(sandbox.parse_alpine_release(text),
                         ("alpine-minirootfs-3.22.2-x86_64.tar.gz", "aaa111"))
        with self.assertRaises(sandbox.SandboxError):
            sandbox.parse_alpine_release("nothing here")


if __name__ == "__main__":
    unittest.main()


class KaliLoginNoiseTests(unittest.TestCase):
    """1.3.10: Kali's login scripts must not spam errors; /usr/games must be on PATH."""

    def test_proc_whitelist_has_the_random_files(self):
        self.assertIn("sys/kernel/random/uuid", sandbox.SAFE_PROC_FILES)
        self.assertIn("sys/kernel/random/boot_id", sandbox.SAFE_PROC_FILES)

    def test_games_dirs_are_on_the_path(self):
        self.assertIn("/usr/games", sandbox.SYSTEM_PATH.split(":"))
        self.assertIn("/usr/local/games", sandbox.SYSTEM_PATH.split(":"))
        self.assertIn("/usr/games", sandbox.PROMPT_FILE_TEXT)
        self.assertIn("/usr/games", sandbox.ENV_FILE_TEXT)

    def test_ln_shim_skips_existing_dev_links_only(self):
        import subprocess, tempfile, os
        with tempfile.TemporaryDirectory() as d:
            shim = os.path.join(d, "ln")
            with open(shim, "w") as f:
                f.write(sandbox.LN_SHIM_TEXT)
            os.chmod(shim, 0o755)
            # /dev/fd exists on the test host: silently succeed
            r = subprocess.run([shim, "-s", "/proc/self/fd", "/dev/fd"], capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stderr), (0, ""))
            # anything else goes to the real ln
            target = os.path.join(d, "a"); open(target, "w").close()
            r = subprocess.run([shim, "-s", target, os.path.join(d, "b")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)
            self.assertTrue(os.path.islink(os.path.join(d, "b")))


class JournalctlShimTests(unittest.TestCase):
    """1.3.11: systemd's postinst must not print /proc and catalog errors in a VM."""

    def test_update_catalog_is_a_silent_noop(self):
        import subprocess, tempfile, os
        with tempfile.TemporaryDirectory() as d:
            shim = os.path.join(d, "journalctl")
            with open(shim, "w") as f:
                f.write(sandbox.JOURNALCTL_SHIM_TEXT)
            os.chmod(shim, 0o755)
            r = subprocess.run([shim, "--update-catalog"], capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
