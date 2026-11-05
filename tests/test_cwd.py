"""Directories: host default (/home/rconsole), and staying put after an interactive window closes."""
import os
import sys
import tempfile
import time
import unittest

import helpers  # noqa: F401

import auth
import config
import sandbox
import state
import termsess


class DefaultCwdTests(unittest.TestCase):
    def setUp(self):
        self._home = config.HOST_HOME

    def tearDown(self):
        config.HOST_HOME = self._home

    def test_host_default_is_home_rconsole_when_usable(self):
        with tempfile.TemporaryDirectory() as d:
            config.HOST_HOME = d
            self.assertEqual(state.default_cwd(), d)
            self.assertEqual(sandbox.default_cwd("ark", sandbox.HOST), d)

    def test_falls_back_to_the_workspace_when_missing(self):
        config.HOST_HOME = "/nonexistent/rconsole-home"
        self.assertEqual(state.default_cwd(), str(config.WORKSPACE_DIR))

    def test_configured_default_is_home_rconsole(self):
        if "RCONSOLE_HOST_HOME" not in os.environ:
            self.assertEqual(self._home, "/home/rconsole")

    def test_vm_default_is_home_but_home_dir_stays_root(self):
        self.assertEqual(sandbox.VM_HOME, "/root")
        self.assertEqual(sandbox.VM_START, "/home")
        self.assertEqual(sandbox.default_cwd("ark", sandbox.VM), "/home")


class EndTerminalTests(unittest.TestCase):
    def test_ending_a_terminal_keeps_the_tab_and_its_directory(self):
        user = "cwduser"
        sess = state.new_session(user)
        with tempfile.TemporaryDirectory() as d:
            tab = state.ensure_tab(sess, user, "t1")
            tab["cwd"] = d
            tab["history"].append("ls")
            state.set_terminal(sess, user, "t1", "nano x", d)
            self.assertTrue(state.end_terminal(user, "t1"))
            tab = sess["tabs"]["t1"]
            self.assertEqual(tab["cwd"], d)
            self.assertFalse(tab["isTerminal"])
            self.assertEqual(tab["cmd"], "")
            self.assertEqual(tab["history"], ["ls"])
            # the shell ended somewhere else: that is where the tab carries on
            self.assertTrue(state.end_terminal(user, "t1", "/tmp"))
            self.assertEqual(sess["tabs"]["t1"]["cwd"], "/tmp")
            self.assertFalse(state.end_terminal(user, "gone"))


@unittest.skipIf(sys.platform == "win32", "needs a POSIX pty")
class HostShellCwdTests(unittest.TestCase):
    def setUp(self):
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()

    def test_shell_cwd_is_carried_back_when_it_exits(self):
        user = "ark"
        sess = state.new_session(user)
        with tempfile.TemporaryDirectory() as start, tempfile.TemporaryDirectory() as dest:
            dest = os.path.realpath(dest)
            tab = state.ensure_tab(sess, user, "tw")
            tab["cwd"] = start
            state.set_terminal(sess, user, "tw", "", start)
            s = termsess.TermSession(user, "tw", "", start)
            with termsess.SESSIONS_LOCK:
                termsess.SESSIONS[(user, "tw")] = s
            try:
                time.sleep(0.6)
                s.write("cd %s\n" % dest)
                deadline = time.time() + 5
                while time.time() < deadline and s.last_cwd != dest:
                    time.sleep(0.1)
                self.assertEqual(s.last_cwd, dest)
                s.write("exit\n")
                deadline = time.time() + 5
                while time.time() < deadline and not s.exited:
                    time.sleep(0.1)
                time.sleep(0.3)
                self.assertTrue(s.exited)
                tab = sess["tabs"]["tw"]
                self.assertEqual(tab["cwd"], dest)
                self.assertFalse(tab["isTerminal"])
            finally:
                s.kill()
                state.remove_tab(user, "tw")


if __name__ == "__main__":
    unittest.main()
