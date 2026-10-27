"""Encrypted backups: what is saved, that it is encrypted, restore safety, git history."""
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

import backup
import config

PASS = "correct horse battery staple"


class BackupBase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k) for k in (
            "BACKUP_URL", "BACKUP_DIR", "BACKUP_TOKEN", "BACKUP_PASSPHRASE", "BACKUP_ENCRYPT",
            "BACKUP_BRANCH", "BACKUP_MAX_MB", "BACKUP_USER")}
        self.tmp = Path(tempfile.mkdtemp(prefix="bk-test-"))
        self.data = self.tmp / "data"
        self.data.mkdir()
        self._data_dir = config.DATA_DIR
        config.DATA_DIR = self.data              # the module reads this at call time
        config.BACKUP_DIR = str(self.tmp / "dest")
        config.BACKUP_URL = ""
        config.BACKUP_PASSPHRASE = PASS
        config.BACKUP_ENCRYPT = True
        config.BACKUP_TOKEN = ""
        config.BACKUP_MAX_MB = 40
        backup._status.update(last_sig=None, last_ok=None, last_error="", count=0, restored=None)

    def tearDown(self):
        config.DATA_DIR = self._data_dir
        for k, v in self._saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def populate(self):
        d = self.data
        (d / "users.json").write_text('{"ark": {"password": "SECRET-HASH-123"}}')
        (d / "secret.key").write_text("SECRET-KEY-456")
        (d / "state").mkdir()
        (d / "state" / "ark.json").write_text('{"env": {"OPENAI_API_KEY": "sk-TOPSECRET"}}')
        (d / "workspace" / "bot").mkdir(parents=True)
        (d / "workspace" / "bot" / "bot.py").write_text("print('hello bot')\n")
        home = d / "vm" / "ark" / "debian" / "home"
        home.mkdir(parents=True)
        (home / "notes.txt").write_text("my notes")
        (d / "vm" / "ark" / "debian" / "distro.json").write_text(json.dumps({"id": "debian", "release": "trixie"}))
        (d / "vm" / "ark" / "debian" / "setup.json").write_text('["python"]')
        # things that must NOT be saved
        (d / "vm" / "ark" / "debian" / "rootfs" / "etc").mkdir(parents=True)
        (d / "vm" / "ark" / "debian" / "rootfs" / "etc" / "passwd").write_text("root:x:0:0")
        (d / "vm" / "cache").mkdir(parents=True)
        (d / "vm" / "cache" / "debian.tar.xz").write_text("huge download")
        (d / "vm" / "bin").mkdir()
        (d / "vm" / "bin" / "proot").write_text("binary")
        (home / "node_modules" / "left-pad").mkdir(parents=True)
        (home / "node_modules" / "left-pad" / "index.js").write_text("x")
        (home / ".local" / "share" / "uv").mkdir(parents=True)
        (home / ".local" / "share" / "uv" / "python.bin").write_text("x")
        (d / "logs").mkdir()
        (d / "logs" / "app.log").write_text("noise")
        big = d / "workspace" / "big.bin"
        with open(big, "wb") as f:
            f.truncate(backup.MAX_FILE_BYTES + 1)
        os.symlink("/etc/passwd", d / "workspace" / "link")


