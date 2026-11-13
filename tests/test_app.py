"""HTTP-level tests of the Flask app (auth, CSRF, throttling, file APIs, proxy)."""
import http.server
import json
import os
import io
import shutil
import threading
import unittest
import zipfile

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
        self.assertTrue(self.c.get("/healthz").get_json()["ok"])

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

    def test_front_end_has_phone_settings_menu_and_close_button(self):
        self.login()
        page = self.c.get("/console").get_data(as_text=True)
        for needle in ('id="settingsbtn"', 'id="settingsmenu"', 'id="sm-theme-slot"',
                       'id="sm-target-slot"', 'id="themebtn"', 'id="targetsel"'):
            self.assertIn(needle, page)
        js = self.c.get("/static/console.js").get_data(as_text=True)
        self.assertIn("term-close", js)             # ✕ on every interactive window
        self.assertIn("placeControls", js)          # moves theme + system into the menu on phones
        self.assertIn("(max-width: 640px)", js)
        css = self.c.get("/static/style.css").get_data(as_text=True)
        self.assertIn(".term-close", css)
        self.assertIn("#settingsmenu", css)

    def test_editor_assets_are_served_and_wired(self):
        self.login()
        page = self.c.get("/console").get_data(as_text=True)
        # editor-core.js has to load before console.js (both are deferred, so order = document order)
        self.assertIn("/static/editor-core.js", page)
        self.assertLess(page.index("/static/editor-core.js"), page.index("/static/console.js"))
        for needle in ('id="editor-find"', 'id="ef-query"', 'id="editor-keys"', 'id="editor-statusbar"',
                       'id="es-fs-up"', 'id="es-fs-down"', 'id="es-indent"', 'id="editor-curline"',
                       'id="files-sel"', 'id="fsel-compress"', 'id="fsel-extract"', 'id="fsel-props"',
                       'id="fsel-delete"', 'id="fsel-check"', 'id="files-dialog"'):
            self.assertIn(needle, page)
        # no always-visible Select all / All buttons: the checkbox lives in the selection bar
        self.assertNotIn('id="files-selall"', page)
        self.assertNotIn('id="fsel-all"', page)
        # the font buttons live in the editor's top bar (next to Save / Close), not in the status bar
        bar = page[page.index('id="editor-bar"'):page.index('id="editor-find"')]
        self.assertIn('id="es-fs-up"', bar)
        self.assertIn('id="es-fs-down"', bar)
        self.assertIn('id="editor-save"', bar)
        self.assertNotIn("A+", page[page.index('id="editor-statusbar"'):])
        self.assertLess(page.index("/static/files-core.js"), page.index("/static/console.js"))
        self.assertEqual(self.c.get("/static/files-core.js").status_code, 200)
        core = self.c.get("/static/editor-core.js")
        self.assertEqual(core.status_code, 200)
        text = core.get_data(as_text=True)
        for fn in ("typeChar", "toggleComment", "detectIndent", "findAll"):
            self.assertIn(fn, text)
        js = self.c.get("/static/console.js").get_data(as_text=True)
        self.assertIn("beforeinput", js)
        self.assertIn("rconsole.editor.fontsize", js)
        css = self.c.get("/static/style.css").get_data(as_text=True)
        self.assertIn("--editor-fs", css)
        self.assertIn("#editor-find", css)

    def test_security_headers(self):
        h = self.c.get("/healthz").headers
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["X-Frame-Options"], "SAMEORIGIN")

    def test_default_password_no_longer_forces_cpass(self):
        auth.USERS_FILE.unlink()
        auth._cache.update(sig=None, users={})
        auth.seed_defaults()                      # ark / ark
        self.assertEqual(self.login("ark", "ark").status_code, 302)
        out = self.run_cmd("ls")["output"]
        self.assertNotIn("sudo cpass", out)       # the console works straight away
        self.assertEqual(self.c.get("/api/dir?path=/").status_code, 200)

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

    def test_browser_attested_same_origin_is_accepted(self):
        """Regression: Origin:null (from Referrer-Policy no-referrer) blocked login."""
        r = self.c.post("/login", data={"username": "ark", "password": "adminpass123"},
                        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin",
                                 "Host": "rconsole-ark.onrender.com"})
        self.assertEqual(r.status_code, 302)
        r = self.c.post("/login", data={"username": "ark", "password": "adminpass123"},
                        headers={"Sec-Fetch-Site": "same-site"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/login", data={"username": "ark", "password": "adminpass123"},
                        headers={"Origin": "https://localhost", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)

    def test_referrer_policy_lets_browsers_send_origin(self):
        self.assertEqual(self.c.get("/healthz").headers["Referrer-Policy"], "same-origin")

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

    def test_platform_detection(self):
        import config
        d = config.detect_platform
        self.assertEqual(d({})[0], None)
        n, hosts, _ = d({"RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": "App.onrender.com"})
        self.assertEqual((n, hosts), ("render", ("app.onrender.com",)))
        n, hosts, vol = d({"RAILWAY_PROJECT_ID": "x", "RAILWAY_PUBLIC_DOMAIN": "a.up.railway.app",
                           "RAILWAY_VOLUME_MOUNT_PATH": "/data"})
        self.assertEqual((n, hosts, vol), ("railway", ("a.up.railway.app",), "/data"))
        self.assertEqual(d({"FLY_APP_NAME": "rc"}), ("fly", ("rc.fly.dev",), None))
        self.assertEqual(d({"DYNO": "web.1"})[0], "heroku")
        self.assertEqual(d({"K_SERVICE": "svc"})[0], "cloudrun")
        self.assertEqual(d({"KOYEB_APP_NAME": "k", "KOYEB_PUBLIC_DOMAIN": "k.koyeb.app"})[1],
                         ("k.koyeb.app",))

    def test_platform_hostname_is_accepted(self):
        import config
        old = config.PLATFORM_HOSTS
        config.PLATFORM_HOSTS = ("rc.fly.dev",)
        try:
            r = self.c.post("/login", data={"username": "ark", "password": "adminpass123"},
                            headers={"Origin": "https://rc.fly.dev", "Host": "10.0.0.1:5000"})
            self.assertEqual(r.status_code, 302)
        finally:
            config.PLATFORM_HOSTS = old

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
        res = self.run_cmd("cat /etc/passwd", client=c)
        self.assertNotIn("root:", res["output"])
        # No system yet: the reply is a picker offering to install one.
        self.assertTrue(res["choice"]["options"])
        self.assertTrue(all(o["cmds"][0].startswith("vm install ") for o in res["choice"]["options"]))
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
        p = os.path.join(os.environ["RCONSOLE_DATA_DIR"], "big.txt")
        with open(p, "wb") as f:
            f.truncate(rapp.MAX_EDIT_BYTES + 1)
        self.assertEqual(self.c.get("/api/file", query_string={"path": p}).status_code, 413)

    def test_editor_never_opens_binary_or_compressed_files(self):
        import gzip
        import zipfile
        self.login()
        d = os.environ["RCONSOLE_DATA_DIR"]
        gz, zp, blob, png, txt = (os.path.join(d, n) for n in ("a.txt.gz", "b.zip", "noext", "pic.png", "ok.py"))
        with gzip.open(gz, "wb") as f:
            f.write(b"hello")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("x", "y")
        with open(blob, "wb") as f:                       # no telling extension: caught by its content
            f.write(b"\x00\x01\x02binary\x00")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n")
        with open(txt, "w", encoding="utf-8") as f:
            f.write("print('héllo')\n")
        for p in (gz, zp, blob, png):
            r = self.c.get("/api/file", query_string={"path": p})
            self.assertEqual(r.status_code, 415, p)
            self.assertTrue(r.get_json()["blocked"])
            before = open(p, "rb").read()
            r = self.c.post("/api/file", json={"path": p, "content": "overwritten"})   # must not corrupt it
            self.assertEqual(r.status_code, 415, p)
            self.assertEqual(open(p, "rb").read(), before)
        self.assertEqual(self.c.get("/api/file", query_string={"path": gz}).get_json()["kind"], "archive")
        self.assertEqual(self.c.get("/api/file", query_string={"path": txt}).status_code, 200)
        self.assertIn("compressed", self.run_cmd(f"edit {gz}")["output"])
        self.assertIn("editor", self.run_cmd(f"edit {txt}"))

    def test_pdf_is_served_inline_for_the_viewer(self):
        self.login()
        p = os.path.join(os.environ["RCONSOLE_DATA_DIR"], "doc.pdf")
        with open(p, "wb") as f:
            f.write(b"%PDF-1.4\n%%EOF\n")
        r = self.c.get("/api/media", query_string={"path": p})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertNotIn("sandbox", r.headers["Content-Security-Policy"])      # a sandboxed PDF will not render
        self.assertEqual(r.headers["Content-Disposition"], "inline")

    def test_kprocess_refuses_server_pid(self):
        self.login()
        out = self.run_cmd(f"sudo kprocess {os.getpid()}")["output"]
        self.assertIn("refusing", out)


class FileTransferTests(Base):
    """The file browser's download (file / folder-as-zip) and upload (file / folder) endpoints."""

    def setUp(self):
        super().setUp()
        self.login()
        self.root = os.path.join(os.environ["RCONSOLE_DATA_DIR"], "xfer")
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def upload(self, name, data=b"hello", folder=None, rel=None, overwrite=False, client=None):
        form = {"dir": folder if folder is not None else self.root, "file": (io.BytesIO(data), name)}
        if rel is not None:
            form["rel"] = rel
        if overwrite:
            form["overwrite"] = "1"
        return (client or self.c).post("/api/upload", data=form, content_type="multipart/form-data")

    def test_download_file(self):
        p = os.path.join(self.root, "n\u00e4me \"q\".txt")
        open(p, "wb").write(b"payload")
        r = self.c.get("/api/download", query_string={"path": p})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"payload")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn("filename*=UTF-8''n%C3%A4me", r.headers["Content-Disposition"])
        self.assertEqual(r.mimetype, "application/octet-stream")
        self.assertNotIn("Content-Encoding", r.headers)           # never run through the compressor

    def test_download_needs_login_and_a_real_file(self):
        anon = rapp.app.test_client()
        self.assertEqual(anon.get("/api/download", query_string={"path": self.root}).status_code, 401)
        self.assertEqual(self.c.get("/api/download").status_code, 400)
        self.assertEqual(self.c.get("/api/download", query_string={"path": self.root + "/nope"}).status_code, 404)

    def test_download_folder_as_zip_skips_symlinks(self):
        os.makedirs(os.path.join(self.root, "site", "sub", "empty"))
        open(os.path.join(self.root, "site", "index.html"), "w").write("<h1>hi</h1>")
        open(os.path.join(self.root, "site", "sub", "a.txt"), "w").write("a")
        secret = os.path.join(self.root, "secret.txt")
        open(secret, "w").write("top secret")
        os.symlink(secret, os.path.join(self.root, "site", "leak.txt"))
        os.symlink(self.root, os.path.join(self.root, "site", "loop"))
        r = self.c.get("/api/download", query_string={"path": os.path.join(self.root, "site")})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/zip")
        self.assertIn("site.zip", r.headers["Content-Disposition"])
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        self.assertIsNone(zf.testzip())
        self.assertEqual(sorted(zf.namelist()),
                         ["site/index.html", "site/sub/a.txt", "site/sub/empty/"])
        self.assertEqual(zf.read("site/index.html"), b"<h1>hi</h1>")

    def test_upload_single_file(self):
        r = self.upload("one.txt", b"first")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(open(os.path.join(self.root, "one.txt"), "rb").read(), b"first")
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".")], [])   # no temp file left

    def test_upload_existing_needs_overwrite(self):
        self.upload("one.txt", b"first")
        r = self.upload("one.txt", b"second")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["exists"])
        self.assertEqual(open(os.path.join(self.root, "one.txt"), "rb").read(), b"first")
        self.assertEqual(self.upload("one.txt", b"second", overwrite=True).status_code, 200)
        self.assertEqual(open(os.path.join(self.root, "one.txt"), "rb").read(), b"second")

    def test_upload_folder_keeps_layout(self):
        r = self.upload("c.txt", b"deep", rel="proj/src/c.txt")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(open(os.path.join(self.root, "proj", "src", "c.txt"), "rb").read(), b"deep")

    def test_upload_rejects_unsafe_names(self):
        for rel in ("../evil.txt", "a/../../evil.txt", "/abs/evil.txt", "a//b.txt", "a\\b.txt", "./x", "a/."):
            r = self.upload("x", rel=rel)
            self.assertEqual(r.status_code, 400, rel)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.root), "evil.txt")))

    def test_upload_errors(self):
        anon = rapp.app.test_client()
        self.assertEqual(self.upload("x", client=anon).status_code, 401)
        self.assertEqual(self.upload("x", folder=self.root + "/missing").status_code, 400)
        self.assertEqual(self.c.post("/api/upload", data={"dir": self.root}).status_code, 400)
        os.makedirs(os.path.join(self.root, "adir"))
        self.assertEqual(self.upload("x", rel="adir", overwrite=True).status_code, 409)   # a folder is in the way

    def test_upload_replaces_a_symlink_instead_of_writing_through_it(self):
        outside = os.path.join(self.root, "outside.txt")
        open(outside, "w").write("untouched")
        os.makedirs(os.path.join(self.root, "d"))
        os.symlink(outside, os.path.join(self.root, "d", "link.txt"))
        r = self.upload("link.txt", b"new", folder=os.path.join(self.root, "d"), overwrite=True)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(open(outside).read(), "untouched")
        self.assertFalse(os.path.islink(os.path.join(self.root, "d", "link.txt")))

    def upload_part(self, name, data, offset, total, uid="ab12cd34ef56", overwrite=False):
        form = {"dir": self.root, "file": (io.BytesIO(data), name), "upload_id": uid,
                "offset": str(offset), "total": str(total)}
        if overwrite:
            form["overwrite"] = "1"
        return self.c.post("/api/upload", data=form, content_type="multipart/form-data")

    def test_upload_in_parts_assembles_a_file_bigger_than_one_request(self):
        parts = [b"A" * 1000, b"B" * 1000, b"C" * 500]
        total, off = 2500, 0
        for i, part in enumerate(parts):
            r = self.upload_part("big.bin", part, off, total)
            self.assertEqual(r.status_code, 200, r.data)
            self.assertEqual(r.get_json().get("partial", False), i < 2)
            off += len(part)
        self.assertEqual(open(os.path.join(self.root, "big.bin"), "rb").read(), b"".join(parts))
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".")], [])

    def test_upload_part_retry_is_idempotent(self):
        self.assertEqual(self.upload_part("r.bin", b"1" * 100, 0, 150).status_code, 200)
        self.assertEqual(self.upload_part("r.bin", b"2" * 50, 100, 150).status_code, 200)   # completes it
        self.assertEqual(self.upload_part("r2.bin", b"1" * 100, 0, 150, uid="1111111122222222").status_code, 200)
        self.assertEqual(self.upload_part("r2.bin", b"x" * 20, 100, 150, uid="1111111122222222").status_code, 200)
        r = self.upload_part("r2.bin", b"2" * 50, 100, 150, uid="1111111122222222")          # retried part
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(open(os.path.join(self.root, "r2.bin"), "rb").read(), b"1" * 100 + b"2" * 50)

    def test_upload_parts_out_of_order_and_over_limit_are_refused(self):
        r = self.upload_part("o.bin", b"x" * 10, 50, 100, uid="aaaaaaaabbbbbbbb")
        self.assertEqual(r.status_code, 409)
        import config
        old = config.MAX_UPLOAD_MB
        config.MAX_UPLOAD_MB = 1
        try:
            r = self.upload_part("huge.bin", b"x", 0, 5 * 1024 * 1024, uid="ccccccccdddddddd")
            self.assertEqual(r.status_code, 413)
        finally:
            config.MAX_UPLOAD_MB = old
        self.assertEqual(self.upload_part("bad", b"x", 0, 10, uid="../evil").status_code, 400)

    def test_upload_part_existing_file_needs_overwrite(self):
        self.upload("e.bin", b"old")
        self.assertEqual(self.upload_part("e.bin", b"n" * 5, 0, 10, uid="eeeeeeeeffffffff").status_code, 409)
        self.assertEqual(open(os.path.join(self.root, "e.bin"), "rb").read(), b"old")

    def test_upload_size_limit(self):
        import config
        old = config.MAX_UPLOAD_MB
        config.MAX_UPLOAD_MB = 1
        try:
            r = self.upload("big.bin", b"x" * (1024 * 1024 + 10))
            self.assertEqual(r.status_code, 413)
            self.assertFalse(os.path.exists(os.path.join(self.root, "big.bin")))
            self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".")], [])
        finally:
            config.MAX_UPLOAD_MB = old

    def test_upload_is_csrf_protected(self):
        r = self.c.post("/api/upload", data={"dir": self.root, "file": (io.BytesIO(b"x"), "x.txt")},
                        content_type="multipart/form-data", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(os.path.exists(os.path.join(self.root, "x.txt")))


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
        self.assertIn("Your systems (1)", out)
        self.assertIn("uid=0", self.run_cmd("vm run id", client=self.uc)["output"])
        self.assertEqual(self.run_cmd("vm shell", client=self.uc).get("cmd"), "@vm:alpine")
        self.assertIn("admins", self.run_cmd("vm mode host", client=self.uc)["output"])
        self.assertIn("--yes", self.run_cmd("vm reset", client=self.uc)["output"])

    def test_download_and_upload_are_confined_to_the_vm(self):
        uc = self.uc
        r = uc.post("/api/upload", data={"dir": "/root", "rel": "up/one.txt", "file": (io.BytesIO(b"vm"), "one.txt")},
                    content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.run_cmd("cat /root/up/one.txt", client=uc)["output"].strip(), "vm")   # visible in the VM
        r = uc.get("/api/download", query_string={"path": "/root/up/one.txt"})
        self.assertEqual(r.data, b"vm")
        r = uc.post("/api/upload", data={"dir": "/root", "rel": "../../escape.txt", "file": (io.BytesIO(b"x"), "x")},
                    content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        # a symlink inside a downloaded folder must not pull in a host file
        home = sandbox.home_path("alice", sandbox.active_vm("alice") or sandbox.installed_vms("alice")[0])
        os.symlink("/etc/hostname", os.path.join(home, "up", "leak"))
        z = zipfile.ZipFile(io.BytesIO(uc.get("/api/download", query_string={"path": "/root/up"}).data))
        self.assertEqual(z.namelist(), ["up/one.txt"])


if __name__ == "__main__":
    unittest.main()
