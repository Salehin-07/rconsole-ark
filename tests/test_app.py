"""HTTP-level tests of the Flask app (auth, CSRF, throttling, file APIs, proxy)."""
import http.server
import json
import os
import shutil
import threading
import unittest

import helpers  # noqa: F401

import app as rapp
import auth
import sandbox
import state

PROOT = os.environ.get("RCONSOLE_VM_PROOT") or shutil.which("proot")
BUSYBOX = os.environ.get("RCONSOLE_TEST_BUSYBOX") or shutil.which("busybox")


def reset_users():
    if not getattr(reset_users, "keep_vms", False):
        helpers.wipe_vms()
    auth._cache.update(sig=None, users={})
    auth.USERS_FILE.unlink(missing_ok=True)
    auth.seed_defaults()
    auth.create_user("alice", "longenough1")
    auth.set_password("ark", "adminpass123")
    rapp.USER_THROTTLE._fails.clear()
    rapp.IP_THROTTLE._fails.clear()


class Base(unittest.TestCase):
    def setUp(self):
        reset_users()
        rapp.app.config["TESTING"] = True
        self.c = rapp.app.test_client()

    def login(self, user="ark", pw="adminpass123", client=None):
        return (client or self.c).post("/login", data={"username": user, "password": pw})

    def run_cmd(self, cmd, tab="main", client=None):
        r = (client or self.c).post("/api/run", json={"cmd": cmd, "tab": tab})
        return r.get_json()


class AuthFlowTests(Base):
    def test_unauthenticated_is_rejected(self):
        self.assertEqual(self.c.post("/api/run", json={"cmd": "id"}).status_code, 401)
        self.assertEqual(self.c.get("/api/file?path=/etc/passwd").status_code, 401)
        self.assertEqual(self.c.get("/api/dir?path=/").status_code, 401)
        self.assertEqual(self.c.get("/").status_code, 302)

    def test_healthz_is_public(self):
        self.assertEqual(self.c.get("/healthz").get_json(), {"ok": True})

    def test_login_ok_and_cookie_flags(self):
        r = self.login()
        self.assertEqual(r.status_code, 302)
        cookie = r.headers["Set-Cookie"]
        self.assertIn("rconsole_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_bad_login_then_lockout(self):
        for _ in range(3):
            self.assertEqual(self.login("ark", "nope").status_code, 401)
        r = self.login("ark", "adminpass123")        # correct, but locked out
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        # A different account from the same IP is not locked by the per-user key.
        self.assertEqual(self.login("alice", "longenough1").status_code, 302)

    def test_security_headers(self):
        h = self.c.get("/healthz").headers
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["X-Frame-Options"], "SAMEORIGIN")

    def test_default_password_forces_change(self):
        auth.USERS_FILE.unlink()
        auth._cache.update(sig=None, users={})
        auth.seed_defaults()                      # ark / ark
        self.assertEqual(self.login("ark", "ark").status_code, 302)
        out = self.run_cmd("ls")["output"]
        self.assertIn("sudo cpass", out)
        self.assertEqual(self.c.get("/api/dir?path=/").status_code, 403)
        self.assertEqual(self.c.get("/api/file?path=/etc/hostname").status_code, 403)
        # cpass is allowed, and lifts the lock.
        self.run_cmd("sudo cpass")
        self.run_cmd("ark")
        self.run_cmd("brandnewpass1")
        res = self.run_cmd("brandnewpass1")
        self.assertIn("Password updated", res["output"])
        self.assertEqual(self.c.get("/api/dir?path=/").status_code, 200)
        self.assertTrue(auth.verify("ark", "brandnewpass1"))