class SnapshotTests(BackupBase):
    def test_what_is_and_is_not_saved(self):
        self.populate()
        names = {arc for arc, _ in backup.iter_files()}
        for want in ("users.json", "secret.key", "state/ark.json", "workspace/bot/bot.py",
                     "vm/ark/debian/home/notes.txt", "vm/ark/debian/distro.json",
                     "vm/ark/debian/setup.json"):
            self.assertIn(want, names)
        joined = "\n".join(sorted(names))
        for nope in ("rootfs", "cache", "proot", "node_modules", "uv", "app.log", "big.bin", "link"):
            self.assertNotIn(nope, joined, nope)

    def test_signature_changes_with_content(self):
        self.populate()
        a = backup.signature()
        self.assertEqual(a, backup.signature())
        (self.data / "workspace" / "bot" / "bot.py").write_text("print('changed, longer')\n")
        self.assertNotEqual(a[0], backup.signature()[0])

    def test_problems_explain_what_is_missing(self):
        config.BACKUP_DIR = ""
        self.assertTrue(any("no destination" in p for p in backup.problems()))
        config.BACKUP_DIR = str(self.tmp / "d")
        config.BACKUP_PASSPHRASE = ""
        self.assertTrue(any("PASSPHRASE" in p for p in backup.problems()))
        config.BACKUP_ENCRYPT = False
        self.assertEqual(backup.problems(), [])
        self.assertFalse(backup.configured() is False)


class CryptoTests(BackupBase):
    def test_roundtrip_across_chunks_and_wrong_passphrase(self):
        old = backup.CHUNK
        backup.CHUNK = 1000
        try:
            src, enc, out = self.tmp / "a", self.tmp / "b", self.tmp / "c"
            payload = os.urandom(7777)
            src.write_bytes(payload)
            backup.encrypt_file(src, enc, PASS)
            self.assertTrue(enc.read_bytes().startswith(backup.MAGIC_ENC))
            self.assertNotIn(payload[:64], enc.read_bytes())
            backup.decrypt_file(enc, out, PASS)
            self.assertEqual(out.read_bytes(), payload)
            with self.assertRaises(backup.BackupError) as cm:
                backup.decrypt_file(enc, self.tmp / "x", "wrong passphrase")
            self.assertIn("wrong RCONSOLE_BACKUP_PASSPHRASE", str(cm.exception))
            with self.assertRaises(backup.BackupError):
                backup.decrypt_file(enc, self.tmp / "x", "")
            (self.tmp / "junk").write_bytes(b"not a backup")
            with self.assertRaises(backup.BackupError):
                backup.decrypt_file(self.tmp / "junk", self.tmp / "x", PASS)
        finally:
            backup.CHUNK = old

    def test_two_encryptions_differ(self):
        src = self.tmp / "a"
        src.write_bytes(b"same content")
        backup.encrypt_file(src, self.tmp / "e1", PASS)
        backup.encrypt_file(src, self.tmp / "e2", PASS)
        self.assertNotEqual((self.tmp / "e1").read_bytes(), (self.tmp / "e2").read_bytes())  # random salt


