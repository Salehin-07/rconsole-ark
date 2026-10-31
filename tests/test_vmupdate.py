"""`vm update`: snapshot/rollback, locking, who may update, what stops, crash recovery.

No proot is needed: the package manager inside the VM is replaced by a fake that edits the
VM's files directly, so the real snapshot, lock, rollback and recovery code all run.
"""
import json
import os
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401

import auth
import cmd_vm
import config
import jobs
import sandbox
import services
import state
import termsess
import vmupdate


def replace_file(path, text):
    """Change a file the way dpkg and apk do: write a new one, then rename it over the old."""
    path = Path(path)
    tmp = path.with_name(path.name + ".pkgnew")
    tmp.write_text(text)
    os.replace(tmp, path)


def make_vm(sid, vm="debian", family="debian"):
    base = sandbox.vm_dir(sid, vm)
    root = base / "rootfs"
    for d in ("etc", "usr/bin", "var/lib/dpkg", "var/lib/apk", "root", "tmp", "bin"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "usr/bin/tool").write_text("v1")
    (root / "etc/config").write_text("original\n")
    (root / "var/lib/dpkg/status").write_text("pkg v1\n")
    os.symlink("tool", root / "usr/bin/tool-link")
    (base / "home").mkdir(exist_ok=True)
    (base / sandbox.DISTRO_FILE).write_text(json.dumps(
        {"id": vm, "release": "x", "family": family, "label": vm,
         "installed_at": int(time.time()) + len(list(sandbox.user_dir(sid).glob("*")))}))
    (base / sandbox.READY_MARKER).write_text("1")
    return root


class FakeApt:
    """Stands in for apt/apk inside the VM by editing the VM's files directly."""

    def __init__(self, root, family="debian"):
        self.root, self.family = Path(root), family
        self.fail = set()           # step names that fail
        self.pending = 2            # packages the "repo" has newer
        self.calls = []
        self.during_upgrade = None

    def __call__(self, sid, vm, script, timeout, on_line):
        cmds = vmupdate.commands_for(self.family, vm)
        if script == cmds["health"]:                       # health and verify run the same check
            name = "verify" if "upgraded" in self.calls else "health"
        else:
            name = next(k for k, v in cmds.items() if v == script)
        self.calls.append(name)
        on_line(f"fake {name}")
        if name in self.fail:
            return 1, f"{name} exploded"
        if name == "count":
            if self.family == "alpine":
                return 0, "\n".join(f"({i + 1}/{self.pending}) Upgrading pkg{i} (1 -> 2)"
                                    for i in range(self.pending))
            return 0, "\n".join(f"Inst pkg{i} [1] (2 repo)" for i in range(self.pending))
        if name == "upgrade":
            if self.during_upgrade:
                self.during_upgrade()
            tmp = self.root / "usr/bin/tool.new"
            tmp.write_text("v2")
            os.replace(tmp, self.root / "usr/bin/tool")         # how dpkg replaces files
            with open(self.root / "etc/config", "a") as f:      # edited in place
                f.write("changed by upgrade\n")
            (self.root / "var/lib/dpkg/status").write_text("pkg v2\n")
            self.calls.append("upgraded")
            return 0, ""
        return 0, ""


