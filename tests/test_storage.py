"""Repo storage (gitstore) against a local bare git repository, and the dashboard-editable settings."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import helpers  # noqa: F401

import config
import gitstore
import settings
import settingsfile
from test_dashboard import DashBase


def make_remote():
    d = tempfile.mkdtemp(prefix="rc-remote-")
    bare = os.path.join(d, "r.git")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    for k, v in (("uploadpack.allowFilter", "true"), ("uploadpack.allowAnySHA1InWant", "true")):
        subprocess.run(["git", "-C", bare, "config", k, v], check=True)
    return d, bare


class StoreBase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k) for k in (
            "BACKUP_URL", "BACKUP_PASSPHRASE", "STORAGE", "STORAGE_ENCRYPT", "STORAGE_CHUNK_MB", "STORAGE_STUB_MB",
            "STORAGE_CACHE_MB", "BACKUP_TOKEN")}
        self.remote_dir, self.bare = make_remote()
        config.BACKUP_URL = "file://" + self.bare
        config.BACKUP_TOKEN = ""
        config.BACKUP_PASSPHRASE = "test-passphrase"
        config.STORAGE, config.STORAGE_ENCRYPT = "github", True
        config.STORAGE_CHUNK_MB, config.STORAGE_STUB_MB, config.STORAGE_CACHE_MB = 1, 1, 1024
        shutil.rmtree(config.WORKSPACE_DIR, ignore_errors=True)
        config.WORKSPACE_DIR.mkdir(parents=True)
        shutil.rmtree(config.STORAGE_DIR, ignore_errors=True)
        gitstore.forget_everything()
        gitstore.bootstrap(log=lambda m: None)
        self.ws = config.WORKSPACE_DIR

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        gitstore.forget_everything()
        shutil.rmtree(self.remote_dir, ignore_errors=True)

    def settle(self):
        time.sleep(gitstore.HOT_SECONDS + 0.3)

    def sync(self):
        self.settle()
        return gitstore.sync_once("test")

    def wipe_disk(self):
        shutil.rmtree(self.ws, ignore_errors=True)
        shutil.rmtree(config.STORAGE_DIR, ignore_errors=True)
        gitstore.forget_everything()
        self.ws.mkdir(parents=True)
        gitstore.bootstrap(log=lambda m: None)

    def tree(self):
        r = subprocess.run(["git", "-C", self.bare, "ls-tree", "-r", "--name-only", "refs/heads/rconsole-storage"],
                           capture_output=True, text=True)
        return r.stdout.split()


class OffByDefaultTests(unittest.TestCase):
    def test_nothing_happens_without_a_repo(self):
        old = (config.BACKUP_URL, config.STORAGE)
        try:
            config.BACKUP_URL, config.STORAGE = "", "github"
            self.assertFalse(gitstore.active())
            self.assertTrue(gitstore.problems())
            self.assertEqual(gitstore.ensure_local("/nonexistent"), 0)
            self.assertEqual(gitstore.sync_once("x")[1], "repo storage is off")
        finally:
            config.BACKUP_URL, config.STORAGE = old

    def test_host_mode_is_inactive_even_with_a_repo(self):
        old = (config.BACKUP_URL, config.STORAGE, config.BACKUP_PASSPHRASE)
        try:
            config.BACKUP_URL, config.STORAGE, config.BACKUP_PASSPHRASE = "file:///x", "host", "p"
            self.assertFalse(gitstore.active())
        finally:
            config.BACKUP_URL, config.STORAGE, config.BACKUP_PASSPHRASE = old

    def test_encryption_without_a_passphrase_is_a_problem(self):
        old = (config.BACKUP_URL, config.BACKUP_PASSPHRASE, config.STORAGE_ENCRYPT)
        try:
            config.BACKUP_URL, config.BACKUP_PASSPHRASE, config.STORAGE_ENCRYPT = "file:///x", "", True
            self.assertTrue(any("passphrase" in p for p in gitstore.problems()))
            config.STORAGE_ENCRYPT = False
            self.assertFalse(any("passphrase" in p for p in gitstore.problems()))
        finally:
            config.BACKUP_URL, config.BACKUP_PASSPHRASE, config.STORAGE_ENCRYPT = old


class RoundTripTests(StoreBase):
    def test_upload_wipe_restore(self):
        (self.ws / "a.txt").write_text("hello")
        (self.ws / "sub").mkdir()
        (self.ws / "sub" / "b.txt").write_text("bee")
        os.symlink("a.txt", self.ws / "ln")
        ok, msg = self.sync()
        self.assertTrue(ok, msg)
        self.wipe_disk()
        self.assertEqual((self.ws / "a.txt").read_text(), "hello")
        self.assertEqual((self.ws / "sub" / "b.txt").read_text(), "bee")
        self.assertEqual(os.readlink(self.ws / "ln"), "a.txt")

    def test_big_file_is_split_and_comes_back_whole(self):
        data = os.urandom(2_600_000)                       # 3 pieces at 1 MB
        (self.ws / "big.bin").write_bytes(data)
        ok, msg = self.sync()
        self.assertTrue(ok, msg)
        entry = gitstore.STATE["man"]["host/big.bin"]
        self.assertEqual(len(entry["c"]), 3)
        self.assertTrue(all(len(self.tree()) > 0 for _ in [0]))
        self.wipe_disk()                                    # big file is only a placeholder now
        self.assertIn("host/big.bin", gitstore.STUBS)
        self.assertEqual(gitstore.dir_stub_sizes(self.ws), {"big.bin": len(data)})
        self.assertLess(os.path.getsize(self.ws / "big.bin"), 1024)
        self.assertEqual(gitstore.ensure_local(self.ws / "big.bin"), 1)
        self.assertEqual((self.ws / "big.bin").read_bytes(), data)
        self.assertNotIn("host/big.bin", gitstore.STUBS)

    def test_no_piece_exceeds_the_chunk_size_plus_overhead(self):
        (self.ws / "big.bin").write_bytes(os.urandom(2_600_000))
        self.sync()
        r = subprocess.run(["git", "-C", self.bare, "ls-tree", "-r", "-l", "refs/heads/rconsole-storage"],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "\tb/" in line:
                self.assertLess(int(line.split()[3]), 1024 * 1024 + 4096)

    def test_repo_content_is_encrypted(self):
        (self.ws / "secret-name.txt").write_text("TOPSECRETCONTENT")
        self.sync()
        listing = " ".join(self.tree())
        self.assertNotIn("secret-name", listing)
        self.assertIn("manifest.enc", listing)
        blobs = subprocess.run(["git", "-C", self.bare, "grep", "-c", "TOPSECRETCONTENT", "refs/heads/rconsole-storage"],
                               capture_output=True, text=True)
        self.assertNotEqual(blobs.returncode, 0)

    def test_plain_mode_and_rewrite_both_ways(self):
        (self.ws / "p.txt").write_text("PLAINCONTENT")
        self.sync()
        config.STORAGE_ENCRYPT = False
        ok, msg = gitstore.reencode()
        self.assertTrue(ok, msg)
        self.assertIn("manifest.gz", self.tree())
        self.assertNotIn("manifest.enc", self.tree())
        self.wipe_disk()
        self.assertEqual((self.ws / "p.txt").read_text(), "PLAINCONTENT")
        config.STORAGE_ENCRYPT = True
        ok, msg = gitstore.reencode()
        self.assertTrue(ok, msg)
        self.assertIn("manifest.enc", self.tree())

    def test_deleting_a_file_removes_its_pieces(self):
        (self.ws / "gone.bin").write_bytes(os.urandom(300_000))
        self.sync()
        before = len([t for t in self.tree() if t.startswith("b/")])
        (self.ws / "gone.bin").unlink()
        self.sync()
        self.assertLess(len([t for t in self.tree() if t.startswith("b/")]), before)

    def test_identical_data_is_stored_once(self):
        blob = os.urandom(200_000)
        (self.ws / "one").write_bytes(blob)
        (self.ws / "two").write_bytes(blob)
        self.sync()
        self.assertEqual(len([t for t in self.tree() if t.startswith("b/")]), 1)

    def test_wrong_passphrase_is_reported_not_silently_ignored(self):
        (self.ws / "a.txt").write_text("x")
        self.sync()
        config.BACKUP_PASSPHRASE = "another"
        gitstore._key_cache.clear()
        shutil.rmtree(self.ws)
        shutil.rmtree(config.STORAGE_DIR, ignore_errors=True)
        gitstore.forget_everything()
        self.ws.mkdir()
        gitstore.bootstrap(log=lambda m: None)
        self.assertIn("passphrase", gitstore.status()["blocked"])
        self.assertFalse((self.ws / "a.txt").exists())


class SafetyTests(StoreBase):
    def test_unreachable_repo_at_boot_never_deletes_anything(self):
        (self.ws / "keep.txt").write_text("precious")
        self.sync()
        self.wipe_disk()
        self.assertTrue((self.ws / "keep.txt").exists())
        shutil.rmtree(self.ws)
        shutil.rmtree(config.STORAGE_DIR, ignore_errors=True)
        gitstore.forget_everything()
        self.ws.mkdir()
        config.BACKUP_URL = "file://" + self.bare + "-missing"
        gitstore.bootstrap(log=lambda m: None)
        self.assertTrue(gitstore.status()["blocked"])
        ok, _ = gitstore.sync_once("t")
        self.assertFalse(ok)                                # paused: nothing pushed, nothing removed
        config.BACKUP_URL = "file://" + self.bare
        self.assertTrue(gitstore._retry_boot())
        self.assertEqual((self.ws / "keep.txt").read_text(), "precious")

    def test_a_file_still_being_written_is_not_uploaded_half_done(self):
        (self.ws / "live.log").write_text("x")
        puts, dels, roots, hot = gitstore._scan()
        self.assertTrue(hot)
        self.assertEqual(puts, [])

    def test_moved_placeholder_keeps_the_data(self):
        data = os.urandom(1_500_000)
        (self.ws / "m.bin").write_bytes(data)
        self.sync()
        self.wipe_disk()
        os.rename(self.ws / "m.bin", self.ws / "moved.bin")
        ok, msg = self.sync()
        self.assertTrue(ok, msg)
        self.assertNotIn("host/m.bin", gitstore.STATE["man"])
        self.assertEqual(gitstore.ensure_local(self.ws / "moved.bin"), 1)
        self.assertEqual((self.ws / "moved.bin").read_bytes(), data)

    def test_eviction_turns_only_clean_big_files_into_placeholders(self):
        big = os.urandom(1_500_000)
        (self.ws / "big.bin").write_bytes(big)
        (self.ws / "small.txt").write_text("s")
        self.sync()
        config.STORAGE_CACHE_MB = 1
        gitstore._evict_if_needed()
        self.assertIn("host/big.bin", gitstore.STUBS)
        self.assertNotIn("host/small.txt", gitstore.STUBS)
        gitstore.pin(self.ws / "big.bin")
        self.assertEqual((self.ws / "big.bin").read_bytes(), big)
        self.assertFalse(gitstore.evict("host/big.bin"))    # pinned files stay

    def test_command_naming_a_placeholder_fetches_it(self):
        data = os.urandom(1_300_000)
        (self.ws / "tool.bin").write_bytes(data)
        self.sync()
        self.wipe_disk()
        self.assertIn("host/tool.bin", gitstore.STUBS)
        n = gitstore.before_command("python tool.bin --x", "", lambda w: self.ws / w if not w.startswith("-") else None)
        self.assertEqual(n, 1)
        self.assertEqual((self.ws / "tool.bin").read_bytes(), data)

    def test_restore_refuses_to_write_through_a_symlink(self):
        outside = tempfile.mkdtemp()
        try:
            os.symlink(outside, self.ws / "evil")
            with self.assertRaises(gitstore.StoreError):
                gitstore._safe_target("host/evil/x.txt")
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_status_never_contains_secrets(self):
        config.BACKUP_TOKEN = "ghp_SECRETTOKEN"
        text = repr(gitstore.status())
        self.assertNotIn("ghp_SECRETTOKEN", text)
        self.assertNotIn("test-passphrase", text)


class BackupInteractionTests(StoreBase):
    def test_workspace_is_not_in_the_backup_when_storage_is_on(self):
        import backup
        (self.ws / "w.txt").write_text("x")
        names = [n for n, _p in backup.iter_files()]
        self.assertFalse(any(n.startswith("workspace/") for n in names))
        config.STORAGE = "host"
        names = [n for n, _p in backup.iter_files()]
        self.assertTrue(any(n.startswith("workspace/") for n in names))


class SettingsTests(DashBase):
    def setUp(self):
        super().setUp()
        self._env = dict(os.environ)
        settingsfile.path(config.DATA_DIR).unlink(missing_ok=True)

    def tearDown(self):
        settings.set_values({e: None for e in settingsfile.load(config.DATA_DIR)})
        os.environ.clear()
        os.environ.update(self._env)
        settings._pending_restart.clear()
        super().tearDown()

    def test_save_applies_live_and_persists(self):
        ok, msg, restart = settings.set_values({"RCONSOLE_MAX_USERS": "7"})
        self.assertTrue(ok, msg)
        self.assertEqual(config.MAX_USERS, 7)
        self.assertEqual(settingsfile.load(config.DATA_DIR)["RCONSOLE_MAX_USERS"], "7")
        self.assertEqual(restart, [])

    def test_reset_restores_the_default(self):
        settings.set_values({"RCONSOLE_MAX_USERS": "7"})
        settings.set_values({"RCONSOLE_MAX_USERS": None})
        self.assertEqual(config.MAX_USERS, 10)
        self.assertNotIn("RCONSOLE_MAX_USERS", settingsfile.load(config.DATA_DIR))

    def test_invalid_values_change_nothing(self):
        ok, msg, _ = settings.set_values({"RCONSOLE_MAX_USERS": "7", "RCONSOLE_STORAGE_CHUNK_MB": "500"})
        self.assertFalse(ok)
        self.assertNotEqual(config.MAX_USERS, 7)
        self.assertFalse(settingsfile.load(config.DATA_DIR))

    def test_restart_only_options_are_flagged(self):
        ok, msg, restart = settings.set_values({"RCONSOLE_THREADS": "32"})
        self.assertTrue(ok)
        self.assertEqual(restart, ["RCONSOLE_THREADS"])
        self.assertIn("RCONSOLE_THREADS", settings.pending_restart())

    def test_locked_names_cannot_be_set_or_loaded(self):
        for env in ("RCONSOLE_SECRET", "RCONSOLE_PEPPER", "RCONSOLE_DATA_DIR", "RCONSOLE_ADMIN_PASSWORD"):
            ok, _msg, _r = settings.set_values({env: "x"})
            self.assertFalse(ok, env)
        settingsfile.save({"RCONSOLE_SECRET": "evil"}, config.DATA_DIR)
        self.assertNotIn("RCONSOLE_SECRET", settingsfile.load(config.DATA_DIR))

    def test_secrets_are_write_only_and_the_file_is_private(self):
        settings.set_values({"RCONSOLE_BACKUP_TOKEN": "ghp_topsecret"})
        self.assertEqual(config.BACKUP_TOKEN, "ghp_topsecret")
        snap = settings.snapshot()
        self.assertNotIn("ghp_topsecret", repr(snap))
        row = next(s for g in snap["groups"] for s in g["settings"] if s["env"] == "RCONSOLE_BACKUP_TOKEN")
        self.assertTrue(row["set"])
        self.assertNotIn("value", row)
        self.assertEqual(oct(settingsfile.path(config.DATA_DIR).stat().st_mode & 0o777), "0o600")

    def test_backup_copy_of_settings_leaves_secrets_out(self):
        import backup
        settings.set_values({"RCONSOLE_BACKUP_TOKEN": "ghp_topsecret", "RCONSOLE_MAX_USERS": "6"})
        p = backup._settings_copy()
        text = p.read_text()
        self.assertIn("RCONSOLE_MAX_USERS", text)
        self.assertNotIn("ghp_topsecret", text)

    def test_every_setting_is_a_documented_variable(self):
        import sys
        sys.path.insert(0, str(config.HERE / "scripts"))
        import envvars
        known = {v[1] for v in envvars.VARS}
        for s in settings.SETTINGS:
            self.assertIn(s.env, known, s.env)

    def test_dashboard_endpoints(self):
        c = self.admin()
        d = self.data(c, "settings")
        self.assertTrue(any(g["key"] == "storage" for g in d["groups"]))
        code, r = self.act(c, "settings.save", changes={"RCONSOLE_MAX_USERS": "9"})
        self.assertEqual(code, 200, r)
        self.assertEqual(config.MAX_USERS, 9)
        code, r = self.act(c, "settings.reset", names=["RCONSOLE_MAX_USERS"])
        self.assertEqual(code, 200, r)
        self.assertEqual(config.MAX_USERS, 10)
        code, r = self.act(c, "repo.mode", mode="github")
        self.assertEqual(code, 200)
        self.assertIn("host", r["message"])                  # no repo: stays on the host
        self.assertFalse(gitstore.active())
        self.act(c, "repo.mode", mode="host")
        repo = self.data(c, "repo")
        self.assertIn("status", repo)
        self.assertEqual(self.user().get("/api/dashboard/data?section=settings").status_code, 403)

    def test_dashboard_value_wins_over_the_environment(self):
        os.environ["RCONSOLE_METRICS_INTERVAL"] = "30"
        settings.set_values({"RCONSOLE_METRICS_INTERVAL": "12"})
        self.assertEqual(config.METRICS_INTERVAL, 12)
        settings.set_values({"RCONSOLE_METRICS_INTERVAL": None})
        self.assertEqual(os.environ["RCONSOLE_METRICS_INTERVAL"], "30")


class StorageCommandTests(DashBase):
    def test_status_and_usage(self):
        c = self.admin()
        out = self.run_cmd(c, "storage")
        self.assertIn("Repo storage", out)
        self.assertIn("host", out)
        self.assertIn("usage", self.run_cmd(c, "storage nonsense"))
        self.assertIn("only admins", self.run_cmd(self.user(), "storage sync"))
        self.assertIn("already on this disk", self.run_cmd(c, "storage get foo"))


if __name__ == "__main__":
    unittest.main()
