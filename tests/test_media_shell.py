"""v1.4.2: the admin-only host `shell` command and the file browser's photo / video viewer."""
import os
import re
import unittest
import unittest.mock

import helpers  # noqa: F401  (puts the project on sys.path)
import sandbox
from test_app import Base, rapp
from test_fileman_api import FileManagerBase


class ShellCommandTests(Base):
    def test_admin_gets_an_interactive_host_window(self):
        self.login()
        r = self.run_cmd("shell")
        self.assertEqual(r.get("pty"), True, r)
        self.assertEqual(r["cmd"], "@host")                    # pinned to the host, whatever the tab's target
        self.assertTrue(self.run_cmd("sudo shell").get("pty"))   # sudo is accepted, not needed

    def test_the_launch_is_the_host_shell_with_no_command(self):
        self.login()
        sp = sandbox.spec("ark", "@host", os.getcwd(), tty=True)
        self.assertEqual(sp.mode, sandbox.HOST)
        self.assertEqual(len(sp.argv), 1)                        # a bare shell: interactive

    def test_normal_users_are_refused(self):
        self.login("alice", "longenough1")
        for cmd in ("shell", "sudo shell"):
            r = self.run_cmd(cmd)
            self.assertFalse(r.get("pty"), r)
            self.assertIn("denied" if cmd == "shell" else "sudoers", r["output"])

    def test_refused_even_when_users_may_use_the_host(self):
        self.login("alice", "longenough1")
        with unittest.mock.patch.object(sandbox.config, "ALLOW_USER_HOST", True):
            r = self.run_cmd("shell")
        self.assertFalse(r.get("pty"), r)

    def test_takes_no_arguments(self):
        self.login()
        r = self.run_cmd("shell ls")
        self.assertFalse(r.get("pty"))
        self.assertIn("usage", r["output"])

    def test_is_in_help_and_is_not_a_sudo_command(self):
        import commands
        self.assertIn("shell", commands.COMMANDS)
        self.assertNotIn("shell", commands.PROTECTED)
        self.login()
        self.assertIn("shell", self.run_cmd("help")["output"])


class MediaEndpointTests(FileManagerBase):
    PNG = (b"\x89PNG\r\n\x1a\n" + b"\0" * 40)

    def get(self, path, **kw):
        return self.c.get("/api/media", query_string={"path": path}, **kw)

    def test_needs_a_login(self):
        f = self.write("a.png", self.PNG)
        self.assertEqual(rapp.app.test_client().get("/api/media", query_string={"path": f}).status_code, 401)

    def test_image_is_served_inline_with_a_safe_policy(self):
        f = self.write("pic.PNG", self.PNG)
        r = self.get(f)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/png")
        self.assertEqual(r.headers["Content-Disposition"], "inline")
        self.assertEqual(r.data, self.PNG)
        self.assertIn("sandbox", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_svg_cannot_run_scripts(self):
        f = self.write("x.svg", "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
        r = self.get(f)
        self.assertEqual(r.mimetype, "image/svg+xml")
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("sandbox", csp)
        self.assertIn("default-src 'none'", csp)               # no script-src: scripts are off

    def test_video_supports_range_requests_so_it_can_seek(self):
        data = bytes(range(256)) * 40
        f = self.write("clip.mp4", data)
        r = self.get(f, headers={"Range": "bytes=100-199"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.mimetype, "video/mp4")
        self.assertEqual(r.data, data[100:200])
        self.assertEqual(r.headers["Content-Range"], f"bytes 100-199/{len(data)}")
        self.assertEqual(self.get(f).headers.get("Accept-Ranges"), "bytes")

    def test_only_images_and_videos(self):
        for name in ("notes.txt", "page.html", "run.sh", "data.json", "noext"):
            f = self.write(name, "hello")
            self.assertEqual(self.get(f).status_code, 415, name)

    def test_errors(self):
        self.assertEqual(self.c.get("/api/media").status_code, 400)
        self.assertEqual(self.get(self.p("missing.png")).status_code, 404)
        os.makedirs(self.p("folder.png"))
        self.assertEqual(self.get(self.p("folder.png")).status_code, 404)


class FrontEndTests(Base):
    def test_viewer_is_wired_into_the_page(self):
        self.login()
        page = self.c.get("/console").get_data(as_text=True)
        for needle in ('id="viewer"', 'id="viewer-stage"', 'id="viewer-close"', 'id="viewer-prev"',
                       'id="viewer-next"', 'id="viewer-dl"'):
            self.assertIn(needle, page)
        js = self.c.get("/static/console.js").get_data(as_text=True)
        self.assertIn("/api/media?path=", js)
        self.assertIn("openViewer", js)
        self.assertIn("#viewer", self.c.get("/static/style.css").get_data(as_text=True))

    def test_client_and_server_agree_on_which_extensions_are_media(self):
        js = self.c.get("/static/console.js").get_data(as_text=True)

        def exts(name):
            m = re.search(rf"const {name} = \[(.*?)\];", js)
            return set(re.findall(r'"([a-z0-9]+)"', m.group(1)))
        self.assertEqual(exts("IMAGE_EXTS") | exts("VIDEO_EXTS"), set(rapp.MEDIA_TYPES))
        self.assertTrue(all(rapp.MEDIA_TYPES[e].startswith("image/") for e in exts("IMAGE_EXTS")))
        self.assertTrue(all(rapp.MEDIA_TYPES[e].startswith("video/") for e in exts("VIDEO_EXTS")))


if __name__ == "__main__":
    unittest.main()