class Base(unittest.TestCase):
    def setUp(self):
        helpers.wipe_vms()
        vmupdate.reset()
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()
        auth.set_password("ark", "adminpass123")
        auth.create_user("alice", "longenough1")
        services.FILE.unlink(missing_ok=True)
        services._cache.update(sig=None, data={})
        services._rt.clear()
        for u in ("alice", "ark"):
            state.STATE.pop(u, None)
            state.new_session(u)
            state.set_vm(u, None)
            state.set_mode(u, None)
        self.patches = [
            mock.patch.object(sandbox, "find_proot", return_value="/bin/true"),
            mock.patch.object(sandbox, "_ensure_pinned_proot"),
            mock.patch.object(sandbox, "_probe", return_value=None),
            mock.patch.object(sandbox, "sync_guest_tools"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        vmupdate._abort.clear()
        for u in ("alice", "ark"):
            jobs.kill_all_jobs(u)
        services.clear()
        helpers.wipe_vms()

    def update(self, sid, vm, fake, **kw):
        """Run an update to completion with ``fake`` as the package manager."""
        with mock.patch.object(vmupdate, "_exec", fake):
            ok, msg = vmupdate.start(sid, vm, **kw)
            if ok:
                self.wait(sid, vm)
        return ok, msg

    def wait(self, sid, vm, timeout=15):
        end = time.time() + timeout
        while sandbox.install_state(sid, vm)["state"] == "updating" and time.time() < end:
            time.sleep(0.02)
        self.assertNotEqual(sandbox.install_state(sid, vm)["state"], "updating", "update hung")

    def run_vm(self, sid, args):
        return cmd_vm.cmd_vm({"sid": sid, "cols": 80}, args)


class PureTests(unittest.TestCase):
    def test_counts_what_the_simulated_upgrade_would_change(self):
        apt = "Reading...\nInst libc6 [2.40] (2.41 stable)\nConf libc6 (2.41)\nInst curl [8] (9 stable)\n"
        self.assertEqual(vmupdate.parse_count("debian", apt), 2)
        apk = "(1/2) Upgrading busybox (1.36-r0 -> 1.36-r1)\n(2/2) Upgrading musl (1 -> 2)\nOK: 8 MiB"
        self.assertEqual(vmupdate.parse_count("alpine", apk), 2)
        self.assertEqual(vmupdate.parse_count("debian", "0 upgraded, 0 newly installed"), 0)

    def test_commands_never_ask_questions_and_keep_config_files(self):
        c = vmupdate.commands_for("debian", "debian")
        self.assertIn("--force-confold", c["upgrade"])
        self.assertIn("apt-get -y", c["upgrade"])
        self.assertTrue(c["upgrade"].endswith(" upgrade"))             # never removes packages
        self.assertTrue(vmupdate.commands_for("debian", "kali")["upgrade"].endswith("full-upgrade"))
        self.assertEqual(vmupdate.commands_for("alpine")["refresh"], "apk update")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="vmupd-"))
        self.root = self.tmp / "rootfs"
        for d in ("etc", "usr/bin", "var/lib/dpkg", "usr/share"):
            (self.root / d).mkdir(parents=True)
        (self.root / "usr/bin/tool").write_text("v1")
        (self.root / "etc/config").write_text("orig\n")
        (self.root / "var/lib/dpkg/status").write_text("s1\n")
        os.symlink("/etc/config", self.root / "usr/share/abs-link")
        os.chmod(self.root / "usr/bin/tool", 0o755)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_big_files_are_links_and_settings_are_real_copies(self):
        snap = self.tmp / "snap"
        stats = vmupdate.make_snapshot(self.root, snap)
        self.assertEqual(os.stat(snap / "usr/bin/tool").st_ino, os.stat(self.root / "usr/bin/tool").st_ino)
        self.assertNotEqual(os.stat(snap / "etc/config").st_ino, os.stat(self.root / "etc/config").st_ino)
        self.assertNotEqual(os.stat(snap / "var/lib/dpkg/status").st_ino,
                            os.stat(self.root / "var/lib/dpkg/status").st_ino)
        self.assertEqual(os.readlink(snap / "usr/share/abs-link"), "/etc/config")
        self.assertEqual(oct(os.stat(snap / "usr/bin/tool").st_mode & 0o777), "0o755")
        self.assertEqual((stats["linked"], stats["copied"]), (1, 2))

    def test_the_snapshot_survives_both_ways_packages_change_files(self):
        snap = self.tmp / "snap"
        vmupdate.make_snapshot(self.root, snap)
        (self.root / "usr/bin/tool.new").write_text("v2")                   # replace (dpkg/apk way)
        os.replace(self.root / "usr/bin/tool.new", self.root / "usr/bin/tool")
        with open(self.root / "etc/config", "a") as f:                      # in place
            f.write("more\n")
        self.assertEqual((snap / "usr/bin/tool").read_text(), "v1")
        self.assertEqual((snap / "etc/config").read_text(), "orig\n")

    def test_rollback_puts_the_old_system_back(self):
        base = self.tmp
        snap = base / vmupdate.SNAP
        vmupdate.make_snapshot(self.root, snap)
        replace_file(self.root / "usr/bin/tool", "broken")
        ok, msg = vmupdate.rollback(base)
        self.assertTrue(ok, msg)
        self.assertEqual((self.root / "usr/bin/tool").read_text(), "v1")
        self.assertFalse(snap.exists())
        self.assertFalse((base / vmupdate.FAILED).exists())

    def test_rollback_without_a_snapshot_changes_nothing(self):
        ok, _msg = vmupdate.rollback(self.tmp)
        self.assertFalse(ok)
        self.assertEqual((self.root / "usr/bin/tool").read_text(), "v1")

    def test_falls_back_to_copying_when_links_are_not_allowed(self):
        snap = self.tmp / "snap"
        with mock.patch.object(vmupdate.os, "link", side_effect=OSError("EXDEV")):
            stats = vmupdate.make_snapshot(self.root, snap)
        self.assertEqual(stats["linked"], 0)
        self.assertEqual((snap / "usr/bin/tool").read_text(), "v1")
        self.assertNotEqual(os.stat(snap / "usr/bin/tool").st_ino, os.stat(self.root / "usr/bin/tool").st_ino)