class FolderBackendTests(BackupBase):
    def test_backup_wipe_restore(self):
        self.populate()
        ok, msg = backup.backup_now(force=True)
        self.assertTrue(ok, msg)
        blob = (Path(config.BACKUP_DIR) / backup.BLOB).read_bytes()
        for plain in (b"SECRET-HASH-123", b"sk-TOPSECRET", b"hello bot", b"my notes"):
            self.assertNotIn(plain, blob)                                   # encrypted, really
        for p in list(self.data.iterdir()):                                  # a brand-new instance
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        ok, msg = backup.restore(force=False)
        self.assertTrue(ok, msg)
        self.assertEqual((self.data / "users.json").read_text(), '{"ark": {"password": "SECRET-HASH-123"}}')
        self.assertEqual((self.data / "workspace/bot/bot.py").read_text(), "print('hello bot')\n")
        self.assertEqual((self.data / "vm/ark/debian/home/notes.txt").read_text(), "my notes")
        self.assertEqual(json.loads((self.data / "vm/ark/debian/setup.json").read_text()), ["python"])
        self.assertFalse((self.data / "vm/ark/debian/rootfs").exists())      # system files come back via vm install
        self.assertEqual((self.data / "secret.key").stat().st_mode & 0o777, 0o600)

    def test_unchanged_data_is_not_backed_up_again(self):
        self.populate()
        self.assertIn("backed up", backup.backup_now()[1])
        self.assertIn("no changes", backup.backup_now()[1])
        (self.data / "workspace" / "new.txt").write_text("x")
        self.assertIn("backed up", backup.backup_now()[1])
        self.assertIn("backed up", backup.backup_now(force=True)[1])
        self.assertEqual(backup.status()["count"], 3)

    def test_restore_never_overwrites_on_boot_but_does_when_forced(self):
        self.populate()
        backup.backup_now(force=True)
        (self.data / "workspace/bot/bot.py").write_text("LOCAL EDIT")
        ok, msg = backup.restore(force=False)
        self.assertTrue(ok)
        self.assertIn("already present", msg)
        self.assertEqual((self.data / "workspace/bot/bot.py").read_text(), "LOCAL EDIT")
        backup.restore(force=True)
        self.assertEqual((self.data / "workspace/bot/bot.py").read_text(), "print('hello bot')\n")

    def test_restore_if_fresh_only_on_a_blank_instance(self):
        self.populate()
        backup.backup_now(force=True)
        self.assertIsNone(backup.restore_if_fresh(log=lambda m: None))      # users.json exists
        (self.data / "users.json").unlink()
        shutil.rmtree(self.data / "workspace")
        self.assertTrue(backup.restore_if_fresh(log=lambda m: None))
        self.assertTrue((self.data / "workspace/bot/bot.py").exists())

    def test_wrong_passphrase_on_restore_is_reported(self):
        self.populate()
        backup.backup_now(force=True)
        config.BACKUP_PASSPHRASE = "another passphrase"
        ok, msg = backup.restore(force=True)
        self.assertFalse(ok)
        self.assertIn("wrong RCONSOLE_BACKUP_PASSPHRASE", msg)

    def test_size_limit_names_the_culprit(self):
        self.populate()
        config.BACKUP_MAX_MB = 1
        with open(self.data / "workspace" / "noise.bin", "wb") as f:
            f.write(os.urandom(2 * 1024 * 1024))                              # incompressible
        ok, msg = backup.backup_now(force=True)
        self.assertFalse(ok)
        self.assertIn("limit", msg)
        self.assertIn("workspace", msg)

    def test_unencrypted_mode_is_explicit(self):
        self.populate()
        config.BACKUP_ENCRYPT, config.BACKUP_PASSPHRASE = False, ""
        self.assertTrue(backup.backup_now(force=True)[0])
        self.assertTrue((Path(config.BACKUP_DIR) / backup.BLOB).read_bytes().startswith(backup.MAGIC_PLAIN))
        shutil.rmtree(self.data / "workspace")
        self.assertTrue(backup.restore()[0])
        self.assertTrue((self.data / "workspace/bot/bot.py").exists())

    def test_no_backup_yet_is_not_an_error_crash(self):
        ok, msg = backup.restore()
        self.assertFalse(ok)
        self.assertIn("no backup found", msg)


class HostileArchiveTests(BackupBase):
    def test_restore_ignores_paths_outside_the_allowed_set(self):
        evil = io.BytesIO()
        with tarfile.open(fileobj=evil, mode="w:gz") as tf:
            for name, data in (("../escaped.txt", b"x"), ("/etc/cron.d/evil", b"x"),
                               ("workspace/../../up.txt", b"x"), ("vm/ark/debian/rootfs/etc/passwd", b"x"),
                               ("vm/ark/debian/home/ok.txt", b"fine"), ("other/top.txt", b"x"),
                               ("workspace/ok.txt", b"fine")):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        dest = Path(config.BACKUP_DIR)
        dest.mkdir(parents=True)
        config.BACKUP_ENCRYPT, config.BACKUP_PASSPHRASE = False, ""
        (dest / backup.BLOB).write_bytes(backup.MAGIC_PLAIN + evil.getvalue())
        self.assertTrue(backup.restore(force=True)[0])
        self.assertFalse((self.tmp / "escaped.txt").exists())
        self.assertFalse((self.tmp / "up.txt").exists())
        self.assertFalse((self.data / "other").exists())
        self.assertFalse((self.data / "vm/ark/debian/rootfs").exists())
        self.assertEqual((self.data / "workspace/ok.txt").read_text(), "fine")
        self.assertEqual((self.data / "vm/ark/debian/home/ok.txt").read_text(), "fine")

    def test_allowed_names(self):
        for ok in ("users.json", "state/a.json", "workspace/x/y.txt", "vm/u/debian/home/a/b",
                   "vm/u/debian/distro.json"):
            self.assertTrue(backup._allowed(ok), ok)
        for bad in ("../x", "/abs", "workspace/../x", "vm/u/debian/rootfs/x", "vm/u/debian",
                    "evil.sh", "state", "", "workspace//x"):
            self.assertFalse(backup._allowed(bad), bad)


