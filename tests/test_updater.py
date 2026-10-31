"""``rconsole update``: checking, downloading, verifying, swapping files, rolling back, and the lock."""
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

import helpers  # noqa: F401

import app as rapp
import commands
import config
import interp
import updater

ROOT = Path(__file__).resolve().parent.parent
SKIP_TOP = {".git", ".venv", "data", "__pycache__", ".ruff_cache", "node_modules"}


def copy_tree(dst, version, extra=None):
    """A copy of this repository at ``dst`` carrying ``version`` (a stand-in install / release)."""
    dst = Path(dst)
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if rel.parts[0] in SKIP_TOP or "__pycache__" in rel.parts or "node_modules" in rel.parts:
            continue
        if p.is_file():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / rel)
    (dst / "version.py").write_text(f'__version__ = "{version}"\n')
    for rel, text in (extra or {}).items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_text(text)
    return dst


def make_tarball(tree, version, path):
    with tarfile.open(path, "w:gz") as tf:
        tf.add(tree, arcname=f"rconsole-{version}")
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Stub:
    """Lifecycle / reload stand-ins that record what the update asked for."""
    def __init__(self):
        self.calls = []
        self.reloaded = threading.Event()

    def begin_shutdown(self):
        self.calls.append("shutdown")

    def reload(self):
        self.calls.append("reload")
        self.reloaded.set()


class UpdaterBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rc-upd-"))
        self.app_dir = copy_tree(self.tmp / "app", "1.0.0")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = (updater.APP_DIR, updater.RELOAD_DELAY, updater.RELOAD_GRACE)
        updater.APP_DIR = self.app_dir
        updater.RELOAD_DELAY = updater.RELOAD_GRACE = 0
        shutil.rmtree(config.UPDATE_DIR, ignore_errors=True)
        updater._live.update(active=False, mode="", stage="", pct=0, error="", log=[])

    def tearDown(self):
        updater.APP_DIR, updater.RELOAD_DELAY, updater.RELOAD_GRACE = self._saved
        updater._live["active"] = False
        shutil.rmtree(config.UPDATE_DIR, ignore_errors=True)


class PureFunctionTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(updater.parse_version("v1.3.0"), (1, 3, 0))
        self.assertEqual(updater.parse_version("1.10.2"), (1, 10, 2))
        self.assertGreater(updater.parse_version("1.10.0"), updater.parse_version("1.9.9"))
        for bad in ("1.3", "v1.3.0-rc1", "latest", "", None, "1.3.0.1"):
            self.assertIsNone(updater.parse_version(bad), bad)

    def test_checksum_lookup(self):
        h = "a" * 64
        text = f"{h}  rconsole-1.3.0.tar.gz\n{'b' * 64} *Caddyfile\nnot a line\n"
        self.assertEqual(updater.checksum_for(text, "rconsole-1.3.0.tar.gz"), h)
        self.assertEqual(updater.checksum_for(text, "Caddyfile"), "b" * 64)
        self.assertIsNone(updater.checksum_for(text, "rconsole-9.9.9.tar.gz"))
        self.assertIsNone(updater.checksum_for("zz  rconsole-1.3.0.tar.gz", "rconsole-1.3.0.tar.gz"))

    def test_version_file_is_read_from_disk(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "version.py").write_text('"""doc"""\n__version__ = "4.5.6"\n')
        self.assertEqual(updater.read_version(d), "4.5.6")
        self.assertIsNone(updater.read_version(d / "nope"))

    def test_real_version_file_matches_the_module(self):
        from version import __version__
        self.assertEqual(updater.read_version(ROOT), __version__)