class UpdateFlowTests(Base):
    def test_a_good_update_upgrades_unlocks_and_drops_the_snapshot(self):
        root = make_vm("alice")
        fake = FakeApt(root)
        ok, msg = self.update("alice", "debian", fake)
        self.assertTrue(ok, msg)
        self.assertEqual((root / "usr/bin/tool").read_text(), "v2")
        self.assertEqual(fake.calls, ["health", "refresh", "count", "upgrade", "upgraded", "verify"])
        self.assertFalse((sandbox.vm_dir("alice", "debian") / vmupdate.SNAP).exists())
        meta = vmupdate.read_meta("alice", "debian")
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["phase"], "done")
        self.assertIn("2 package(s) upgraded", meta["message"])
        self.assertEqual(sandbox.install_state("alice", "debian")["state"], "ready")
        info = sandbox.distro_info("alice", "debian")
        self.assertEqual(info["packages_updated"], 2)
        self.assertIn("updated_at", info)
        self.assertEqual(sandbox.resolve("alice").vm, "debian")            # usable again

    def test_nothing_new_means_nothing_is_touched(self):
        root = make_vm("alice")
        fake = FakeApt(root)
        fake.pending = 0
        self.update("alice", "debian", fake)
        self.assertNotIn("upgrade", fake.calls)
        self.assertEqual((root / "usr/bin/tool").read_text(), "v1")
        self.assertEqual(vmupdate.read_meta("alice", "debian")["message"], "already up to date")

    def test_a_failed_upgrade_rolls_everything_back(self):
        root = make_vm("alice")
        fake = FakeApt(root)
        fake.fail = {"verify"}          # fails AFTER the files were changed
        self.update("alice", "debian", fake)
        self.assertEqual((root / "usr/bin/tool").read_text(), "v1")
        self.assertEqual((root / "etc/config").read_text(), "original\n")
        self.assertEqual((root / "var/lib/dpkg/status").read_text(), "pkg v1\n")
        meta = vmupdate.read_meta("alice", "debian")
        self.assertFalse(meta["ok"])
        self.assertEqual(meta["phase"], "rolled-back")
        self.assertIn("previous system was restored", meta["message"])
        self.assertEqual(sandbox.install_state("alice", "debian")["state"], "ready")
        self.assertFalse((sandbox.vm_dir("alice", "debian") / vmupdate.SNAP).exists())

    def test_the_upgrade_command_failing_midway_rolls_back(self):
        root = make_vm("alice")
        fake = FakeApt(root)

        def boom():
            replace_file(root / "usr/bin/tool", "half-written")
            raise RuntimeError("disk full")
        fake.during_upgrade = boom
        self.update("alice", "debian", fake)
        self.assertEqual((root / "usr/bin/tool").read_text(), "v1")
        self.assertIn("RuntimeError", vmupdate.read_meta("alice", "debian")["message"])

    def test_a_system_that_cannot_start_programs_is_rolled_back(self):
        root = make_vm("alice")
        fake = FakeApt(root)
        with mock.patch.object(sandbox, "_probe", return_value="cannot execute /bin/sh"):
            self.update("alice", "debian", fake)
        self.assertEqual((root / "usr/bin/tool").read_text(), "v1")
        self.assertIn("no longer starts programs", vmupdate.read_meta("alice", "debian")["message"])

    def test_no_network_changes_nothing_and_takes_no_snapshot(self):
        root = make_vm("alice")
        fake = FakeApt(root)
        fake.fail.add("refresh")
        self.update("alice", "debian", fake)
        self.assertNotIn("upgrade", fake.calls)
        meta = vmupdate.read_meta("alice", "debian")
        self.assertEqual(meta["phase"], "refused")
        self.assertIn("network", meta["message"])
        self.assertEqual((root / "usr/bin/tool").read_text(), "v1")

    def test_a_system_that_is_already_broken_is_not_touched(self):
        root = make_vm("alice")
        fake = FakeApt(root)
        fake.fail.add("health")
        self.update("alice", "debian", fake)
        self.assertEqual(fake.calls, ["health"])
        self.assertIn("already has problems", vmupdate.read_meta("alice", "debian")["message"])

    def test_alpine_goes_through_the_same_steps(self):
        root = make_vm("alice", "alpine", "alpine")
        fake = FakeApt(root, "alpine")
        self.update("alice", "alpine", fake)
        self.assertEqual((root / "usr/bin/tool").read_text(), "v2")

    def test_not_enough_disk_for_a_snapshot_refuses_before_touching_anything(self):
        root = make_vm("alice")
        fake = FakeApt(root)
        with mock.patch.object(vmupdate, "snapshot_needed_bytes", return_value=10 ** 15):
            self.update("alice", "debian", fake)
        self.assertNotIn("upgrade", fake.calls)
        self.assertIn("not enough free disk", vmupdate.read_meta("alice", "debian")["message"])

    def test_a_snapshot_that_cannot_be_made_refuses_without_changing_the_system(self):
        root = make_vm("alice")
        fake = FakeApt(root)
        with mock.patch.object(vmupdate, "make_snapshot", side_effect=OSError("no space left")):
            self.update("alice", "debian", fake)
        self.assertNotIn("upgrade", fake.calls)
        self.assertFalse((sandbox.vm_dir("alice", "debian") / vmupdate.SNAP).exists())
        self.assertIn("could not save a snapshot", vmupdate.read_meta("alice", "debian")["message"])

    def test_log_and_summary_for_status(self):
        root = make_vm("alice")
        self.update("alice", "debian", FakeApt(root))
        self.assertIn("=== upgrading 2 package(s) ===", vmupdate.log_tail("alice", "debian"))
        self.assertIn("2 package(s) upgraded", vmupdate.summary("alice", "debian"))
        out = self.run_vm("alice", ["status"])
        self.assertIn("last update", out)


