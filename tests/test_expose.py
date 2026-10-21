"""`expose <port> <name>`: registry rules, commands, and the proxy end to end."""
import http.server
import json
import os
import threading
import unittest

import helpers  # noqa: F401

import app as rapp
import auth
import exposure
import state


class Upstream(http.server.BaseHTTPRequestHandler):
    """A tiny app: echoes what it receives, plus a few special pages."""
    port = 0

    def _send(self, code, body, ctype="application/json", extra=()):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _echo(self, body=b""):
        self._send(200, json.dumps({
            "method": self.command, "path": self.path, "body": body.decode(),
            "prefix": self.headers.get("X-Forwarded-Prefix"),
            "fwd_host": self.headers.get("X-Forwarded-Host"),
            "cookie": self.headers.get("Cookie", ""),
        }))

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/redir":
            return self._send(302, "", extra=[("Location", "/login")])
        if p == "/abs":
            return self._send(302, "", extra=[("Location", f"http://127.0.0.1:{Upstream.port}/dash")])
        if p == "/page":
            return self._send(200, (
                '<link href="/static/a.css" rel="stylesheet"><a href="/">home</a>'
                '<img src=\'/img/x.png\'><script src="//cdn.example/x.js"></script>'
                '<a href="/myapp/already">ok</a><a href="rel/path">rel</a>'
                '<form action="/submit"></form>'), "text/html; charset=utf-8")
        if p == "/style.css":
            return self._send(200, "body{background:url(/img/bg.png)} a{b:url('/x.png')}", "text/css")
        if p == "/data":
            return self._send(200, '{"href": "/not/rewritten"}')
        self._echo()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._echo(self.rfile.read(n))

    def log_message(self, *a):
        pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]
        Upstream.port = cls.port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        exposure.FILE.unlink(missing_ok=True)
        exposure._cache.update(sig=None, data={})
        auth.seed_defaults()
        auth.set_password("ark", "adminpass123")
        auth.create_user("alice", "longenough1")
        auth.create_user("bob", "longenough1")
        state.ACTIVE_HOSTS.clear()
        rapp.USER_THROTTLE._fails.clear()
        rapp.IP_THROTTLE._fails.clear()
        rapp.app.config["TESTING"] = True

    def client(self, user=None, pw=None):
        c = rapp.app.test_client()
        if user:
            self.assertEqual(c.post("/login", data={"username": user, "password": pw}).status_code, 302)
        return c

    def alice(self):
        return self.client("alice", "longenough1")

    def admin(self):
        return self.client("ark", "adminpass123")

    def run_cmd(self, c, cmd):
        return c.post("/api/run", json={"cmd": cmd, "tab": "main"}).get_json()["output"]


class RegistryTests(Base):
    def test_name_rules(self):
        for bad in ("", "-a", "a-", "A b", "a_b", "x" * 33, "8000", "login", "api", "static", "ws",
                    "healthz", "console", "a/b", ".."):
            self.assertIsNotNone(exposure.validate_name(bad), bad)
        for good in ("myapp", "my-app", "a", "a1", "x" * 32, "app-2"):
            self.assertIsNone(exposure.validate_name(good), good)

    def test_port_rules(self):
        self.assertIsNotNone(exposure.validate_port(80))                   # privileged
        self.assertIsNotNone(exposure.validate_port(70000))
        self.assertIsNotNone(exposure.validate_port("8000"))
        self.assertIsNotNone(exposure.validate_port(exposure.server_port()))   # the console itself
        self.assertIsNone(exposure.validate_port(8000))

    def test_ownership_update_and_removal(self):
        self.assertEqual(exposure.add("Site", 8001, "alice"), (True, "created"))   # names are lowercased
        self.assertEqual(exposure.get("SITE")["port"], 8001)
        self.assertEqual(exposure.add("site", 8002, "alice"), (True, "updated"))
        self.assertEqual(exposure.get("site")["port"], 8002)
        ok, msg = exposure.add("site", 8003, "bob")
        self.assertFalse(ok)
        self.assertIn("already used", msg)
        self.assertFalse(exposure.remove("site", "bob")[0])
        self.assertTrue(exposure.remove("site", "ark", is_admin=True)[0])      # admins may
        self.assertIsNone(exposure.get("site"))

    def test_per_user_limit(self):
        for i in range(exposure.MAX_PER_USER):
            self.assertTrue(exposure.add(f"app{i}x", 9000 + i, "alice")[0])
        ok, msg = exposure.add("one-more", 9500, "alice")
        self.assertFalse(ok)
        self.assertIn("remove one", msg)
        self.assertTrue(exposure.add("app0x", 9999, "alice")[0])               # updating is fine
        self.assertTrue(exposure.add("bobs", 9500, "bob")[0])

    def test_persists_and_is_private_on_disk(self):
        exposure.add("keep", 8010, "alice", public=True, rewrite=True)
        exposure._cache.update(sig=None, data={})                              # force a reload
        e = exposure.get("keep")
        self.assertEqual((e["port"], e["public"], e["rewrite"], e["owner"]), (8010, True, True, "alice"))
        self.assertEqual(exposure.FILE.stat().st_mode & 0o777, 0o600)

    def test_owner_cleanup_and_rename(self):
        exposure.add("a1", 8011, "alice")
        exposure.add("a2", 8012, "alice")
        exposure.add("b1", 8013, "bob")
        exposure.rename_owner("alice", "alicia")
        self.assertEqual([e["name"] for e in exposure.list_for("alicia")], ["a1", "a2"])
        self.assertEqual(exposure.remove_owner("alicia"), 2)
        self.assertEqual([e["name"] for e in exposure.list_all()], ["b1"])
        exposure.clear()
        self.assertEqual(exposure.list_all(), [])

    def test_corrupt_registry_means_nothing_exposed(self):
        exposure.FILE.write_text("{nope")
        exposure._cache.update(sig=None, data={})
        self.assertEqual(exposure.list_all(), [])
        self.assertTrue(exposure.add("fresh", 8020, "alice")[0])               # and it heals


