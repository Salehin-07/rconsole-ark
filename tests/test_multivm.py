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
import sandbox
import state

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
        bashrc = (sandbox.home_path("alice", "debian") / ".bashrc").read_text()
        self.assertIn("u@debian", bashrc)
        self.assertIn(".bashrc", (sandbox.home_path("alice", "debian") / ".profile").read_text())

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
        self.assertEqual(out[1]["cwd"], "/root")
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
        self.assertEqual(r["cwds"]["main"], "/root")
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

    def test_catalog_alias_install_choice(self):
        self.assertEqual(distros.parse_choice("কালী")[0].id, "kali")
        self.install("কালী")
        self.assertEqual(sandbox.installed_vms("alice"), ["kali"])
        meta = json.loads((sandbox.vm_dir("alice", "kali") / "distro.json").read_text())
        self.assertEqual(meta["id"], "kali")


if __name__ == "__main__":
    unittest.main()