class LockTests(Base):
    def test_the_vm_is_inaccessible_while_it_updates_others_are_not(self):
        root_d = make_vm("alice", "debian")
        make_vm("alice", "alpine", "alpine")
        seen = {}

        def during():
            for what, call in (("resolve", lambda: sandbox.resolve("alice", "vm:debian")),
                               ("spec", lambda: sandbox.spec("alice", "@vm:debian ls", "/root")),
                               ("fs", lambda: sandbox.fs("alice", "vm:debian"))):
                try:
                    call()
                    seen[what] = "allowed"
                except sandbox.SandboxError as e:
                    seen[what] = str(e)
            seen["other"] = sandbox.resolve("alice", "vm:alpine").vm
            seen["status"] = self.run_vm("alice", ["status"])
        fake = FakeApt(root_d)
        fake.during_upgrade = during
        self.update("alice", "debian", fake)
        for what in ("resolve", "spec", "fs"):
            self.assertIn("being updated", seen[what])
        self.assertEqual(seen["other"], "alpine")
        self.assertIn("updating", seen["status"])

    def test_nothing_can_reset_remove_or_install_over_an_updating_vm(self):
        root = make_vm("alice")
        results = {}

        def during():
            results["remove"] = self.run_vm("alice", ["remove", "debian", "--yes"])
            results["reset"] = self.run_vm("alice", ["reset", "debian", "--yes"])
            results["install"] = sandbox.install_many("alice", ["alpine"])
            results["doctor"] = self.run_vm("alice", ["doctor", "debian"])
            results["update"] = self.run_vm("alice", ["update", "debian"])
            results["raw"] = None
            try:
                sandbox.reset("alice", "debian")
            except sandbox.SandboxError as e:
                results["raw"] = str(e)
        fake = FakeApt(root)
        fake.during_upgrade = during
        self.update("alice", "debian", fake)
        self.assertIn("being updated", results["remove"])
        self.assertIn("being updated", results["reset"])
        self.assertFalse(results["install"][0])
        self.assertIn("being updated", results["doctor"])
        self.assertIn("already being updated", results["update"])
        self.assertIn("being updated", results["raw"])
        self.assertTrue((root / "usr/bin/tool").exists())       # nothing was deleted

    def test_account_rename_and_delete_wait_for_the_update(self):
        import cmd_users
        root = make_vm("alice")
        out = {}

        def during():
            out["ruser"] = cmd_users.cmd_ruser({"sid": "ark", "username": "ark"}, ["alice"])
            with self.assertRaises(sandbox.SandboxError):
                sandbox.rename_user("alice", "alicia")
        fake = FakeApt(root)
        fake.during_upgrade = during
        self.update("alice", "debian", fake)
        self.assertIn("being updated", out["ruser"])
        self.assertIsNotNone(auth.get_user("alice"))

    def test_services_wait_quietly_and_start_again_afterwards(self):
        root = make_vm("alice")
        services.add("web", "alice", "sleep 30", "vm:debian")
        started = []
        real = jobs.start_job
        with mock.patch.object(jobs, "start_job", side_effect=lambda *a, **k: started.append(a) or real(*a, **k)):
            fake = FakeApt(root)

            def during():
                services.tick(now=1000)
                services.tick(now=1002)
            fake.during_upgrade = during
            self.update("alice", "debian", fake)
        self.assertEqual(started, [])                           # nothing was launched meanwhile
        rt = services._rt["web"]
        self.assertEqual(rt["restarts"], 0)                     # and no restart storm
        self.assertIn("being updated", rt["last_error"])