@unittest.skipUnless(shutil.which("git"), "needs git")
class GitBackendTests(BackupBase):
    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        config.BACKUP_DIR = ""
        config.BACKUP_URL = f"file://{self.remote}"

    def commits(self):
        r = subprocess.run(["git", "-C", str(self.remote), "rev-list", "--count", config.BACKUP_BRANCH],
                           capture_output=True, text=True)
        return int(r.stdout.strip() or 0)

    def test_roundtrip_through_a_git_remote_with_single_commit_history(self):
        self.populate()
        ok, msg = backup.backup_now(force=True)
        self.assertTrue(ok, msg)
        (self.data / "workspace" / "second.txt").write_text("more")
        self.assertTrue(backup.backup_now(force=True)[0])
        self.assertTrue(backup.backup_now(force=True)[0])
        self.assertEqual(self.commits(), 1)                                  # history never accumulates
        listing = subprocess.run(["git", "-C", str(self.remote), "ls-tree", "-r", "--name-only",
                                  config.BACKUP_BRANCH], capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(listing), ["README.md", "backup.rcbk"])
        blob = subprocess.run(["git", "-C", str(self.remote), "show", f"{config.BACKUP_BRANCH}:backup.rcbk"],
                              capture_output=True).stdout
        self.assertNotIn(b"sk-TOPSECRET", blob)
        shutil.rmtree(self.data / "workspace")
        self.assertTrue(backup.restore(force=False)[0])
        self.assertEqual((self.data / "workspace/second.txt").read_text(), "more")

    def test_empty_remote_means_no_backup_yet(self):
        ok, msg = backup.restore()
        self.assertFalse(ok)
        self.assertIn("no backup found", msg)

    def test_token_never_leaks_into_messages(self):
        self.populate()
        config.BACKUP_URL = "https://user:hunter2secret@127.0.0.1:1/x/y.git"
        config.BACKUP_TOKEN = "ghp_SUPERSECRETTOKEN"
        ok, msg = backup.backup_now(force=True)
        self.assertFalse(ok)
        self.assertNotIn("ghp_SUPERSECRETTOKEN", msg)
        self.assertNotIn("hunter2secret", msg)
        self.assertNotIn(PASS, msg)
        ok, msg = backup.restore()
        self.assertFalse(ok)
        self.assertNotIn("ghp_SUPERSECRETTOKEN", msg)
        self.assertNotIn("hunter2secret", backup.describe_target())

    def test_auth_header_is_used_only_for_https(self):
        g = backup.GitBackend("file:///x", token="TOK")
        seen = []
        orig = subprocess.run
        subprocess.run = lambda cmd, **k: seen.append(cmd) or orig(["true"], capture_output=True, text=True)
        try:
            g._git(["status"])
            backup.GitBackend("https://h/x.git", token="TOK")._git(["status"])
        finally:
            subprocess.run = orig
        self.assertFalse(any("extraheader" in " ".join(c) for c in seen[:1]))
        self.assertTrue(any("http.extraheader=Authorization: Basic" in " ".join(c) for c in seen[1:]))


if __name__ == "__main__":
    unittest.main()
