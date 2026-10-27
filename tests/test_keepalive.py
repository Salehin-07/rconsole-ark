"""Keep-alive: pings our public URL (and extras), reports honestly, admin controls."""
import http.server
import threading
import time
import unittest

import helpers  # noqa: F401

import app as rapp
import auth
import config
import keepalive


class Target(http.server.BaseHTTPRequestHandler):
    hits = []
    code = 200

    def do_GET(self):
        Target.hits.append((self.path, self.headers.get("User-Agent")))
        self.send_response(Target.code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class KeepAliveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        Target.hits.clear()
        Target.code = 200
        self._saved = (config.PUBLIC_URL, config.KEEPALIVE_EXTRA, config.KEEPALIVE, keepalive._enabled)
        keepalive._status.update(pings=0, failures=0, last_time=None, last_ok=None, last_error="")
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()
        auth.set_password("ark", "adminpass123")
        auth.create_user("alice", "longenough1")
        rapp.app.config["TESTING"] = True
        rapp.USER_THROTTLE._fails.clear()
        rapp.IP_THROTTLE._fails.clear()

    def tearDown(self):
        config.PUBLIC_URL, config.KEEPALIVE_EXTRA, config.KEEPALIVE, keepalive._enabled = self._saved
        keepalive.stop()

    def test_pings_the_public_healthz_not_localhost(self):
        config.PUBLIC_URL = self.url + "/"
        config.KEEPALIVE_EXTRA = ()
        self.assertEqual(keepalive.targets(), [self.url + "/healthz"])
        res = keepalive.ping_once()
        self.assertEqual([(ok, d) for _u, ok, d in res], [(True, "HTTP 200")])
        self.assertEqual(Target.hits[0][0], "/healthz")
        self.assertEqual(Target.hits[0][1], "rconsole-keepalive")
        st = keepalive.status()
        self.assertEqual((st["pings"], st["failures"], st["last_ok"]), (1, 0, True))

    def test_extra_urls_are_pinged_too(self):
        config.PUBLIC_URL = self.url
        config.KEEPALIVE_EXTRA = (self.url + "/other-site",)
        keepalive.ping_once()
        self.assertEqual(sorted(p for p, _ in Target.hits), ["/healthz", "/other-site"])

    def test_failures_are_recorded_not_raised(self):
        config.PUBLIC_URL = "http://127.0.0.1:1"            # nothing listens there
        res = keepalive.ping_once(timeout=2)
        self.assertFalse(res[0][1])
        st = keepalive.status()
        self.assertEqual(st["failures"], 1)
        self.assertIn("127.0.0.1:1", st["last_error"])
        Target.code = 503                                    # a 5xx counts as a failure
        config.PUBLIC_URL = self.url
        self.assertFalse(keepalive.ping_once()[0][1])

    def test_no_url_means_no_targets(self):
        config.PUBLIC_URL, config.KEEPALIVE_EXTRA = "", ()
        self.assertEqual(keepalive.targets(), [])
        self.assertEqual(keepalive.ping_once(), [])

    def test_background_thread_runs_and_stops(self):
        config.PUBLIC_URL, config.KEEPALIVE_EXTRA = self.url, ()
        config.KEEPALIVE = True
        self.assertTrue(keepalive.start_background())
        self.assertTrue(keepalive.start_background())        # idempotent
        self.assertTrue(keepalive.status()["running"])
        keepalive.stop()
        time.sleep(0.2)
        self.assertFalse(keepalive._thread.is_alive())

    def test_disabled_by_default_off_render_and_never_starts(self):
        config.KEEPALIVE = False
        keepalive._enabled = None
        self.assertFalse(keepalive.start_background())

    def test_platform_public_url_detection(self):
        d = config.detect_public_url
        self.assertEqual(d({}), "")
        self.assertEqual(d({"RENDER_EXTERNAL_URL": "https://a.onrender.com/"}), "https://a.onrender.com")
        self.assertEqual(d({"FLY_APP_NAME": "rc"}), "https://rc.fly.dev")
        self.assertEqual(d({"RAILWAY_PUBLIC_DOMAIN": "x.up.railway.app"}), "https://x.up.railway.app")
        self.assertEqual(d({"RCONSOLE_PUBLIC_URL": "https://mine.example/",
                            "RENDER_EXTERNAL_URL": "https://a.onrender.com"}), "https://mine.example")

    def test_command_status_now_and_permissions(self):
        config.PUBLIC_URL, config.KEEPALIVE_EXTRA = self.url, ()
        c = rapp.app.test_client()
        c.post("/login", data={"username": "alice", "password": "longenough1"})
        run = lambda cmd, cl=c: cl.post("/api/run", json={"cmd": cmd, "tab": "main"}).get_json()["output"]  # noqa: E731
        status = run("keepalive")
        self.assertIn("Keep-alive", status)
        self.assertIn(self.url, status)
        self.assertIn("✓", run("keepalive now"))
        self.assertIn("only admins", run("keepalive off"))                    # users can look, not touch
        admin = rapp.app.test_client()
        admin.post("/login", data={"username": "ark", "password": "adminpass123"})
        self.assertIn("keepalive: off", run("keepalive off", admin))
        self.assertFalse(keepalive.is_enabled())
        self.assertIn("enabled   : no", run("keepalive", admin))
        self.assertIn("keepalive: on", run("keepalive on", admin))
        self.assertTrue(keepalive.is_enabled())
        keepalive.stop()
        self.assertIn("usage", run("keepalive wat"))

    def test_status_without_a_public_url_explains_what_to_set(self):
        config.PUBLIC_URL = ""
        c = rapp.app.test_client()
        c.post("/login", data={"username": "alice", "password": "longenough1"})
        out = c.post("/api/run", json={"cmd": "keepalive", "tab": "main"}).get_json()["output"]
        self.assertIn("RCONSOLE_PUBLIC_URL", out)


if __name__ == "__main__":
    unittest.main()