class WhoMayUpdateTests(Base):
    def test_admins_must_be_in_the_host_shell(self):
        make_vm("ark")
        state.set_mode("ark", sandbox.VM)
        out = self.run_vm("ark", ["update", "debian"])
        self.assertIn("host shell", out)
        self.assertIn("vm mode host", out)
        self.assertEqual(sandbox.install_state("ark", "debian")["state"], "ready")
        state.set_mode("ark", sandbox.HOST)
        root = sandbox.rootfs_path("ark", "debian")
        ok, msg = self.update("ark", "debian", FakeApt(root))
        self.assertTrue(ok, msg)
        self.assertEqual((root / "usr/bin/tool").read_text(), "v2")

    def test_a_user_who_may_use_the_host_keeps_host_work_when_in_the_host_shell(self):
        make_vm("alice", "debian")
        self.assertTrue(vmupdate.stops_everything("alice", "debian"))
        with mock.patch.object(config, "ALLOW_USER_HOST", True):
            state.set_mode("alice", sandbox.HOST)
            self.assertFalse(vmupdate.stops_everything("alice", "debian"))
            state.set_mode("alice", sandbox.VM)
            self.assertTrue(vmupdate.stops_everything("alice", "debian"))

    def test_the_name_is_optional_only_with_a_single_vm(self):
        root = make_vm("alice", "debian")
        with mock.patch.object(vmupdate, "_exec", FakeApt(root)):
            out = self.run_vm("alice", ["update"])
            self.assertIn("Updating debian", out)
            self.wait("alice", "debian")
        make_vm("alice", "alpine", "alpine")
        out = self.run_vm("alice", ["update"])
        self.assertIsInstance(out, dict)                          # a picker, never a guess
        self.assertEqual([o["cmds"] for o in out["choice"]["options"]],
                         [["vm update debian"], ["vm update alpine"]])
        self.assertIn("not installed", self.run_vm("alice", ["update", "kali"]))
        self.assertIn("vm install kali", self.run_vm("alice", ["update", "kali"]))

    def test_no_vm_at_all_offers_to_install_one(self):
        out = self.run_vm("alice", ["update"])
        self.assertIsInstance(out, dict)
        self.assertIn("no system", out["output"])

    def test_with_one_vm_everything_stops_but_with_several_only_that_vm(self):
        self.assertFalse(vmupdate.stops_everything("alice", "debian"))     # no vm yet
        make_vm("alice", "debian")
        self.assertTrue(vmupdate.stops_everything("alice", "debian"))
        self.assertFalse(vmupdate.stops_everything("ark", "debian"))      # admins never
        make_vm("alice", "alpine", "alpine")
        self.assertFalse(vmupdate.stops_everything("alice", "debian"))

    def _fake_running(self, sid, vm_name):
        killed = []
        job = SimpleNamespace(alive=True, vm=vm_name, kill=lambda: killed.append(("job", vm_name)))
        term = SimpleNamespace(alive=True, vm=vm_name, kill=lambda: killed.append(("term", vm_name)))
        return job, term, killed

    def test_only_what_runs_in_the_updating_vm_stops_when_there_are_several(self):
        make_vm("alice", "debian")
        make_vm("alice", "alpine", "alpine")
        killed = []
        jd = SimpleNamespace(alive=True, vm="debian", kill=lambda: killed.append("job-debian"))
        ja = SimpleNamespace(alive=True, vm="alpine", kill=lambda: killed.append("job-alpine"))
        td = SimpleNamespace(alive=True, vm="debian", kill=lambda: killed.append("term-debian"))
        ta = SimpleNamespace(alive=True, vm="alpine", kill=lambda: killed.append("term-alpine"))
        jobs.JOBS[("alice", 901)], jobs.JOBS[("alice", 902)] = jd, ja
        termsess.SESSIONS[("alice", "t1")], termsess.SESSIONS[("alice", "t2")] = td, ta
        try:
            vmupdate.stop_running("alice", "debian", everything=False)
            self.assertEqual(sorted(killed), ["job-debian", "term-debian"])
            self.assertNotIn(("alice", "t1"), termsess.SESSIONS)
            self.assertIn(("alice", "t2"), termsess.SESSIONS)
        finally:
            for k in (("alice", 901), ("alice", 902)):
                jobs.JOBS.pop(k, None)
            termsess.SESSIONS.pop(("alice", "t2"), None)

    def test_with_everything_set_all_of_the_users_processes_stop(self):
        make_vm("alice", "debian")
        killed = []
        j1 = SimpleNamespace(alive=True, vm="debian", kill=lambda: killed.append("j1"))
        j2 = SimpleNamespace(alive=True, vm="", kill=lambda: killed.append("j2"))
        t1 = SimpleNamespace(alive=True, vm="debian", kill=lambda: killed.append("t1"))
        jobs.JOBS[("alice", 911)], jobs.JOBS[("alice", 912)] = j1, j2
        termsess.SESSIONS[("alice", "a")] = t1
        try:
            vmupdate.stop_running("alice", "debian", everything=True)
            self.assertEqual(sorted(killed), ["j1", "j2", "t1"])
        finally:
            for k in (("alice", 911), ("alice", 912)):
                jobs.JOBS.pop(k, None)

    def test_running_things_need_a_yes_and_the_message_says_what_stops(self):
        root = make_vm("alice")
        t = SimpleNamespace(alive=True, vm="debian", kill=lambda: None)
        termsess.SESSIONS[("alice", "x")] = t
        try:
            out = self.run_vm("alice", ["update"])
            self.assertIn("1 terminal(s)", out)
            self.assertIn("--yes", out)
            self.assertEqual(sandbox.install_state("alice", "debian")["state"], "ready")
            with mock.patch.object(vmupdate, "_exec", FakeApt(root)):
                out = self.run_vm("alice", ["update", "--yes"])
                self.assertIn("Updating debian", out)
                self.assertIn("everything you had running", out)
                self.wait("alice", "debian")
            self.assertNotIn(("alice", "x"), termsess.SESSIONS)
        finally:
            termsess.SESSIONS.pop(("alice", "x"), None)

    def test_low_disk_is_refused_up_front(self):
        make_vm("alice")
        usage = SimpleNamespace(total=10 ** 9, used=10 ** 9 - 1000, free=1000)
        with mock.patch.object(vmupdate.shutil, "disk_usage", return_value=usage):
            out = self.run_vm("alice", ["update"])
        self.assertIn("free", out)
        self.assertEqual(sandbox.install_state("alice", "debian")["state"], "ready")

    def test_only_a_limited_number_of_updates_run_at_once(self):
        roots = [make_vm("alice", "debian"), make_vm("ark", "debian")]
        state.set_mode("ark", sandbox.HOST)
        gate = threading.Event()
        started = threading.Event()

        def slow():
            started.set()
            gate.wait(10)
        fakes = [FakeApt(r) for r in roots]
        fakes[0].during_upgrade = slow
        with mock.patch.object(vmupdate, "_exec", lambda sid, vm, *a, **k: (
                fakes[0] if sid == "alice" else fakes[1])(sid, vm, *a, **k)):
            self.assertTrue(vmupdate.start("alice", "debian")[0])
            started.wait(10)
            held = [vmupdate._slots.acquire(blocking=False) for _ in range(vmupdate.MAX_PARALLEL)]
            try:
                ok, msg = vmupdate.start("ark", "debian")
                self.assertFalse(ok)
                self.assertIn("other updates are running", msg)
            finally:
                for h in held:
                    if h:
                        vmupdate._slots.release()
                gate.set()
                self.wait("alice", "debian")