class CommandTests(Base):
    def test_expose_list_and_remove(self):
        c = self.alice()
        out = self.run_cmd(c, f"expose {self.port} myapp")
        self.assertIn("Exposed port", out)
        self.assertIn("/myapp/", out)
        self.assertIn("login required", out)
        self.assertNotIn("nothing is listening", out)                          # our upstream is up
        self.assertIn("nothing is listening", self.run_cmd(c, "expose 45999 later"))
        listing = self.run_cmd(c, "expose list")
        self.assertIn("/myapp/ -> :%d [up]" % self.port, listing)
        self.assertIn("/later/ -> :45999 [down]", listing)
        self.assertIn("http://localhost/myapp/", listing)                      # full URL shown
        self.assertEqual(self.run_cmd(c, "expose").count("/myapp/"), 2)        # bare `expose` lists too
        self.assertIn("myapp: removed", self.run_cmd(c, "expose rm myapp"))
        self.assertIn("no exposure called", self.run_cmd(c, "unexpose myapp"))
        self.assertIn("Removed 1", self.run_cmd(c, "expose rm all"))
        self.assertIn("Nothing exposed", self.run_cmd(c, "expose list"))

    def test_input_errors_are_explained(self):
        c = self.alice()
        self.assertIn("not a port number", self.run_cmd(c, "expose abc myapp"))
        self.assertIn("between 1024", self.run_cmd(c, "expose 80 myapp"))
        self.assertIn("reserved", self.run_cmd(c, "expose 8000 login"))
        self.assertIn("only digits", self.run_cmd(c, "expose 8000 1234"))
        self.assertIn("unknown option", self.run_cmd(c, "expose 8000 x --wat"))
        self.assertIn("usage", self.run_cmd(c, "expose 8000"))
        self.assertIn("console itself", self.run_cmd(c, f"expose {exposure.server_port()} me"))
        self.assertIn("expose <port> <name>", self.run_cmd(c, "expose --help"))

    def test_names_are_global_and_ownership_is_enforced(self):
        a, b = self.alice(), self.client("bob", "longenough1")
        self.run_cmd(a, f"expose {self.port} shared")
        self.assertIn("already used by someone else", self.run_cmd(b, "expose 8100 shared"))
        self.assertIn("belongs to alice", self.run_cmd(b, "expose rm shared"))
        self.assertIn("Nothing exposed", self.run_cmd(b, "expose list"))       # users see only their own
        adm = self.admin()
        self.assertIn("/shared/", self.run_cmd(adm, "expose list"))            # admins see everything
        self.assertIn("shared: removed", self.run_cmd(adm, "expose rm shared"))

    def test_public_is_admin_only(self):
        self.assertIn("admins only", self.run_cmd(self.alice(), f"expose {self.port} pub --public"))
        out = self.run_cmd(self.admin(), f"expose {self.port} pub --public --rewrite")
        self.assertIn("anyone, no login", out)
        self.assertNotIn("re-run with --rewrite", out)
        e = exposure.get("pub")
        self.assertTrue(e["public"] and e["rewrite"])


