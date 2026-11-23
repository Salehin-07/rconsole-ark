"""1.5.9: a safe /proc for VMs without a PID namespace, the non-dumpable server, the bash host shell."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (isolates the data dir; must come before rconsole modules)
import procguard
import procview
import sandbox
import shellutil


class ProcViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.view = procview.view_dir(self.tmp)

    def test_binds_put_the_view_at_proc_and_the_system_files_on_top(self):
        with mock.patch.object(procview, "vm_pids", return_value=[]):
            b = procview.binds(self.tmp, "/nonexistent-root")
        self.assertEqual(b[0], (str(self.view), "/proc"))
        guests = [g for _, g in b[1:]]
        for name in ("uptime", "meminfo", "stat", "vmstat", "diskstats", "loadavg"):
            self.assertIn(f"/proc/{name}", guests)
        for _, g in b:                       # nothing that leads to other processes is ever mounted
            self.assertNotRegex(g, r"/proc/(self|thread-self|\d+|net|mounts|kcore|kmsg|sysrq-trigger)")

    def test_mounts_file_describes_the_vm(self):
        with mock.patch.object(procview, "vm_pids", return_value=[]):
            procview.refresh(self.view, "/r")
        text = (self.view / "mounts").read_text()
        self.assertIn("rootfs / ", text)
        self.assertNotIn(str(self.tmp), text)

    def test_the_process_view_is_plain_copies_of_this_vms_processes(self):
        with mock.patch.object(procview, "vm_pids", return_value=[os.getpid()]):
            procview.refresh(self.view, "/r")
        d = self.view / str(os.getpid())
        for f in ("stat", "status", "cmdline"):
            self.assertTrue((d / f).is_file() and not (d / f).is_symlink(), f)
        self.assertIn(b"python", (d / "cmdline").read_bytes().lower() + (d / "status").read_bytes().lower())
        for forbidden in ("environ", "fd", "root", "cwd", "exe", "mem"):
            self.assertFalse((d / forbidden).exists(), forbidden)

    def test_vanished_processes_and_planted_junk_are_removed(self):
        self.view.mkdir(parents=True)
        (self.view / "424242").mkdir()
        (self.view / "evil").symlink_to("/etc")
        (self.view / "junkdir").mkdir()
        with mock.patch.object(procview, "vm_pids", return_value=[]):
            procview.refresh(self.view, "/r")
        self.assertEqual(sorted(p.name for p in self.view.iterdir()), ["mounts"])

    def test_a_planted_symlink_cannot_make_the_server_write_outside(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("untouched")
        pid = os.getpid()
        with mock.patch.object(procview, "vm_pids", return_value=[pid]):
            procview.refresh(self.view, "/r")
            for f in ("stat", "status", "cmdline", "comm", "statm"):       # the VM replaces every file by a link
                (self.view / str(pid) / f).unlink(missing_ok=True)
                (self.view / str(pid) / f).symlink_to(outside)
            shutil.rmtree(self.view / "mounts", ignore_errors=True)
            (self.view / "mounts").unlink(missing_ok=True)
            (self.view / "mounts").symlink_to(outside)
            procview.refresh(self.view, "/r")
        self.assertEqual(outside.read_text(), "untouched")
        self.assertFalse((self.view / str(pid) / "stat").is_symlink())
        self.assertFalse((self.view / "mounts").is_symlink())

    def test_a_planted_directory_link_cannot_redirect_a_process_folder(self):
        victim = self.tmp / "victim"
        victim.mkdir()
        pid = os.getpid()
        self.view.mkdir(parents=True)
        (self.view / str(pid)).symlink_to(victim)                       # the VM points the pid folder elsewhere
        with mock.patch.object(procview, "vm_pids", return_value=[pid]):
            procview.refresh(self.view, "/r")
        self.assertEqual(list(victim.iterdir()), [])
        self.assertFalse((self.view / str(pid)).is_symlink())

    def test_a_directory_where_a_file_belongs_is_cleared(self):
        pid = os.getpid()
        with mock.patch.object(procview, "vm_pids", return_value=[pid]):
            procview.refresh(self.view, "/r")
            (self.view / str(pid) / "stat").unlink()
            (self.view / str(pid) / "stat").mkdir()
            (self.view / str(pid) / "stat" / "x").write_text("x")
            procview.refresh(self.view, "/r")
        self.assertTrue((self.view / str(pid) / "stat").is_file())

    def test_vm_pids_finds_only_the_tracees_of_this_vms_proot(self):
        self.assertEqual(procview.vm_pids("/no/such/rootfs-" + os.urandom(4).hex()), [])

    def test_forget_removes_the_view(self):
        with mock.patch.object(procview, "vm_pids", return_value=[]):
            procview.refresh(self.view, "/r")
        procview.forget(self.tmp)
        self.assertFalse(self.view.exists())


class PsShimTests(unittest.TestCase):
    def test_the_shim_lists_processes_from_a_proc_view_without_self(self):
        d = Path(tempfile.mkdtemp(prefix="ps-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "12").mkdir()
        (d / "12" / "stat").write_text("12 (sleep) S 1 12 12 0 -1 4194560 0 0 0 0 250 50 0 0 20 0 1 0 1 1 1 1")
        (d / "12" / "cmdline").write_bytes(b"sleep\x0099\x00")
        script = sandbox.PS_SHIM_TEXT.replace("/proc/", str(d) + "/").replace("[ -e %s/self ]" % d, "false")
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True).stdout
        self.assertIn("PID", out)
        self.assertRegex(out, r"12\s+\S+\s+0:03\s+sleep 99")

    def test_the_shim_is_installed_in_every_vm(self):
        self.assertIn('"ps"', __import__("inspect").getsource(sandbox.sync_guest_tools))


class ProotPrefixTests(unittest.TestCase):
    def prefix(self, admin, own_pidns=False, user_proc=False):
        env = {"RCONSOLE_USER_VM_PROC": "1"} if user_proc else {"RCONSOLE_USER_VM_PROC": "0"}
        with mock.patch.object(sandbox, "find_proot", return_value="proot"), \
                mock.patch.object(procview, "vm_pids", return_value=[]), \
                mock.patch.dict(os.environ, env):
            return sandbox._proot_prefix("pvuser", "debian", "/root", admin, own_pidns=own_pidns)

    @staticmethod
    def binds(argv):
        return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-b"]

    def test_without_a_pid_namespace_nobody_gets_the_real_proc(self):
        for admin in (False, True):
            b = self.binds(self.prefix(admin))
            self.assertNotIn("/proc", b)
            self.assertTrue(any(x.endswith("procview:/proc") for x in b), b)
            self.assertIn("/proc/uptime:/proc/uptime", b)

    def test_with_a_pid_namespace_the_real_proc_is_safe_and_used(self):
        b = self.binds(self.prefix(False, own_pidns=True))
        self.assertIn("/proc", b)
        self.assertFalse(any(x.endswith("procview:/proc") for x in b))

    def test_the_explicit_opt_in_still_gives_the_real_proc(self):
        self.assertIn("/proc", self.binds(self.prefix(False, user_proc=True)))


class ProcGuardTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_the_server_marks_itself_non_dumpable(self):
        code = ("import procguard as p; a = p.dumpable(); ok = p.harden(); "
                "print(a, ok, p.dumpable(), len(__import__('os').listdir('/proc/self/fd')) > 0)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=str(Path(procguard.__file__).parent)).stdout.split()
        self.assertEqual(out, ["True", "True", "False", "True"])      # and it can still read its own /proc/self

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_a_launched_program_is_dumpable_again(self):
        code = ("import procguard as p, subprocess, sys; p.harden(); "
                "print(subprocess.run([sys.executable, '-c', 'import procguard as q; print(q.dumpable())'],"
                " capture_output=True, text=True).stdout.strip())")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=str(Path(procguard.__file__).parent)).stdout.strip()
        self.assertEqual(out, "True")


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "needs bash and a PTY")
class HostShellTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="hs-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_interactive_host_is_bash_with_a_managed_rcfile(self):
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            argv, extra = shellutil.interactive_host(self.home)
        self.assertTrue(argv[0].endswith("bash"), argv)
        self.assertIn("--rcfile", argv)
        rc = Path(argv[argv.index("--rcfile") + 1]).read_text()
        self.assertIn("rconsole@host", rc)
        self.assertIn("\\w", rc)                                   # the path is in the prompt
        self.assertIn("history -a", rc)                            # history is saved as you go
        self.assertEqual(extra["HISTFILE"], os.path.join(self.home, ".bash_history"))

    def test_the_host_tty_launch_uses_it_and_a_command_does_not(self):
        with mock.patch.object(sandbox, "is_admin", return_value=True):
            sp = sandbox.spec("ark", "@host", os.getcwd(), tty=True)
            plain = sandbox.spec("ark", "@host echo hi", os.getcwd(), tty=True)
        self.assertEqual(sp.mode, sandbox.HOST)
        self.assertTrue(sp.argv[0].endswith("bash") and "-i" in sp.argv, sp.argv)
        self.assertIn("HISTFILE", sp.env)
        self.assertEqual(plain.argv[1:], ["-c", "echo hi"])

    def test_the_fallback_shell_still_shows_the_path(self):
        with mock.patch.object(shellutil.shutil, "which", return_value=None), \
                mock.patch.object(shellutil, "detect_shell", return_value=("/bin/sh", "-c")):
            argv, extra = shellutil.interactive_host(self.home)
        self.assertEqual(argv, ["/bin/sh", "-i"])
        self.assertIn("$PWD", Path(extra["ENV"]).read_text())

    def test_up_arrow_recalls_the_previous_command_and_the_prompt_shows_the_path(self):
        import pty
        import select
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            argv, extra = shellutil.interactive_host(self.home)
        env = dict(os.environ, TERM="xterm-256color", HOME=self.home, **extra)
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.home)
            os.execvpe(argv[0], argv, env)

        def read(wait=1.0):
            out, end = b"", time.time() + wait
            while time.time() < end:
                if select.select([fd], [], [], 0.1)[0]:
                    try:
                        out += os.read(fd, 4096)
                    except OSError:
                        break
            return out.decode(errors="replace")

        try:
            read(1.5)
            os.write(fd, b"echo ran-$((20+22))\r")
            first = read()
            self.assertIn("ran-42", first)
            self.assertIn("rconsole@host", first)
            os.write(fd, b"cd /tmp\r")
            self.assertIn("/tmp", read())                          # the prompt now shows the new path
            os.write(fd, b"\x1b[A\x1b[A\r")                        # up, up, enter -> the echo again
            self.assertIn("ran-42", read(1.5))
        finally:
            try:
                os.write(fd, b"exit\r")
                read(0.3)
                os.waitpid(pid, 0)
            except OSError:
                pass
        self.assertIn("echo ran-", Path(self.home, ".bash_history").read_text())


if __name__ == "__main__":
    unittest.main()