class CsrfTests(Base):
    def test_cross_origin_post_blocked(self):
        self.login()
        r = self.c.post("/api/run", json={"cmd": "id"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/run", json={"cmd": "id"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/run", json={"cmd": "id"}, headers={"Origin": "null"})
        self.assertEqual(r.status_code, 403)

    def test_same_origin_and_headerless_allowed(self):
        self.login()
        r = self.c.post("/api/run", json={"cmd": "rwhoami"}, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.post("/api/run", json={"cmd": "rwhoami"}).status_code, 200)

    def test_proxied_host_is_accepted(self):
        """Behind a proxy Host may differ from Origin: X-Forwarded-Host fixes it."""
        r = self.c.post("/login", data={"username": "ark", "password": "adminpass123"},
                        headers={"Origin": "https://console.example.com",
                                 "Host": "internal:10000",
                                 "X-Forwarded-Host": "console.example.com"})
        self.assertEqual(r.status_code, 302)
        # ...but a different site is still refused even with that header present.
        r = self.c.post("/login", data={"username": "ark", "password": "adminpass123"},
                        headers={"Origin": "https://evil.example",
                                 "X-Forwarded-Host": "console.example.com"})
        self.assertEqual(r.status_code, 403)

    def test_allowed_origins_env(self):
        import config
        old = config.ALLOWED_ORIGINS
        config.ALLOWED_ORIGINS = ("my.domain.dev",)
        try:
            r = self.c.post("/login", data={"username": "ark", "password": "adminpass123"},
                            headers={"Origin": "https://my.domain.dev", "Host": "internal"})
            self.assertEqual(r.status_code, 302)
        finally:
            config.ALLOWED_ORIGINS = old

    def test_login_csrf_blocked(self):
        r = self.c.post("/login", data={"username": "ark", "password": "adminpass123"},
                        headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_websocket_cross_origin_blocked(self):
        self.login()
        r = self.c.get("/ws", headers={"Upgrade": "websocket", "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)


class HostModeTests(Base):
    def test_admin_shell_pipes_work(self):
        """Regression: shlex.join used to quote '|' so pipes never worked."""
        self.login()
        out = self.run_cmd("echo hello | tr a-z A-Z")["output"]
        self.assertEqual(out.strip(), "HELLO")
        self.assertEqual(self.run_cmd("echo a && echo b")["output"].split(), ["a", "b"])

    def test_at_prefix_and_interactive_detection(self):
        self.login()
        self.assertEqual(self.run_cmd("@host echo hi | tr a-z A-Z")["output"].strip(), "HI")
        self.assertTrue(self.run_cmd("@host vim x.txt").get("pty"))
        self.assertEqual(self.run_cmd("@host vim x.txt")["cmd"], "@host vim x.txt")
        c = rapp.app.test_client()
        self.login("alice", "longenough1", c)
        self.assertIn("restricted to admins", self.run_cmd("@host id", client=c)["output"])

    def test_alias_and_background(self):
        self.login()
        self.run_cmd("alias say=echo")
        self.assertEqual(self.run_cmd("say hi there")["output"].strip(), "hi there")
        self.assertIn("job", self.run_cmd("sleep 0.2 &")["output"])

    def test_non_admin_cannot_run_host_commands(self):
        c = rapp.app.test_client()
        self.login("alice", "longenough1", c)
        out = self.run_cmd("cat /etc/passwd", client=c)["output"]
        self.assertNotIn("root:", out)
        self.assertIn("vm install", out)
        self.assertIn("not in the sudoers", self.run_cmd("sudo id", client=c)["output"])
        self.assertIn("not in the sudoers", self.run_cmd("sudo ls", client=c)["output"])

    def test_non_admin_file_apis_denied_without_vm(self):
        c = rapp.app.test_client()
        self.login("alice", "longenough1", c)
        self.assertEqual(c.get("/api/file?path=/etc/passwd").status_code, 409)
        self.assertEqual(c.get("/api/dir?path=/").status_code, 409)
        r = c.post("/api/fs", json={"action": "mkdir", "path": "/tmp/pwn-test"})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(os.path.exists("/tmp/pwn-test"))

    def test_websocket_cmd_is_authorized(self):
        # The ws handler authorizes through sandbox.resolve_target; a user can't
        # smuggle a host command via the query string, nor persist it.
        with self.assertRaises(sandbox.PermissionDenied):
            sandbox.resolve_target("alice", sandbox.parse_target("@host bash")[0])

    def test_admin_file_api_roundtrip(self):
        self.login()
        p = os.path.join(helpers.os.environ["RCONSOLE_DATA_DIR"], "scratch", "a.txt")
        r = self.c.post("/api/file", json={"path": p, "content": "hi"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.c.get("/api/file", query_string={"path": p}).get_json()["content"], "hi")
        d = self.c.get("/api/dir", query_string={"path": os.path.dirname(p)}).get_json()
        self.assertEqual([e["name"] for e in d["entries"]], ["a.txt"])
        r = self.c.post("/api/fs", json={"action": "rename", "src": p, "dst": p + ".bak"})
        self.assertTrue(r.get_json()["ok"])
        r = self.c.post("/api/fs", json={"action": "rmtree", "path": os.path.dirname(p)})
        self.assertTrue(r.get_json()["ok"])

    def test_big_file_refused_by_editor(self):
        self.login()
        p = os.path.join(os.environ["RCONSOLE_DATA_DIR"], "big.bin")
        with open(p, "wb") as f:
            f.truncate(rapp.MAX_EDIT_BYTES + 1)
        self.assertEqual(self.c.get("/api/file", query_string={"path": p}).status_code, 413)

    def test_kprocess_refuses_server_pid(self):
        self.login()
        out = self.run_cmd(f"sudo kprocess {os.getpid()}")["output"]
        self.assertIn("refusing", out)


class UpstreamHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"cookie": self.headers.get("Cookie", "")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "csrftoken=a; Path=/")
        self.send_header("Set-Cookie", "sessionid=b; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ProxyTests(Base):
    def test_cookie_isolation_and_multiple_set_cookie(self):
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        state.ACTIVE_HOSTS[("site", port)] = {"url": "", "proc": None, "sid": "ark"}
        try:
            self.assertEqual(self.c.get(f"/site/{port}/").status_code, 302)  # needs login
            self.login()
            self.c.set_cookie("appcookie", "xyz")
            r = self.c.get(f"/site/{port}/")
            seen = r.get_json()["cookie"]
            self.assertIn("appcookie=xyz", seen)
            self.assertNotIn("rconsole_session", seen)     # console session never leaks
            cookies = r.headers.getlist("Set-Cookie")
            self.assertTrue(any(c.startswith("csrftoken=") for c in cookies), cookies)
            self.assertTrue(any(c.startswith("sessionid=") for c in cookies), cookies)
        finally:
            state.ACTIVE_HOSTS.pop(("site", port), None)
            httpd.shutdown()


@unittest.skipUnless(PROOT and BUSYBOX, "needs proot and busybox")
class VmApiTests(Base):
    @classmethod
    def setUpClass(cls):
        import test_vm_e2e as e2e
        e2e.VmEndToEnd.setUpClass()
        cls.e2e = e2e

    @classmethod
    def tearDownClass(cls):
        reset_users.keep_vms = False
        cls.e2e.VmEndToEnd.tearDownClass()

    def setUp(self):
        reset_users.keep_vms = True
        super().setUp()
        self.uc = rapp.app.test_client()
        self.login("alice", "longenough1", self.uc)

    def test_plain_command_runs_in_vm_for_user(self):
        out = self.run_cmd("id", client=self.uc)["output"]
        self.assertIn("uid=0", out)
        out = self.run_cmd("echo ok | tr a-z A-Z", client=self.uc)["output"]
        self.assertEqual(out.strip(), "OK")

    def test_cd_is_confined_to_vm(self):
        self.assertEqual(self.run_cmd("cd /etc", client=self.uc)["cwd"], "/etc")
        self.assertEqual(self.run_cmd("pwd", client=self.uc)["output"].strip(), "/etc")
        self.assertEqual(self.run_cmd("cd ../../..", client=self.uc)["cwd"], "/")
        self.assertIn("No such file", self.run_cmd("cd /home/claude", client=self.uc)["output"])
        self.assertEqual(self.run_cmd("cd ~", client=self.uc)["cwd"], "/root")

    def test_file_api_is_confined_and_shares_home(self):
        uc = self.uc
        r = uc.get("/api/file", query_string={"path": "/etc/os-release"}).get_json()
        self.assertIn("TestLinux", r["content"])
        # path traversal lands inside the VM, not on the host
        r = uc.get("/api/file", query_string={"path": "/../../../../etc/os-release"}).get_json()
        self.assertIn("TestLinux", r["content"])
        r = uc.get("/api/file", query_string={"path": "/etc/escape"}).get_json()
        self.assertFalse(r["exists"])
        # a file written by the API is visible to commands inside the VM (/root bind)
        uc.post("/api/file", json={"path": "/root/note.txt", "content": "from api"})
        self.assertEqual(self.run_cmd("cat /root/note.txt", client=self.uc)["output"].strip(), "from api")
        d = uc.get("/api/dir", query_string={"path": "/root"}).get_json()
        self.assertIn("note.txt", [e["name"] for e in d["entries"]])
        self.assertEqual(d["parent"], "/")

    def test_fs_mutations_cannot_touch_host(self):
        uc = self.uc
        r = uc.post("/api/fs", json={"action": "rmtree", "path": "/"})
        self.assertEqual(r.status_code, 400)
        marker = os.path.join(os.environ["RCONSOLE_DATA_DIR"], "host-marker")
        open(marker, "w").write("keep")
        uc.post("/api/fs", json={"action": "mkdir", "path": f"/..{marker}"})
        uc.post("/api/fs", json={"action": "rmtree", "path": f"/{os.path.relpath(marker, '/')}"})
        self.assertTrue(os.path.exists(marker))

    def test_vm_commands(self):
        out = self.run_cmd("vm", client=self.uc)["output"]
        self.assertIn("installed", out)
        self.assertIn("uid=0", self.run_cmd("vm run id", client=self.uc)["output"])
        self.assertEqual(self.run_cmd("vm shell", client=self.uc).get("cmd"), "@vm")
        self.assertIn("admins", self.run_cmd("vm mode host", client=self.uc)["output"])
        self.assertIn("--yes", self.run_cmd("vm reset", client=self.uc)["output"])

    def test_serve_static_from_vm(self):
        self.run_cmd("sudo id", client=self.uc)
        admin = rapp.app.test_client()
        self.login(client=admin)
        self.assertTrue(sandbox.install("ark", wait=True)[0])    # admin may use the VM too
        self.assertIn("mode: vm", self.run_cmd("vm mode vm", client=admin)["output"])
        self.run_cmd("mkdir -p /root/site", client=admin)
        self.run_cmd("echo '<h1>vm site</h1>' > /root/site/index.html", client=admin)
        self.run_cmd("cd /root/site", client=admin)
        out = self.run_cmd("sudo serve 18765", client=admin)["output"]
        self.assertIn("Hosting", out, out)
        try:
            import time
            time.sleep(0.3)
            body = admin.get("/site/18765/").data
            self.assertIn(b"vm site", body)
        finally:
            self.run_cmd("sudo kprocess", client=admin)
            self.run_cmd("vm mode host", client=admin)


if __name__ == "__main__":
    unittest.main()