class ProxyTests(Base):
    def expose(self, name="myapp", user="alice", flags=""):
        c = self.client(user, "longenough1" if user != "ark" else "adminpass123")
        out = self.run_cmd(c, f"expose {self.port} {name} {flags}".strip())
        self.assertIn("Exposed", out)
        return c

    def test_private_exposure_needs_login(self):
        self.expose()
        anon = self.client()
        r = anon.get("/myapp/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_request_reaches_the_app_with_prefix_headers(self):
        c = self.expose()
        r = c.get("/myapp/", headers={"X-Forwarded-Host": "console.example.com"})
        j = r.get_json()
        self.assertEqual((r.status_code, j["path"], j["prefix"]), (200, "/", "/myapp"))
        self.assertEqual(j["fwd_host"], "localhost")
        j = c.get("/myapp/api/items?tag=a&tag=b&q=1").get_json()
        self.assertEqual(j["path"], "/api/items?tag=a&tag=b&q=1")              # duplicates preserved
        j = c.post("/myapp/submit", data="hello", headers={"Origin": "https://other.example"}).get_json()
        self.assertEqual((j["method"], j["path"], j["body"]), ("POST", "/submit", "hello"))
        self.assertEqual(c.get("/myapp/8000/x").get_json()["path"], "/8000/x")   # digits-first path

    def test_console_session_cookie_never_reaches_the_app(self):
        c = self.expose()
        c.set_cookie("mycookie", "1")
        cookie = c.get("/myapp/").get_json()["cookie"]
        self.assertIn("mycookie=1", cookie)
        self.assertNotIn("rconsole_session", cookie)

    def test_bare_name_redirects_to_the_slash_form(self):
        c = self.expose()
        r = c.get("/myapp?x=1")
        self.assertEqual(r.status_code, 308)
        self.assertTrue(r.headers["Location"].endswith("/myapp/?x=1"))

    def test_redirects_stay_inside_the_prefix(self):
        c = self.expose()
        self.assertTrue(c.get("/myapp/redir").headers["Location"].endswith("/myapp/login"))
        self.assertTrue(c.get("/myapp/abs").headers["Location"].endswith("/myapp/dash"))

    def test_rewrite_fixes_root_relative_links_only_when_asked(self):
        c = self.expose()
        plain = c.get("/myapp/page").get_data(as_text=True)
        self.assertIn('href="/static/a.css"', plain)                           # untouched by default
        c.post("/api/run", json={"cmd": f"expose {self.port} myapp --rewrite", "tab": "main"})
        html = c.get("/myapp/page").get_data(as_text=True)
        self.assertIn('href="/myapp/static/a.css"', html)
        self.assertIn('<a href="/myapp/">home</a>', html)
        self.assertIn("src='/myapp/img/x.png'", html)
        self.assertIn('action="/myapp/submit"', html)
        self.assertIn('src="//cdn.example/x.js"', html)                        # protocol-relative: left alone
        self.assertIn('href="/myapp/already"', html)                           # not double-prefixed
        self.assertNotIn("/myapp/myapp", html)
        self.assertIn('href="rel/path"', html)                                 # relative: left alone
        css = c.get("/myapp/style.css").get_data(as_text=True)
        self.assertIn("url(/myapp/img/bg.png)", css)
        self.assertIn("url('/myapp/x.png')", css)
        self.assertIn('"/not/rewritten"', c.get("/myapp/data").get_data(as_text=True))   # JSON untouched

    def test_public_exposure_needs_no_login_but_stays_cookie_clean(self):
        self.expose("pub", user="ark", flags="--public")
        self.expose("priv")
        anon = self.client()
        r = anon.get("/pub/hello")
        self.assertEqual((r.status_code, r.get_json()["path"]), (200, "/hello"))
        self.assertEqual(anon.get("/priv/").status_code, 302)                  # private stays private
        self.assertEqual(anon.get("/nosuchthing/").status_code, 302)           # unknown: login first
        self.assertEqual(anon.post("/pub/hook", data="x").get_json()["body"], "x")   # webhooks work

    def test_hosted_app_paths_win_over_a_public_exposure_with_the_same_name(self):
        self.expose("site", user="ark", flags="--public")
        state.ACTIVE_HOSTS[("site", 8000)] = {"url": "", "proc": None, "sid": "ark"}
        anon = self.client()
        self.assertEqual(anon.get("/site/8000/").status_code, 302)             # hosted app: login required
        self.assertEqual(anon.get("/site/8001/x").get_json()["path"], "/8001/x")   # exposure path

    def test_unknown_name_and_console_paths_unaffected(self):
        c = self.alice()
        self.assertEqual(c.get("/nothing-here/").status_code, 404)
        self.assertEqual(c.get("/nothing-here").status_code, 404)
        self.assertTrue(c.get("/healthz").get_json()["ok"])
        self.assertEqual(self.client().get("/login").status_code, 200)
        self.assertEqual(c.get("/api/tabs").status_code, 200)

    def test_app_down_gives_502_not_a_crash(self):
        c = self.alice()
        self.run_cmd(c, "expose 45998 deadapp")
        self.assertEqual(c.get("/deadapp/").status_code, 502)

    def test_removed_exposure_stops_serving(self):
        c = self.expose()
        self.assertEqual(c.get("/myapp/").status_code, 200)
        self.run_cmd(c, "expose rm myapp")
        self.assertEqual(c.get("/myapp/").status_code, 404)

    def test_user_lifecycle_cleans_up(self):
        self.expose()
        import cmd_users
        cmd_users._migrate_cleanup("alice")
        self.assertIsNone(exposure.get("myapp"))


if __name__ == "__main__":
    os.environ.setdefault("RCONSOLE_TEST", "1")
    unittest.main()
