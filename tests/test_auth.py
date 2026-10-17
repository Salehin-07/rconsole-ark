import hashlib
import json
import unittest

import helpers  # noqa: F401  (must come first)

import auth
import config


class AuthTests(unittest.TestCase):
    def setUp(self):
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()

    def test_seed_and_verify(self):
        self.assertTrue(auth.verify("ark", "ark"))
        self.assertFalse(auth.verify("ark", "wrong"))
        self.assertFalse(auth.verify("ghost", "ark"))
        self.assertTrue(auth.is_admin("ark"))

    def test_users_file_is_private(self):
        self.assertEqual(auth.USERS_FILE.stat().st_mode & 0o777, 0o600)

    def test_password_policy(self):
        ok, err = auth.create_user("bob", "short")
        self.assertFalse(ok)
        self.assertIn("at least", err)
        ok, _ = auth.create_user("bob", "longenough1")
        self.assertTrue(ok)
        self.assertFalse(auth.set_password("bob", "ark")[0])
        self.assertFalse(auth.set_password("ark", "ark")[0])  # default refused

    def test_username_validation(self):
        for bad in ("..", ".", "a/b", "", "x" * 40, ".hidden", "sp ace"):
            self.assertFalse(auth.create_user(bad, "longenough1")[0], bad)
        self.assertTrue(auth.create_user("a.b-c_1", "longenough1")[0])

    def test_legacy_hash_upgrades_on_login(self):
        users = json.loads(auth.USERS_FILE.read_text())
        users["ark"]["password"] = hashlib.sha256(
            ("ark" + auth._LEGACY_PEPPER).encode()).hexdigest()
        auth.USERS_FILE.write_text(json.dumps(users))
        self.assertTrue(auth.verify("ark", "ark"))
        self.assertTrue(json.loads(auth.USERS_FILE.read_text())["ark"]["password"]
                        .startswith("pbkdf2v2$"))
        self.assertTrue(auth.verify("ark", "ark"))

    def test_legacy_pbkdf2_with_old_pepper_upgrades(self):
        salt = "ab" * 16
        old = f"pbkdf2${salt}${auth._derive('ark', salt, auth._LEGACY_PEPPER)}"
        users = json.loads(auth.USERS_FILE.read_text())
        users["ark"]["password"] = old
        auth.USERS_FILE.write_text(json.dumps(users))
        self.assertTrue(auth.verify("ark", "ark"))
        self.assertTrue(auth.get_user("ark")["password"].startswith("pbkdf2v2$"))

    def test_corrupt_store_is_quarantined_not_reseeded(self):
        auth.USERS_FILE.write_text("{not json")
        auth._cache.update(sig=None, users={})
        with self.assertRaises(auth.UserStoreError):
            auth.get_user("ark")
        self.assertFalse(auth.USERS_FILE.exists())
        self.assertTrue(list(config.DATA_DIR.glob("users.corrupt.*")))

    def test_last_admin_protected(self):
        self.assertFalse(auth.set_admin("ark", False)[0])
        self.assertFalse(auth.delete_user("ark")[0])

    def test_throttle_lockout_and_reset(self):
        now = [0.0]
        t = auth.LoginThrottle(max_attempts=3, base=30, cap=100, clock=lambda: now[0])
        k = ("1.2.3.4", "ark")
        for _ in range(2):
            t.failure(k)
        self.assertEqual(t.retry_after(k), 0)
        t.failure(k)
        self.assertEqual(t.retry_after(k), 30)
        now[0] = 31
        self.assertEqual(t.retry_after(k), 0)
        for _ in range(3):
            t.failure(k)
        self.assertEqual(t.retry_after(k), 60)       # doubled
        t.success(k)
        self.assertEqual(t.retry_after(k), 0)
        self.assertEqual(t.retry_after(("5.6.7.8", "ark")), 0)  # isolated per key


if __name__ == "__main__":
    unittest.main()