class RecoveryAndShutdownTests(Base):
    def test_a_crash_during_the_upgrade_is_rolled_back_on_the_next_start(self):
        root = make_vm("alice")
        base = sandbox.vm_dir("alice", "debian")
        vmupdate.make_snapshot(root, base / vmupdate.SNAP)
        replace_file(root / "usr/bin/tool", "half-upgraded")
        vmupdate._write_meta(base, phase="upgrade", started=1, finished=None)
        self.assertEqual(vmupdate.stale_updates(), [("alice", "debian")])
        self.assertTrue(vmupdate.recover_background())
        self.assertIn("updating", sandbox.install_state("alice", "debian")["state"])   # locked meanwhile
        self.wait("alice", "debian")
        self.assertEqual((root / "usr/bin/tool").read_text(), "v1")
        meta = vmupdate.read_meta("alice", "debian")
        self.assertEqual(meta["phase"], "recovered")
        self.assertIn("restored", meta["message"])
        self.assertEqual(vmupdate.stale_updates(), [])

    def test_leftovers_of_an_interrupted_rollback_are_swept_at_start(self):
        make_vm("alice")
        left = sandbox.vm_dir("alice", "debian") / vmupdate.FAILED
        (left / "usr").mkdir(parents=True)
        self.assertEqual(vmupdate._sweep_leftovers(), 1)
        end = time.time() + 5
        while left.exists() and time.time() < end:
            time.sleep(0.05)
        self.assertFalse(left.exists())

    def test_a_crash_before_the_snapshot_finished_leaves_the_system_alone(self):
        root = make_vm("alice")
        base = sandbox.vm_dir("alice", "debian")
        (base / vmupdate.SNAP / "etc").mkdir(parents=True)             # partial snapshot
        vmupdate._write_meta(base, phase="snapshot", started=1, finished=None)
        vmupdate.recover_background()
        self.wait("alice", "debian")
        self.assertFalse((base / vmupdate.SNAP).exists())
        self.assertEqual((root / "usr/bin/tool").read_text(), "v1")

    def test_shutdown_interrupts_a_running_update_and_rolls_it_back(self):
        root = make_vm("alice")
        started = threading.Event()
        fake = FakeApt(root)

        def real_exec(sid, vm, script, timeout, on_line):
            cmds = vmupdate.commands_for("debian", vm)
            if script == cmds["upgrade"]:
                replace_file(root / "usr/bin/tool", "half")
                started.set()
                end = time.time() + 10
                while not vmupdate._abort.is_set() and time.time() < end:
                    time.sleep(0.02)
                return -9, "(interrupted: server shutting down)"
            return fake(sid, vm, script, timeout, on_line)
        with mock.patch.object(vmupdate, "_exec", real_exec):
            self.assertTrue(vmupdate.start("alice", "debian")[0])
            self.assertTrue(started.wait(10))
            self.assertTrue(sandbox.any_updating())
            vmupdate.abort_all(timeout=10)
        self.assertFalse(sandbox.any_updating())
        self.assertEqual((root / "usr/bin/tool").read_text(), "v1")
        self.assertEqual(vmupdate.read_meta("alice", "debian")["phase"], "rolled-back")

    def test_lifecycle_shutdown_waits_for_and_rolls_back_updates(self):
        import lifecycle
        calls = []
        with mock.patch.object(vmupdate, "abort_all", lambda *a, **k: calls.append("abort")), \
                mock.patch.object(lifecycle.backup, "final", lambda **k: calls.append("backup")):
            lifecycle.reset()
            lifecycle.begin_shutdown()
        lifecycle.reset()
        self.assertEqual(calls, ["abort", "backup"])                    # rolled back BEFORE the backup


if __name__ == "__main__":
    unittest.main()
