"""Several VMs per user: separate roots/homes, picking, targets API, migration.

Every system is installed from one locally served busybox tarball (via
RCONSOLE_VM_ROOTFS_URL), so the whole pipeline runs for real without a network.
"""
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import helpers  # noqa: F401

import app as rapp
import auth
import config
import distros
import pidns
import sandbox
import services
import state
import vmdeps

PROOT = os.environ.get("RCONSOLE_VM_PROOT") or shutil.which("proot")
BUSYBOX = os.environ.get("RCONSOLE_TEST_BUSYBOX") or shutil.which("busybox")


@unittest.skipUnless(PROOT and BUSYBOX, "needs proot and busybox")
class MultiVm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_vm_e2e as e2e
        os.environ["RCONSOLE_VM_PROOT"] = PROOT
        cls.srv_dir = Path(tempfile.mkdtemp(prefix="mv-srv-"))
        tarball = cls.srv_dir / "root.tar.gz"
        e2e.build_rootfs_tarball(tarball)
        os.environ["RCONSOLE_VM_ROOTFS_SHA256"] = hashlib.sha256(tarball.read_bytes()).hexdigest()
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=str(cls.srv_dir), **k)
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        os.environ["RCONSOLE_VM_ROOTFS_URL"] = (
            f"http://127.0.0.1:{cls.httpd.server_address[1]}/root.tar.gz")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        helpers.wipe_vms()
        shutil.rmtree(cls.srv_dir, ignore_errors=True)
        for k in ("RCONSOLE_VM_ROOTFS_URL", "RCONSOLE_VM_ROOTFS_SHA256"):
            os.environ.pop(k, None)

    def setUp(self):
        helpers.wipe_vms()
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()
        auth.set_password("ark", "adminpass123")
        auth.create_user("alice", "longenough1")
        state.STATE.pop("alice", None)
        state.new_session("alice")
        for u in ("alice", "ark"):
            state.set_vm(u, None)
            state.set_mode(u, None)
        rapp.app.config["TESTING"] = True
        rapp.USER_THROTTLE._fails.clear()
        rapp.IP_THROTTLE._fails.clear()

    def run_in(self, cmd, sid="alice", tty=False):
        sp = sandbox.spec(sid, cmd, "/root", tty=tty)
        return subprocess.run(sp.argv, cwd=sp.cwd, env=sp.env, capture_output=True,
                              text=True, timeout=30)

    def install(self, *names, sid="alice"):
        ok, msg = sandbox.install_many(sid, list(names), wait=True)
        self.assertTrue(ok, msg)

    # ------------------------------------------------------------ separation
    def test_each_system_has_its_own_root_and_home(self):
        self.install("alpine", "debian", "kali")
        self.assertEqual(sandbox.installed_vms("alice"), ["alpine", "debian", "kali"])
        roots = {sandbox.rootfs_path("alice", v) for v in ("alpine", "debian", "kali")}
        homes = {sandbox.home_path("alice", v) for v in ("alpine", "debian", "kali")}
        self.assertEqual(len(roots), 3)
        self.assertEqual(len(homes), 3)
        self.run_in("@vm:debian echo from-debian > /root/who; echo d > /etc/only-in-debian")
        self.assertEqual(self.run_in("@vm:debian cat /root/who").stdout.strip(), "from-debian")
        # the other systems cannot see debian's home or files
        self.assertNotEqual(self.run_in("@vm:alpine cat /root/who").stdout.strip(), "from-debian")
        self.assertNotEqual(self.run_in("@vm:kali cat /etc/only-in-debian").stdout.strip(), "d")
        self.assertEqual(self.run_in("@vm:kali echo $RCONSOLE_VM").stdout.strip(), "kali")

    def test_debian_family_tweaks_apply_only_to_those_systems(self):
        self.install("alpine", "ubuntu")
        apt = "etc/apt/apt.conf.d/99rconsole"
        conf = (sandbox.rootfs_path("alice", "ubuntu") / apt).read_text()
        self.assertIn('APT::Sandbox::User "root"', conf)
        self.assertIn('APT::Cmd::Disable-Script-Warning "true"', conf)   # the "stable CLI" nag
        self.assertFalse((sandbox.rootfs_path("alice", "alpine") / apt).exists())
        self.assertEqual(sandbox.family("alice", "ubuntu"), "debian")
        self.assertEqual(sandbox.family("alice", "alpine"), "alpine")

    def test_resolv_conf_symlink_is_made_a_real_file(self):
        root = Path(tempfile.mkdtemp())
        (root / "etc").mkdir()
        os.symlink("../run/systemd/resolve/stub-resolv.conf", root / "etc/resolv.conf")
        sandbox._fix_resolv_conf(root)
        conf = root / "etc/resolv.conf"
        self.assertFalse(conf.is_symlink())
        self.assertIn("nameserver", conf.read_text())
        shutil.rmtree(root)

    def test_home_gets_dotfiles_and_a_prompt_naming_the_vm(self):
        self.install("debian")
        home = sandbox.home_path("alice", "debian")
        # the prompt reads <system>@root and is loaded from both .bashrc and .profile
        self.assertIn(".rconsole_prompt", (home / ".bashrc").read_text())
        self.assertIn(".rconsole_prompt", (home / ".profile").read_text())
        self.assertIn("${RCONSOLE_VM:-vm}@root", (home / ".rconsole_prompt").read_text())
        self.assertEqual(sandbox._vm_env("alice", "debian")["RCONSOLE_VM"], "debian")
        self.assertIn(".bashrc", (home / ".profile").read_text())

    def test_existing_vms_get_hushlogin_once(self):
        self.install("kali")
        home = sandbox.home_path("alice", "kali")
        (home / ".hushlogin").unlink()
        (home / ".rconsole-hush-done").unlink()                 # a VM made before the fix
        sandbox.sync_guest_tools("alice", "kali")
        self.assertTrue((home / ".hushlogin").exists())         # applied
        (home / ".hushlogin").unlink()                          # the user wants the banner back
        sandbox.sync_guest_tools("alice", "kali")
        self.assertFalse((home / ".hushlogin").exists())        # and we respect that

    def test_never_asks_questions_even_in_a_terminal(self):
        """Regression: tzdata's 'select your geographic area' prompt in `vm shell`."""
        self.install("debian")
        plain = sandbox.spec("alice", "true", "/root")
        tty = sandbox.spec("alice", "", "/root", tty=True)
        for sp in (plain, tty):
            self.assertEqual(sp.env["DEBIAN_FRONTEND"], "noninteractive")
            self.assertEqual(sp.env["NEEDRESTART_MODE"], "a")
            self.assertEqual(sp.env["PIP_BREAK_SYSTEM_PACKAGES"], "1")   # bare `pip install` works
            self.assertEqual(sp.env["PIP_ROOT_USER_ACTION"], "ignore")
        self.assertEqual(plain.vm, "debian")

    def test_default_install_is_debian(self):
        ok, msg = sandbox.install("alice", wait=True)                 # no name given
        self.assertTrue(ok, msg)
        self.assertEqual(sandbox.installed_vms("alice"), ["debian"])
        self.assertEqual(sandbox.family("alice"), "debian")

    def test_timezone_hushlogin_and_private_dev_net(self):
        self.install("kali")
        root = sandbox.rootfs_path("alice", "kali")
        self.assertEqual((root / "etc/timezone").read_text().strip(), "Etc/UTC")  # tzdata pre-answered
        self.assertTrue((sandbox.home_path("alice", "kali") / ".hushlogin").exists())  # Kali banner
        argv = " ".join(sandbox.spec("alice", "true", "/root").argv)
        self.assertIn(f"{sandbox.vm_dir('alice', 'kali') / 'dev-net'}:/dev/net", argv)
        # Kali's login script does exactly this; both steps must now succeed.
        out = self.run_in("mkdir -p /dev/net && ln -s /dev/null /dev/net/tun && echo ok")
        self.assertEqual(out.stdout.strip(), "ok", out.stderr)
        self.assertTrue((sandbox.vm_dir("alice", "kali") / "dev-net" / "tun").is_symlink())

    # ------------------------------------------------------------- choosing
    def test_one_system_is_automatic_several_need_a_choice(self):
        self.install("alpine")
        self.assertEqual(sandbox.active_vm("alice"), "alpine")
        self.assertEqual(sandbox.resolve("alice").vm, "alpine")        # no questions asked
        self.install("debian")
        self.assertIsNone(sandbox.active_vm("alice"))
        with self.assertRaises(sandbox.NeedVmChoice) as cm:
            sandbox.resolve("alice")
        self.assertEqual(cm.exception.vms, ["alpine", "debian"])
        ok, msg = sandbox.set_mode("alice", "vm")                      # ambiguous: refused
        self.assertFalse(ok)
        self.assertIn("several systems", msg)
        ok, _ = sandbox.set_mode("alice", "vm", "debian")
        self.assertTrue(ok)
        self.assertEqual(sandbox.resolve("alice").vm, "debian")
        self.assertEqual(sandbox.current_target("alice"), "vm:debian")
        self.assertEqual(sandbox.target_label("alice"), "debian")

    def test_forced_target_overrides_the_active_one(self):
        self.install("alpine", "kali")
        sandbox.set_mode("alice", "vm", "kali")
        self.assertEqual(self.run_in("echo $RCONSOLE_VM").stdout.strip(), "kali")
        self.assertEqual(self.run_in("@vm:alpine echo $RCONSOLE_VM").stdout.strip(), "alpine")
        with self.assertRaises(sandbox.SandboxError):
            sandbox.spec("alice", "@vm:ubuntu id", "/")                # not installed

    def test_choice_payload_lists_installed_and_installable(self):
        self.install("alpine", "debian")
        pick = sandbox.choice("alice", then="ls -la")
        runs = [o for o in pick["options"] if o.get("group") != "install"]
        self.assertEqual([o["cmds"] for o in runs],
                         [["vm use alpine", "ls -la"], ["vm use debian", "ls -la"]])
        installs = {o["cmds"][0] for o in pick["options"] if o.get("group") == "install"}
        self.assertEqual(installs, {"vm install ubuntu", "vm install kali"})
        tpl = sandbox.choice("alice", template="vm shell {vm}", install=False)
        self.assertEqual([o["cmds"] for o in tpl["options"]], [["vm shell alpine"], ["vm shell debian"]])

    def test_admin_picker_offers_the_host_too(self):
        self.install("alpine", "debian", sid="ark")
        pick = sandbox.choice("ark", then="ls")
        self.assertIn(["vm mode host", "ls"], [o["cmds"] for o in pick["options"]])
        self.assertFalse(any(o["cmds"][0] == "vm mode host"
                             for o in sandbox.choice("alice", then="ls")["options"]))

    # --------------------------------------------------------- through HTTP
    def client(self, user, pw):
        c = rapp.app.test_client()
        r = c.post("/login", data={"username": user, "password": pw})
        self.assertEqual(r.status_code, 302)
        return c

    def run_cmd(self, c, cmd):
        return c.post("/api/run", json={"cmd": cmd, "tab": "main", "cols": 60}).get_json()

    def test_plain_command_with_several_systems_returns_a_picker_then_runs(self):
        self.install("alpine", "debian")
        c = self.client("alice", "longenough1")
        res = self.run_cmd(c, "id")
        self.assertIn("choice", res)
        opt = [o for o in res["choice"]["options"] if o["cmds"][0] == "vm use debian"][0]
        self.assertEqual(opt["cmds"], ["vm use debian", "id"])
        # the browser runs the picked option's commands in order
        out = [self.run_cmd(c, cmd) for cmd in opt["cmds"]]
        self.assertIn("Using debian", out[0]["output"])
        self.assertIn("uid=0", out[1]["output"])
        self.assertEqual(out[1]["target"], "debian")                   # prompt label
        self.assertEqual(out[1]["cwd"], "/home")
        self.assertNotIn("choice", self.run_cmd(c, "id"))              # the choice stuck

    def test_vm_commands_with_several_systems(self):
        self.install("alpine", "debian")
        c = self.client("alice", "longenough1")
        shell = self.run_cmd(c, "vm shell")                            # which one?
        self.assertEqual([o["cmds"] for o in shell["choice"]["options"]
                          if o.get("group") != "install"],
                         [["vm shell alpine"], ["vm shell debian"]])
        self.assertEqual(self.run_cmd(c, "vm shell debian")["cmd"], "@vm:debian")
        self.assertEqual(self.run_cmd(c, "vm run --vm alpine echo $RCONSOLE_VM")["output"].strip(),
                         "alpine")
        self.assertIn("choice", self.run_cmd(c, "vm run echo x"))
        status = self.run_cmd(c, "vm")["output"]
        self.assertIn("Your systems (2)", status)
        self.assertIn("vm setup python", status)
        self.assertIn("alpine", status)
        self.assertIn("debian", status)
        self.assertIn("choice", self.run_cmd(c, "vm mode vm"))         # ambiguous -> picker
        self.assertIn("mode: vm (debian)", self.run_cmd(c, "vm mode vm debian")["output"])
        self.assertIn("debian", self.run_cmd(c, "vm mode")["output"])

    def test_install_several_and_all(self):
        c = self.client("alice", "longenough1")
        res = self.run_cmd(c, "vm install debian ubuntu")["output"]
        self.assertIn("installing", res)
        import time
        for _ in range(60):
            if not sandbox.installing("alice"):
                break
            time.sleep(0.2)
        self.assertEqual(sorted(sandbox.installed_vms("alice")), ["debian", "ubuntu"])
        self.assertIn("already installed", self.run_cmd(c, "vm install debian")["output"])
        self.run_cmd(c, "vm install all")                              # only adds the missing
        for _ in range(60):
            if not sandbox.installing("alice"):
                break
            time.sleep(0.2)
        self.assertEqual(sorted(sandbox.installed_vms("alice")), ["alpine", "debian", "kali", "ubuntu"])

    def test_remove_one_keeps_the_others(self):
        self.install("alpine", "debian")
        c = self.client("alice", "longenough1")
        self.assertIn("--yes", self.run_cmd(c, "vm remove debian")["output"])
        self.assertIn("which one", self.run_cmd(c, "vm remove --yes")["output"])
        self.assertIn("not installed", self.run_cmd(c, "vm remove kali --yes")["output"])
        self.assertIn("removed", self.run_cmd(c, "vm remove debian --yes")["output"])
        self.assertEqual(sandbox.installed_vms("alice"), ["alpine"])
        self.assertEqual(sandbox.active_vm("alice"), "alpine")        # one left: automatic again
        self.assertFalse(sandbox.vm_dir("alice", "debian").exists())
        self.assertIn("removed", self.run_cmd(c, "vm remove all --yes")["output"])
        self.assertEqual(sandbox.installed_vms("alice"), [])

    def test_targets_api_and_switching(self):
        self.install("alpine", "debian")
        c = self.client("alice", "longenough1")
        info = c.get("/api/targets").get_json()
        self.assertEqual([t["id"] for t in info["targets"]], ["vm:alpine", "vm:debian"])
        self.assertIsNone(info["current"])
        self.assertEqual({d["id"] for d in info["installable"]}, {"ubuntu", "kali"})
        r = c.post("/api/target", json={"target": "vm:debian"}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["current"], "vm:debian")
        self.assertEqual(r["label"], "debian")
        self.assertEqual(r["cwds"]["main"], "/home")
        self.assertFalse(c.post("/api/target", json={"target": "host"}).get_json()["ok"])   # not admin
        self.assertFalse(c.post("/api/target", json={"target": "vm:kali"}).get_json()["ok"])
        self.assertEqual(c.post("/api/target", json={"target": "nope"}).status_code, 400)
        admin = self.client("ark", "adminpass123")
        self.assertEqual(admin.get("/api/targets").get_json()["targets"][0]["id"], "host")
        self.assertTrue(admin.post("/api/target", json={"target": "host"}).get_json()["ok"])

    def test_targets_api_requires_login_and_origin(self):
        c = rapp.app.test_client()
        self.assertEqual(c.get("/api/targets").status_code, 401)
        self.assertEqual(c.post("/api/target", json={"target": "host"}).status_code, 401)
        c = self.client("alice", "longenough1")
        r = c.post("/api/target", json={"target": "vm:alpine"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_file_api_asks_when_it_cannot_tell_which_system(self):
        self.install("alpine", "debian")
        c = self.client("alice", "longenough1")
        r = c.get("/api/dir", query_string={"path": "/"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["choose"])
        c.post("/api/target", json={"target": "vm:debian"})
        self.assertEqual(c.get("/api/dir", query_string={"path": "/"}).status_code, 200)

    def test_files_are_per_system(self):
        self.install("alpine", "debian")
        c = self.client("alice", "longenough1")
        c.post("/api/target", json={"target": "vm:debian"})
        c.post("/api/file", json={"path": "/root/note.txt", "content": "debian note"})
        c.post("/api/target", json={"target": "vm:alpine"})
        self.assertFalse(c.get("/api/file", query_string={"path": "/root/note.txt"}).get_json()["exists"])
        self.assertEqual(
            (sandbox.home_path("alice", "debian") / "note.txt").read_text(), "debian note")

    def test_neofetch_follows_the_chosen_system(self):
        self.install("alpine", "debian")
        c = self.client("alice", "longenough1")
        self.assertIn("choice", self.run_cmd(c, "neofetch"))
        c.post("/api/target", json={"target": "vm:debian"})
        out = self.run_cmd(c, "neofetch --plain")["output"]
        self.assertIn("root@debian", out)                              # HOSTNAME is the VM's name

    def test_vm_setup_lists_profiles_and_starts_a_job(self):
        self.install("debian")
        c = self.client("alice", "longenough1")
        listing = self.run_cmd(c, "vm setup")["output"]
        for name in ("python", "build", "node", "rust", "db", "tools", "all"):
            self.assertIn(name, listing)
        self.assertIn("psycopg2", listing)
        self.assertIn("unknown profile", self.run_cmd(c, "vm setup cobol")["output"])
        res = self.run_cmd(c, "vm setup python")
        self.assertTrue(res["fg"])                                      # output streams live
        jid = res["jobtail"]
        script = sandbox.rootfs_path("alice", "debian") / "tmp" / "rconsole-setup-python.sh"
        text = script.read_text()
        self.assertIn("libpq-dev", text)                                # psycopg2's headers
        self.assertIn("build-essential", text)                          # compiler
        self.assertIn("apt-get install", text)
        import jobs, time
        job = jobs.get_job("alice", jid)
        self.assertIn("rconsole-setup-python.sh", job.cmd)
        for _ in range(50):                                             # runs in the VM (busybox:
            if not job.alive:                                           #  no apt, so it stops early)
                break
            time.sleep(0.1)
        text_out, _ = job.read_from(0)
        self.assertIn("Setting up 'python'", text_out)

    def test_vm_setup_asks_which_system_when_ambiguous(self):
        self.install("alpine", "debian")
        c = self.client("alice", "longenough1")
        res = self.run_cmd(c, "vm setup python")
        self.assertEqual([o["cmds"] for o in res["choice"]["options"] if o.get("group") != "install"],
                         [["vm setup python alpine"], ["vm setup python debian"]])

    def test_doctor_reports_each_check(self):
        self.install("alpine")
        c = self.client("alice", "longenough1")
        out = self.run_cmd(c, "vm doctor")["output"]
        self.assertIn("Doctor: alpine", out)
        self.assertIn("✓ start a shell", out)
        self.assertIn("✓ run programs (ls)", out)
        self.assertIn("✓ you are root", out)
        self.assertIn("proot", out)

    # -------------------------------------------------------- housekeeping
    def test_legacy_single_vm_layout_is_migrated(self):
        self.install("debian")
        # rebuild the old layout: everything directly under <user>/
        new = sandbox.vm_dir("alice", "debian")
        base = sandbox.user_dir("alice")
        for name in ("rootfs", "home", "distro.json", ".ready"):
            os.replace(new / name, base / name)
        shutil.rmtree(new / "dev-net", ignore_errors=True)       # didn't exist in the old layout
        new.rmdir()
        sandbox._migrated.discard("alice")
        self.assertEqual(sandbox.installed_vms("alice"), ["debian"])    # moved under debian/
        self.assertTrue((base / "debian" / "rootfs").is_dir())
        self.assertFalse((base / "rootfs").exists())
        self.assertEqual(self.run_in("echo ok").stdout.strip(), "ok")

    def test_rename_and_delete_user_move_or_remove_every_vm(self):
        self.install("alpine", "debian")
        c = self.client("alice", "longenough1")
        self.assertTrue(auth.set_username("alice", "alicia")[0])
        sandbox.rename_user("alice", "alicia")
        self.assertEqual(sandbox.installed_vms("alicia"), ["alpine", "debian"])
        sandbox.reset("alicia", keep_home=False)
        self.assertFalse(sandbox.user_dir("alicia").exists())
        del c

    def test_pinned_proot_is_preferred_over_the_system_one(self):
        saved = os.environ.pop("RCONSOLE_VM_PROOT", None)
        pinned = config.VM_DIR / "bin" / "proot"
        pinned.parent.mkdir(parents=True, exist_ok=True)
        pinned.write_text("#!/bin/sh\nexit 0\n")
        pinned.chmod(0o755)
        try:
            self.assertEqual(sandbox.find_proot(), str(pinned))
        finally:
            pinned.unlink()
            if saved:
                os.environ["RCONSOLE_VM_PROOT"] = saved

    def test_a_redeploy_is_survivable_backup_restore_rebuilds_vms(self):
        """The free-hosting story: wipe the machine, restore, VMs and files come back."""
        import backup
        self.install("debian")
        sandbox.record_setup("alice", "debian", "tools")
        (sandbox.home_path("alice", "debian") / "bot.py").write_text("print('my bot')")
        saved = (config.BACKUP_DIR, config.BACKUP_PASSPHRASE, config.BACKUP_ENCRYPT, config.BACKUP_URL)
        config.BACKUP_DIR = str(Path(tempfile.mkdtemp(prefix="bk-dest-")))
        config.BACKUP_URL, config.BACKUP_PASSPHRASE, config.BACKUP_ENCRYPT = "", "pass phrase", True
        try:
            ok, msg = backup.backup_now(force=True)
            self.assertTrue(ok, msg)
            helpers.wipe_vms()                                       # a brand-new instance
            self.assertEqual(sandbox.installed_vms("alice"), [])
            ok, msg = backup.restore(force=False)
            self.assertTrue(ok, msg)
            # files and the *list* of VMs are back, the (large) system is not
            self.assertEqual((sandbox.home_path("alice", "debian") / "bot.py").read_text(), "print('my bot')")
            self.assertEqual(sandbox.installed_vms("alice"), [])
            self.assertEqual(sandbox.missing_vms(), [("alice", "debian", "trixie")])
            self.assertEqual(sandbox.recorded_setups("alice", "debian"), ["tools"])
            logs = []
            sandbox.restore_vms(log=logs.append)                     # what the boot thread does
            self.assertEqual(sandbox.installed_vms("alice"), ["debian"])
            self.assertEqual(sandbox.missing_vms(), [])
            self.assertEqual(self.run_in("echo up", sid="alice").stdout.strip(), "up")
            self.assertEqual((sandbox.home_path("alice", "debian") / "bot.py").read_text(), "print('my bot')")
            self.assertTrue(any("rebuilding alice/debian" in m for m in logs), logs)
            self.assertTrue(any("re-applying setup 'tools'" in m for m in logs), logs)
        finally:
            shutil.rmtree(config.BACKUP_DIR, ignore_errors=True)
            config.BACKUP_DIR, config.BACKUP_PASSPHRASE, config.BACKUP_ENCRYPT, config.BACKUP_URL = saved

    def test_removed_or_reset_vms_are_not_resurrected(self):
        self.install("kali")
        sandbox.reset("alice", "kali", keep_home=True)
        self.assertEqual(sandbox.missing_vms(), [])                  # reset deletes distro.json
        self.install("alpine")
        sandbox.reset("alice", "alpine", keep_home=False)
        self.assertEqual(sandbox.missing_vms(), [])

    def test_catalog_alias_install_choice(self):
        self.assertEqual(distros.parse_choice("কালী")[0].id, "kali")
        self.install("কালী")
        self.assertEqual(sandbox.installed_vms("alice"), ["kali"])
        meta = json.loads((sandbox.vm_dir("alice", "kali") / "distro.json").read_text())
        self.assertEqual(meta["id"], "kali")

    # ------------------------------------------------- PID isolation (v1.3.8)
    def _numeric_proc_dirs(self, cmd_vm, sid="alice"):
        out = self.run_in(f"@vm:{cmd_vm} ls /proc", sid=sid).stdout.split()
        return [x for x in out if x.isdigit()]

    @unittest.skipUnless(pidns.probe()[0], "this host does not allow user namespaces")
    def test_every_vm_has_its_own_pid_space(self):
        auth.create_user("bob", "longenough1")
        self.install("alpine", "debian")
        self.install("alpine", sid="bob")
        sp = sandbox.spec("alice", "@vm:alpine sleep 40", "/root")
        sleeper = subprocess.Popen(sp.argv, cwd=sp.cwd, env=sp.env, stdout=subprocess.DEVNULL)
        try:
            import time
            for _ in range(80):
                if any("sleep" in r["cmd"] for r in pidns.vm_procs("alice", "alpine")):
                    break
                time.sleep(0.1)
            self.assertTrue(any("sleep" in r["cmd"] for r in pidns.vm_procs("alice", "alpine")))
            # another VM of the same user, and another user's VM, see none of it
            for sid, vm in (("alice", "debian"), ("bob", "alpine")):
                self.assertFalse(any("sleep 40" in r["cmd"] for r in pidns.vm_procs(sid, vm)))
                self.assertLessEqual(len(self._numeric_proc_dirs(vm, sid)), 3)   # init, proot, shell/ls
                self.assertEqual(self.run_in(f"@vm:{vm} cat /proc/1/cmdline | tr '\\0' ' '", sid=sid).returncode, 0)
            # `kill -9 -1` in one VM only reaches that VM
            self.run_in("@vm:debian kill -9 -1", sid="alice")
            self.assertIsNone(sleeper.poll())
            self.assertTrue(pidns.running("alice", "alpine"))
            # its own namespace PIDs start at 1 and `vm ps` can signal by that PID
            rows = pidns.vm_procs("alice", "alpine")
            target = next(r for r in rows if "sleep 40" in r["cmd"])
            self.assertLess(target["pid"], 50)
            self.assertTrue(pidns.kill_proc("alice", "alpine", target["pid"]))
            sleeper.wait(timeout=10)
            self.assertFalse(pidns.kill_proc("alice", "debian", 1))             # never the init
        finally:
            sleeper.kill()
            pidns.stop_all()

    def test_without_namespaces_the_vm_still_hides_host_processes(self):
        saved = dict(pidns._probe_cache)
        pidns._probe_cache.update(ok=False, reason="test: blocked")
        try:
            self.install("alpine", sid="ark")
            sp = sandbox.spec("ark", "@vm:alpine ls /proc", "/root")        # even for the admin
            self.assertNotIn("nsenter", sp.argv)
            self.assertNotIn("-b /proc ", " ".join(sp.argv) + " ")
            self.assertEqual(self.run_in("@vm:alpine ls /proc", sid="ark").returncode, 0)
        finally:
            pidns._probe_cache.clear()
            pidns._probe_cache.update(saved)

    def test_pidns_can_be_turned_off(self):
        saved = (config.VM_PIDNS, dict(pidns._probe_cache))
        config.VM_PIDNS = "off"
        pidns._probe_cache.clear()
        try:
            self.assertFalse(pidns.active())
            self.assertIn("RCONSOLE_VM_PIDNS", pidns.probe()[1])
        finally:
            config.VM_PIDNS = saved[0]
            pidns._probe_cache.clear()
            pidns._probe_cache.update(saved[1])

    # --------------------------------------- dependency manifest + full restore
    def test_parse_manifest_sections(self):
        out = "\n".join([
            "@@captured", "@@apt", "curl", "git", "@@pyversion", "3.12.3", "@@pip", "requests==2.31.0", "flask==3.0.0",
            "@@npmglobal", '{"dependencies":{"npm":{"version":"10"},"pm2":{"version":"5.3.0"}}}',
            "@@venv ./app/.venv", "django==5.0", "#python 3.12", "@@node ./web", "npm-ci"])
        m = vmdeps.parse(out)
        self.assertEqual(m["system"], {"manager": "apt", "packages": ["curl", "git"]})
        self.assertEqual(m["pip"]["packages"], ["requests==2.31.0", "flask==3.0.0"])
        self.assertEqual(m["npm_global"], ["pm2@5.3.0"])
        self.assertEqual(m["venvs"], [{"path": "app/.venv", "python": "3.12", "packages": ["django==5.0"]}])
        self.assertEqual(m["node_projects"], [{"path": "web", "lock": "npm-ci"}])

    def test_a_partial_restore_never_loses_what_failed(self):
        old = {"system": {"packages": ["curl", "ffmpeg"]}, "pip": {"packages": ["requests==2.31.0", "aiohttp==3.9"]},
               "npm_global": ["pm2@5"], "venvs": [{"path": "a", "packages": []}], "node_projects": []}
        new = {"system": {"packages": ["curl"]}, "pip": {"packages": ["requests==2.31.0"]},
               "npm_global": [], "venvs": [], "node_projects": [{"path": "w", "lock": "npm"}]}
        vmdeps._merge_missing(new, old)
        self.assertEqual(new["system"]["packages"], ["curl", "ffmpeg"])
        self.assertEqual(new["pip"]["packages"], ["aiohttp==3.9", "requests==2.31.0"])
        self.assertEqual(new["npm_global"], ["pm2@5"])
        self.assertEqual([v["path"] for v in new["venvs"]], ["a"])

    def test_restore_script_reinstalls_everything_the_manifest_lists(self):
        self.install("debian")
        root = sandbox.rootfs_path("alice", "debian")
        (root / "usr/bin").mkdir(parents=True, exist_ok=True)
        for tool in ("apt-get", "python3", "npm"):                      # recorders standing in for the real tools
            f = root / "usr/bin" / tool
            f.write_text('#!/bin/sh\necho "' + tool + ' $*" >> /root/calls.log\n')
            f.chmod(0o755)
        (sandbox.home_path("alice", "debian") / "web").mkdir()
        (sandbox.home_path("alice", "debian") / "web/package.json").write_text("{}")
        man = {"version": 1, "captured_at": 1, "family": "debian",
               "system": {"manager": "apt", "packages": ["curl", "git"]},
               "pip": {"python": "3.12.3", "packages": ["requests==2.31.0"]},
               "npm_global": ["pm2@5.3.0"],
               "venvs": [{"path": "bot/.venv", "python": "3.12", "packages": ["discord.py==2.3.2"]}],
               "node_projects": [{"path": "web", "lock": "npm-ci"}]}
        vmdeps._path("alice", "debian").write_text(json.dumps(man))
        logs = []
        ok, failed = vmdeps.restore("alice", "debian", log=logs.append)
        calls = (sandbox.home_path("alice", "debian") / "calls.log").read_text()
        self.assertIn("apt-get install -y --no-install-recommends curl git", calls)
        self.assertIn("python3 -m pip install -r /tmp/rc-pip.txt", calls)
        self.assertIn("npm install -g pm2@5.3.0", calls)
        self.assertIn("python3 -m venv /root/bot/.venv", calls)
        self.assertIn("npm ci", calls)
        self.assertTrue(vmdeps.last_report("alice", "debian")["log"].count("==") >= 5)
        self.assertTrue(logs and "dependencies for alice/debian" in logs[0])

    def test_capture_keeps_the_old_list_when_the_vm_cannot_answer(self):
        self.install("debian")
        vmdeps._path("alice", "debian").write_text(json.dumps({"system": {"manager": "apt", "packages": ["curl"]},
                                                              "pip": {"packages": []}, "captured_at": 1, "fp": "x"}))
        sandbox._set_install("alice", "debian", "updating", "x")
        try:
            ok, msg = vmdeps.capture("alice", "debian", force=True)
        finally:
            sandbox._clear_install("alice", "debian")
        self.assertFalse(ok)
        self.assertEqual(vmdeps.load("alice", "debian")["system"]["packages"], ["curl"])

    def test_services_wait_until_the_vm_is_fully_restored(self):
        """After a redeploy a service must not start on a half-built VM: it is locked until packages are back."""
        import backup
        self.install("debian")
        (sandbox.home_path("alice", "debian") / "bot.py").write_text("print('x')")
        vmdeps._path("alice", "debian").write_text(json.dumps(
            {"version": 1, "captured_at": 1, "system": {"manager": "apt", "packages": ["curl"]},
             "pip": {"packages": ["requests==2.31.0"]}, "npm_global": [], "venvs": [], "node_projects": []}))
        saved = (config.BACKUP_DIR, config.BACKUP_PASSPHRASE, config.BACKUP_ENCRYPT, config.BACKUP_URL)
        config.BACKUP_DIR = str(Path(tempfile.mkdtemp(prefix="bk-dest-")))
        config.BACKUP_URL, config.BACKUP_PASSPHRASE, config.BACKUP_ENCRYPT = "", "pass phrase", True
        seen = []
        real = vmdeps.restore
        try:
            config.RESTORE_DEPS = False          # keep the hand-written list (this tiny test VM has no pip to scan)
            self.assertTrue(backup.backup_now(force=True)[0])
            config.RESTORE_DEPS = True
            helpers.wipe_vms()
            self.assertTrue(backup.restore(force=False)[0])
            self.assertTrue(vmdeps.load("alice", "debian")["pip"]["packages"])       # the list travelled in the backup

            def watch(sid, vm, log=print):
                box = []                         # ask from another thread, as the supervisor / a user would
                t = threading.Thread(target=lambda: box.append(
                    (sandbox.install_state(sid, vm)["state"], services._target_busy(sid, f"vm:{vm}"))))
                t.start()
                t.join()
                seen.append((box[0][0], box[0][1], sandbox.installed(sid, vm)))
                return real(sid, vm, log=log)
            vmdeps.restore = watch
            self.assertTrue(sandbox.restore_vms_background() or True)
            for _ in range(300):
                if sandbox.installed_vms("alice") and not sandbox._restoring:
                    break
                __import__("time").sleep(0.1)
        finally:
            vmdeps.restore = real
            config.RESTORE_DEPS = True
            shutil.rmtree(config.BACKUP_DIR, ignore_errors=True)
            config.BACKUP_DIR, config.BACKUP_PASSPHRASE, config.BACKUP_ENCRYPT, config.BACKUP_URL = saved
        self.assertTrue(seen, "dependencies were never restored")
        state_, busy, installed = seen[0]
        self.assertEqual(state_, "restoring")
        self.assertIn("being restored", busy)
        self.assertTrue(installed)                                   # the system is there, packages are not yet
        self.assertIsNone(services._target_busy("alice", "vm:debian"))   # unlocked afterwards

    def test_status_and_doctor_mention_the_new_things(self):
        import cmd_vm
        self.install("alpine")
        ctx = {"sid": "alice", "cols": 80, "tab": {"cwd": "/root"}}
        self.assertIn("nothing is running", cmd_vm.cmd_vm(ctx, ["ps"]) or "")
        self.assertIn("vm kill", cmd_vm.cmd_vm(ctx, ["kill"]))
        self.assertIn("dependency list", cmd_vm.cmd_vm(ctx, ["deps"]))


if __name__ == "__main__":
    unittest.main()