class ExtractTests(unittest.TestCase):
    def archive(self, members):
        path = Path(tempfile.mkdtemp()) / "x.tar.gz"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        with tarfile.open(path, "w:gz") as tf:
            for info, data in members:
                tf.addfile(info, io.BytesIO(data) if data is not None else None)
        return path

    def file(self, name, data=b"x", mode=0o644):
        i = tarfile.TarInfo(name)
        i.size, i.mode = len(data), mode
        return i, data

    def test_extracts_and_strips_the_top_directory(self):
        a = self.archive([self.file("rconsole-1/app.py", b"print(1)"), self.file("rconsole-1/run.sh", b"#!/bin/sh", 0o755)])
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        updater.safe_extract(a, out)
        self.assertEqual((out / "app.py").read_bytes(), b"print(1)")
        self.assertTrue(os.access(out / "run.sh", os.X_OK))
        self.assertFalse(os.access(out / "app.py", os.X_OK))

    def test_refuses_traversal_absolute_links_and_junk(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        link = tarfile.TarInfo("rconsole-1/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        for members in ([self.file("rconsole-1/../../escape.py")], [self.file("/abs.py")],
                        [(link, None)], [self.file("loose.py")]):
            with self.assertRaises(updater.UpdateError, msg=members[0][0].name):
                updater.safe_extract(self.archive(members), out)
        self.assertFalse((out.parent / "escape.py").exists())

    def test_not_a_tarball(self):
        p = Path(tempfile.mkdtemp()) / "x.tar.gz"
        self.addCleanup(shutil.rmtree, p.parent, ignore_errors=True)
        p.write_bytes(b"this is not a tarball")
        with self.assertRaises(updater.UpdateError):
            updater.safe_extract(p, p.parent / "o")


class TreeCheckTests(UpdaterBase):
    def test_good_tree_passes_and_wrong_version_fails(self):
        tree = copy_tree(self.tmp / "rel", "1.0.1")
        updater.check_tree(tree, "1.0.1")
        with self.assertRaises(updater.UpdateError):
            updater.check_tree(tree, "1.0.2")

    def test_missing_file_and_syntax_error_fail(self):
        tree = copy_tree(self.tmp / "rel", "1.0.1")
        (tree / "updater.py").unlink()
        with self.assertRaisesRegex(updater.UpdateError, "updater.py"):
            updater.check_tree(tree, "1.0.1")
        tree2 = copy_tree(self.tmp / "rel2", "1.0.1", {"broken.py": "def oops(:\n"})
        with self.assertRaisesRegex(updater.UpdateError, "does not compile"):
            updater.check_tree(tree2, "1.0.1")

    def test_smoke_test_catches_a_release_that_cannot_start(self):
        bad = copy_tree(self.tmp / "bad", "1.0.1", {"state.py": "raise RuntimeError('boom')\n"})
        with self.assertRaisesRegex(updater.UpdateError, "does not start"):
            updater.smoke_test(bad)

    def test_smoke_test_passes_for_a_working_release(self):
        updater.smoke_test(copy_tree(self.tmp / "ok", "1.0.1"))

    def test_dependency_change_detection(self):
        tree = copy_tree(self.tmp / "rel", "1.0.1")
        self.assertFalse(updater.needs_deps(tree))
        with open(tree / "requirements.txt", "a") as f:
            f.write("somenewpackage==1.0\n")
        self.assertTrue(updater.needs_deps(tree))


class SwapTests(UpdaterBase):
    def test_apply_replaces_adds_removes_and_never_touches_data(self):
        (self.app_dir / "data").mkdir()
        (self.app_dir / "data" / "users.json").write_text("precious")
        (self.app_dir / ".env").write_text("SECRET=1")
        (self.app_dir / "old_module.py").write_text("old")
        updater._write_json(updater.MANIFEST_FILE, {"version": "1.0.0", "files": ["old_module.py", "app.py"]})
        rel = copy_tree(self.tmp / "rel", "1.0.1", {"brand_new.py": "NEW = 1\n", "data/users.json": "evil"})
        updater.apply_tree(rel, "1.0.0", "1.0.1")
        self.assertEqual(updater.read_version(self.app_dir), "1.0.1")
        self.assertTrue((self.app_dir / "brand_new.py").exists())
        self.assertFalse((self.app_dir / "old_module.py").exists())          # in the old manifest, gone from the release
        self.assertEqual((self.app_dir / "data" / "users.json").read_text(), "precious")
        self.assertEqual((self.app_dir / ".env").read_text(), "SECRET=1")
        self.assertEqual(json.loads(updater.MANIFEST_FILE.read_text())["version"], "1.0.1")

    def test_files_that_were_not_installed_by_a_release_are_left_alone(self):
        (self.app_dir / "my_notes.txt").write_text("mine")
        updater.apply_tree(copy_tree(self.tmp / "rel", "1.0.1"), "1.0.0", "1.0.1")
        self.assertEqual((self.app_dir / "my_notes.txt").read_text(), "mine")

    def test_rollback_restores_everything(self):
        (self.app_dir / "old_module.py").write_text("old")
        updater._write_json(updater.MANIFEST_FILE, {"version": "1.0.0", "files": ["old_module.py"]})
        rel = copy_tree(self.tmp / "rel", "1.0.1", {"brand_new.py": "NEW = 1\n"})
        before = (self.app_dir / "app.py").read_bytes()
        updater.apply_tree(rel, "1.0.0", "1.0.1")
        self.assertEqual(updater.restore_previous(), "1.0.0")
        self.assertEqual(updater.read_version(self.app_dir), "1.0.0")
        self.assertEqual((self.app_dir / "app.py").read_bytes(), before)
        self.assertEqual((self.app_dir / "old_module.py").read_text(), "old")
        self.assertFalse((self.app_dir / "brand_new.py").exists())

    def test_a_failure_halfway_puts_the_old_files_back(self):
        rel = copy_tree(self.tmp / "rel", "1.0.1")
        real_place, calls = updater._place, []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 5:
                raise OSError("disk full")
            return real_place(src, dst)

        updater._place = flaky
        try:
            with self.assertRaisesRegex(updater.UpdateError, "previous version was restored"):
                updater.apply_tree(rel, "1.0.0", "1.0.1")
        finally:
            updater._place = real_place
        self.assertEqual(updater.read_version(self.app_dir), "1.0.0")

    def test_a_symlink_in_the_install_is_replaced_not_written_through(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("untouched")
        (self.app_dir / "LICENSE").unlink()
        os.symlink(outside, self.app_dir / "LICENSE")
        updater.apply_tree(copy_tree(self.tmp / "rel", "1.0.1"), "1.0.0", "1.0.1")
        self.assertEqual(outside.read_text(), "untouched")
        self.assertFalse((self.app_dir / "LICENSE").is_symlink())


def fake_release(test, version="1.0.1", extra=None, corrupt=False, with_sums=True):
    """Build a release in memory and return ``(info, patches)`` shaped like :func:`updater.check`."""
    tree = copy_tree(test.tmp / f"rel-{version}", version, extra)
    tar = test.tmp / f"rconsole-{version}.tar.gz"
    digest = make_tarball(tree, version, tar)
    sums = f"{'0' * 64 if corrupt else digest}  rconsole-{version}.tar.gz\n"
    info = {"current": "1.0.0", "latest": version, "tag": f"v{version}", "newer": True, "same": False,
            "notes": "- things", "published": "2026-11-01", "url": "https://example/release",
            "tar_name": f"rconsole-{version}.tar.gz", "bad": False,
            "tar": {"name": f"rconsole-{version}.tar.gz", "size": tar.stat().st_size, "browser_download_url": "x"},
            "sums": {"name": "SHA256SUMS", "browser_download_url": "y"} if with_sums else None}

    def fake_download(asset, dest, a, b):
        shutil.copyfile(tar, dest)
        return hashlib.sha256(Path(dest).read_bytes()).hexdigest()

    return info, fake_download, sums.encode()


class EndToEndTests(UpdaterBase):
    def run_update(self, info, download, sums, smoke=True):
        stub = Stub()
        saved = (updater._download, updater._get_bytes, updater.smoke_test)
        updater._download = download
        updater._get_bytes = lambda *a, **k: sums
        if not smoke:
            updater.smoke_test = lambda *a, **k: None
        try:
            updater._begin("update", info["current"], info["latest"])
            updater._run_update(info, stub, stub.reload)
            stub.reloaded.wait(5)
        finally:
            updater._download, updater._get_bytes, updater.smoke_test = saved
        return stub

    def test_full_update_installs_tests_swaps_and_restarts(self):
        info, dl, sums = fake_release(self, extra={"feature_marker.py": "X = 1\n"})
        stub = self.run_update(info, dl, sums)                       # includes the real import test
        self.assertEqual(stub.calls, ["shutdown", "reload"])         # quiet first, restart last
        self.assertEqual(updater.installed_version(), "1.0.1")
        self.assertTrue((self.app_dir / "feature_marker.py").exists())
        state = updater.read_state()
        self.assertEqual(state["active"]["version"], "1.0.1")
        self.assertEqual(state["pending"]["boots"], 0)
        self.assertTrue(state["last"]["ok"])
        self.assertTrue((config.UPDATE_DIR / "releases" / "1.0.1" / "version.py").exists())
        self.assertFalse((config.UPDATE_DIR / "work").exists())      # scratch space is cleaned up
        self.assertTrue(updater.status()["last"]["ok"])

    def test_checksum_mismatch_changes_nothing_and_stops_nothing(self):
        info, dl, sums = fake_release(self, corrupt=True)
        stub = self.run_update(info, dl, sums, smoke=False)
        self.assertEqual(stub.calls, [])                              # nothing was stopped
        self.assertEqual(updater.installed_version(), "1.0.0")
        self.assertFalse(updater.is_locked())                         # and the console is usable again
        self.assertIn("checksum mismatch", updater.status()["error"])
        self.assertFalse(updater.read_state()["last"]["ok"])

    def test_a_release_that_does_not_start_is_never_installed(self):
        info, dl, sums = fake_release(self, extra={"state.py": "raise RuntimeError('boom')\n"})
        stub = self.run_update(info, dl, sums)
        self.assertEqual(stub.calls, [])
        self.assertEqual(updater.installed_version(), "1.0.0")
        self.assertIn("does not start", updater.status()["error"])

    def test_missing_checksum_entry_is_refused(self):
        info, dl, _ = fake_release(self)
        stub = self.run_update(info, dl, b"deadbeef  something-else.tar.gz\n", smoke=False)
        self.assertEqual(stub.calls, [])
        self.assertIn("no entry", updater.status()["error"])

    def test_start_refuses_a_release_without_checksums(self):
        info, _, _ = fake_release(self, with_sums=False)
        with self.assertRaisesRegex(updater.UpdateError, "SHA256SUMS"):
            updater.start(info)
        self.assertFalse(updater.is_locked())

    def test_rollback_after_an_update(self):
        info, dl, sums = fake_release(self)
        self.run_update(info, dl, sums, smoke=False)
        updater._live["active"] = False
        stub = Stub()
        updater._begin("rollback", "1.0.1", "1.0.0")
        updater._run_rollback("1.0.0", stub, stub.reload)
        stub.reloaded.wait(5)
        self.assertEqual(updater.installed_version(), "1.0.0")
        state = updater.read_state()
        self.assertIsNone(state["active"])
        self.assertIn("1.0.1", state["bad"])                          # not offered again unless --force
        self.assertEqual(stub.calls, ["shutdown", "reload"])

    def test_rollback_without_history_is_refused(self):
        with self.assertRaisesRegex(updater.UpdateError, "no previous version"):
            updater.start_rollback()

    def test_only_one_update_at_a_time(self):
        updater._begin("update", "1.0.0", "1.0.1")
        with self.assertRaisesRegex(updater.UpdateError, "already running"):
            updater._begin("update", "1.0.0", "1.0.2")


class BootSafetyTests(UpdaterBase):
    def installed_update(self):
        info, dl, sums = fake_release(self)
        EndToEndTests.run_update(self, info, dl, sums, smoke=False)
        updater._live["active"] = False

    def test_confirm_boot_clears_the_pending_flag(self):
        self.installed_update()
        self.assertTrue(updater.read_state()["pending"])
        updater.confirm_boot()
        self.assertIsNone(updater.read_state()["pending"])

    def test_a_version_that_keeps_failing_to_start_is_rolled_back(self):
        self.installed_update()
        for _ in range(updater.MAX_BOOTS):
            updater.sync(lambda m: None)
            self.assertEqual(updater.installed_version(), "1.0.1")    # still given its chances
        updater.sync(lambda m: None)                                  # one failed start too many
        self.assertEqual(updater.installed_version(), "1.0.0")
        state = updater.read_state()
        self.assertIn("1.0.1", state["bad"])
        self.assertFalse(state["last"]["ok"])
        self.assertIsNone(state["pending"])

    def test_a_healthy_version_is_not_rolled_back(self):
        self.installed_update()
        updater.confirm_boot()
        for _ in range(5):
            updater.sync(lambda m: None)
        self.assertEqual(updater.installed_version(), "1.0.1")

    def test_a_redeployed_older_image_gets_the_saved_release_back(self):
        self.installed_update()
        updater.confirm_boot()
        (self.app_dir / "version.py").write_text('__version__ = "1.0.0"\n')    # the image was rebuilt from old code
        logs = []
        updater.sync(logs.append)
        self.assertEqual(updater.installed_version(), "1.0.1")
        self.assertTrue(logs)

    def test_a_newer_image_wins_over_the_saved_release(self):
        self.installed_update()
        updater.confirm_boot()
        (self.app_dir / "version.py").write_text('__version__ = "2.0.0"\n')
        updater.sync(lambda m: None)
        self.assertEqual(updater.installed_version(), "2.0.0")
        self.assertIsNone(updater.read_state()["active"])

    def test_after_a_restore_on_a_blank_disk_the_release_is_applied_and_the_server_restarts_once(self):
        self.installed_update()
        updater.confirm_boot()
        (self.app_dir / "version.py").write_text('__version__ = "1.0.0"\n')      # image rebuilt from older code
        restarts = []
        self.assertTrue(updater.after_restore(lambda m: None, restart=lambda: restarts.append(1)))
        self.assertEqual((updater.installed_version(), restarts), ("1.0.1", [1]))
        self.assertFalse(updater.after_restore(lambda m: None, restart=lambda: restarts.append(1)))
        self.assertEqual(restarts, [1])                                          # no restart loop

    def test_sync_with_no_state_does_nothing(self):
        updater.sync(lambda m: self.fail("should be silent"))


class LockTests(unittest.TestCase):
    def setUp(self):
        rapp.app.config["TESTING"] = True
        self.c = rapp.app.test_client()
        updater._live.update(active=False, mode="update", stage="download", pct=30, error="", log=["x"])
        updater._live["from"], updater._live["to"] = "1.0.0", "1.0.1"

    def tearDown(self):
        updater._live["active"] = False

    def test_everything_is_refused_while_updating(self):
        updater._live["active"] = True
        for method, url in (("post", "/api/run"), ("get", "/api/dir?path=/"), ("get", "/api/tabs"),
                            ("post", "/login"), ("get", "/ws"), ("post", "/api/fs"), ("get", "/myapp/")):
            r = getattr(self.c, method)(url, json={"cmd": "id"} if method == "post" else None)
            self.assertEqual(r.status_code, 503, url)
            self.assertEqual(r.headers.get("X-Rconsole-Updating"), "1", url)
            self.assertTrue(r.get_json()["updating"], url)

    def test_a_browser_page_gets_a_readable_waiting_page(self):
        updater._live["active"] = True
        r = self.c.get("/", headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 503)
        self.assertIn(b"rconsole is updating", r.data)
        self.assertIn(b"refresh", r.data)

    def test_health_status_and_static_still_answer(self):
        updater._live["active"] = True
        self.assertEqual(self.c.get("/healthz").status_code, 200)
        st = self.c.get("/api/update/status")
        self.assertEqual(st.status_code, 200)
        body = st.get_json()
        self.assertTrue(body["updating"])
        self.assertEqual(body["to"], "1.0.1")
        self.assertNotIn("log", body)                                 # the log is for logged-in users only
        static = self.c.get("/static/console.js")
        static.close()
        self.assertEqual(static.status_code, 200)

    def test_nothing_is_blocked_when_no_update_runs(self):
        self.assertEqual(self.c.get("/healthz").status_code, 200)
        self.assertEqual(self.c.get("/", headers={"Accept": "text/html"}).status_code, 302)   # to /login
        self.assertFalse(self.c.get("/api/update/status").get_json()["updating"])


class CommandTests(UpdaterBase):
    def setUp(self):
        super().setUp()
        self.info = {"current": "1.0.0", "latest": "1.0.1", "tag": "v1.0.1", "newer": True, "same": False,
                     "notes": "### Added\n- a thing\n- another", "published": "2026-11-01", "url": "https://x/r",
                     "tar_name": "rconsole-1.0.1.tar.gz", "bad": False, "tar": {"a": 1}, "sums": {"a": 1}}
        self._saved_fns = (updater.check, updater.start, updater.start_rollback, updater.blockers)
        self.started = []
        updater.check = lambda: dict(self.info)
        updater.start = lambda info, force=False: self.started.append((info["latest"], force))
        updater.start_rollback = lambda: self.started.append("rollback")
        updater.blockers = lambda force=False: []
        self.tab = {"gen": None, "prompt": None, "cwd": "/", "history": []}

    def tearDown(self):
        updater.check, updater.start, updater.start_rollback, updater.blockers = self._saved_fns
        super().tearDown()

    def run_line(self, line, admin=True):
        return interp.process(self.tab, "ark", admin, "ark", line)

    def test_the_command_is_registered_and_not_hidden_behind_sudo(self):
        self.assertIn("rconsole", commands.COMMANDS)
        self.assertNotIn("rconsole", commands.PROTECTED)

    def test_check_only_reports_and_changes_nothing(self):
        out = self.run_line("rconsole update --check")["output"]
        self.assertIn("1.0.0 → 1.0.1", out)
        self.assertIn("a thing", out)
        self.assertEqual(self.started, [])

    def test_everyone_may_check_but_only_admins_update(self):
        self.assertIn("1.0.1", self.run_line("rconsole update --check", admin=False)["output"])
        self.assertIn("only administrators", self.run_line("rconsole update", admin=False)["output"])
        self.assertEqual(self.started, [])

    def test_update_asks_first_and_yes_starts_it(self):
        r = self.run_line("rconsole update")
        self.assertIn("Update to 1.0.1 now?", r["prompt"])
        self.assertIn("nothing else can be done", r["output"])
        self.assertEqual(self.started, [])                             # nothing yet
        done = self.run_line("y")
        self.assertTrue(done["updating"])
        self.assertEqual(self.started, [("1.0.1", False)])

    def test_anything_but_yes_cancels(self):
        self.run_line("rconsole update")
        self.assertIn("cancelled", self.run_line("n")["output"])
        self.assertEqual(self.started, [])

    def test_dash_dash_yes_skips_the_question(self):
        r = self.run_line("rconsole update --yes")
        self.assertTrue(r["updating"])
        self.assertEqual(self.started, [("1.0.1", False)])

    def test_up_to_date(self):
        self.info.update(newer=False, same=True, latest="1.0.0", tag="v1.0.0")
        self.assertIn("up to date", self.run_line("rconsole update --yes")["output"])
        self.assertEqual(self.started, [])

    def test_force_reinstalls_the_same_version(self):
        self.info.update(newer=False, same=True, latest="1.0.0", tag="v1.0.0")
        self.run_line("rconsole update --yes --force")
        self.assertEqual(self.started, [("1.0.0", True)])

    def test_a_release_that_failed_before_is_not_retried_without_force(self):
        self.info["bad"] = True
        self.assertIn("--force", self.run_line("rconsole update --yes")["output"])
        self.assertEqual(self.started, [])

    def test_blockers_are_reported(self):
        updater.blockers = lambda force=False: ["this is a git checkout — use git pull"]
        self.assertIn("git pull", self.run_line("rconsole update --yes")["output"])
        self.assertEqual(self.started, [])

    def test_github_errors_are_reported_not_raised(self):
        def boom():
            raise updater.UpdateError("cannot reach GitHub: offline")
        updater.check = boom
        self.assertIn("cannot reach GitHub", self.run_line("rconsole update")["output"])

    def test_nothing_starts_while_an_update_runs(self):
        updater._live["active"] = True
        self.assertIn("already running", self.run_line("rconsole update --yes")["output"])
        self.assertEqual(self.started, [])

    def test_rollback_needs_admin_history_and_confirmation(self):
        self.assertIn("only administrators", self.run_line("rconsole update --rollback", admin=False)["output"])
        self.assertIn("nothing to go back", self.run_line("rconsole update --rollback")["output"])
        updater._write_json(updater.PREVIOUS_DIR / "meta.json", {"version": "0.9.0"})
        r = self.run_line("rconsole update --rollback")
        self.assertIn("Roll back now?", r["prompt"])
        self.assertTrue(self.run_line("y")["updating"])
        self.assertEqual(self.started, ["rollback"])

    def test_version_help_and_unknown_subcommand(self):
        self.assertIn("rconsole ", self.run_line("rconsole version")["output"])
        self.assertIn("rconsole update", self.run_line("rconsole")["output"])
        self.assertIn("unknown", self.run_line("rconsole frobnicate")["output"])
        self.assertIn("unknown option", self.run_line("rconsole update --nope")["output"])


class BackupTests(UpdaterBase):
    def setUp(self):
        super().setUp()
        import backup
        self.backup = backup
        self._data = config.DATA_DIR
        config.DATA_DIR = self.tmp / "data"                 # backup reads it at call time
        up = config.DATA_DIR / "update"
        for rel in ("state.json", "releases/1.0.1/app.py", "releases/1.0.1/static/a.js",
                    "work/tree/app.py", "previous/files/app.py", "site-1.0.1/flask/x.py", "manifest.json"):
            (up / rel).parent.mkdir(parents=True, exist_ok=True)
            (up / rel).write_text("x")

    def tearDown(self):
        config.DATA_DIR = self._data
        super().tearDown()

    def test_the_installed_release_is_backed_up_but_scratch_and_rollback_copies_are_not(self):
        names = {arc for arc, _ in self.backup.iter_files()}
        self.assertIn("update/state.json", names)
        self.assertIn("update/releases/1.0.1/app.py", names)
        self.assertIn("update/releases/1.0.1/static/a.js", names)
        for out in ("update/work/tree/app.py", "update/previous/files/app.py", "update/manifest.json",
                    "update/site-1.0.1/flask/x.py"):
            self.assertNotIn(out, names)

    def test_restore_only_accepts_those_update_paths(self):
        ok = self.backup._allowed
        self.assertTrue(ok("update/state.json"))
        self.assertTrue(ok("update/releases/1.0.1/app.py"))
        for bad in ("update/work/x", "update/previous/meta.json", "update/releases/x", "update/../users.json",
                    "update/releases/../../x.py", "update"):
            self.assertFalse(ok(bad), bad)


class BlockerTests(UpdaterBase):
    def test_git_checkout_is_not_updated_without_force(self):
        (self.app_dir / ".git").mkdir()
        self.assertTrue(any("git checkout" in b for b in updater.blockers()))
        self.assertFalse(any("git checkout" in b for b in updater.blockers(force=True)))

    def test_a_clean_install_has_no_blockers(self):
        self.assertEqual(updater.blockers(), [])


class WiringTests(unittest.TestCase):
    def test_update_token_never_reaches_user_commands(self):
        self.assertIn("RCONSOLE_UPDATE_TOKEN", config.SECRET_ENV_VARS)

    def test_entrypoint_runs_the_sync_before_the_server(self):
        text = (ROOT / "docker-entrypoint.sh").read_text()
        self.assertIn("updater.py sync", text)
        self.assertLess(text.index("updater.py sync"), text.rindex('exec "$@"'))

    def test_app_imports_the_dependency_path_fix_first(self):
        text = (ROOT / "app.py").read_text()
        self.assertLess(text.index("import _pathfix"), text.index("import requests"))

    def test_redirects_never_forward_the_token_to_another_host(self):
        import urllib.request
        h = updater._Redirect()
        req = urllib.request.Request("https://api.github.com/x", headers={"Authorization": "Bearer secret"})
        new = h.redirect_request(req, None, 302, "Found", {}, "https://objects.example.net/blob")
        self.assertNotIn("Authorization", new.headers)
        same = h.redirect_request(req, None, 302, "Found", {}, "https://api.github.com/y")
        self.assertIn("Authorization", same.headers)


if __name__ == "__main__":
    unittest.main()
