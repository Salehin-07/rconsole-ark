"""neofetch/fetch: responsive layouts and target awareness."""
import unittest

import helpers  # noqa: F401

import auth
import cmd_fetch
import cmd_vm


def widths(text):
    return [cmd_fetch.visible_len(l) for l in text.split("\n")]


class LayoutTests(unittest.TestCase):
    info = ["\x1b[1muser\x1b[0m@rconsole", "-----------",
            "OS: \x1b[34mrconsole (linux)\x1b[0m",
            "Memory: \x1b[34m47% (1872/3997 MB) plus a very long trailing detail\x1b[0m"]

    def test_never_wider_than_the_screen(self):
        for cols in (200, 100, 60, 42, 40, 30, 24, 20):
            out = cmd_fetch.layout(cmd_fetch.LOGO, self.info, cols)
            self.assertLessEqual(max(widths(out)), cols, f"cols={cols}")

    def test_wide_screens_put_logo_beside_info(self):
        wide = cmd_fetch.layout(cmd_fetch.LOGO, self.info, 100)
        self.assertEqual(len(wide.split("\n")), len(cmd_fetch.LOGO))      # side by side
        self.assertIn("██", wide.split("\n")[0])
        self.assertIn("user", wide.split("\n")[0])

    def test_phone_widths_drop_the_tall_logo(self):
        phone = cmd_fetch.layout(cmd_fetch.LOGO, self.info, 36)
        self.assertNotIn("██", phone)                  # a 10-row logo would bury the info
        self.assertEqual(len(phone.split("\n")), len(self.info))

    def test_short_logo_is_stacked_when_it_fits(self):
        short = ["  /\\", " /__\\"]
        out = cmd_fetch.layout(short, self.info, 36)
        self.assertEqual(len(out.split("\n")), len(short) + len(self.info))

    def test_truncate_keeps_colours_balanced(self):
        s = "\x1b[34m" + "x" * 50 + "\x1b[0m"
        t = cmd_fetch.truncate(s, 10)
        self.assertEqual(cmd_fetch.visible_len(t), 10)
        self.assertTrue(t.rstrip().endswith("\x1b[0m"))
        self.assertIn("~", t)
        self.assertEqual(cmd_fetch.truncate("short", 10), "short")

    def test_no_logo_flag(self):
        out = cmd_fetch.rconsole_fetch({"username": "ark", "sid": "ark", "cols": 100},
                                       ["--no-logo"])
        self.assertNotIn("██", out)


class VmListTests(unittest.TestCase):
    def setUp(self):
        helpers.wipe_vms()
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()

    def test_lists_every_system(self):
        out = cmd_vm._list("ark", 80)
        for name in ("alpine", "debian", "ubuntu", "kali"):
            self.assertIn(name, out)
        self.assertIn("vm install debian", out)
        self.assertIn("Install one", out)

    def test_wraps_to_phone_width(self):
        for cols in (80, 40, 34):
            out = cmd_vm._list("ark", cols)
            self.assertLessEqual(max(len(l) for l in out.split("\n")), max(cols, 24), cols)

    def test_command_entry_point_and_aliases(self):
        ctx = {"sid": "ark", "username": "ark", "is_admin": True, "cols": 40, "tab": {"cwd": "/"}}
        for sub in ("list", "ls", "distros", "images", "available", "options"):
            self.assertIn("Systems you can install", cmd_vm.cmd_vm(ctx, [sub]))

    def test_install_validation_is_immediate(self):
        ctx = {"sid": "ark", "username": "ark", "is_admin": True, "cols": 80, "tab": {"cwd": "/"}}
        out = cmd_vm.cmd_vm(ctx, ["install", "windows"])
        self.assertIn("unknown system", out)
        self.assertIn("kali", out)


class CommandTests(unittest.TestCase):
    def setUp(self):
        helpers.wipe_vms()
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()
        auth.create_user("alice", "longenough1")

    def ctx(self, user, cols=80):
        return {"username": user, "sid": user, "is_admin": auth.is_admin(user), "cols": cols,
                "tab": {"cwd": "/"}}

    def test_host_mode_shows_rconsole(self):
        out = cmd_fetch.cmd_neofetch(self.ctx("ark"), [])
        self.assertIn("rconsole", out)
        self.assertIn("Target:", out)

    def test_vm_mode_without_vm_explains_how_to_get_one(self):
        out = cmd_fetch.cmd_neofetch(self.ctx("alice"), [])      # non-admins are VM-only
        self.assertIn("vm install", out)
        self.assertIn("vm list", out)

    def test_rfetch_is_always_rconsole(self):
        out = cmd_fetch.cmd_rfetch(self.ctx("alice"), [])
        self.assertNotIn("No VM yet", out)

    def test_fetch_aliases_registered(self):
        import commands
        for name in ("neofetch", "fetch", "rfetch"):
            self.assertIn(name, commands.COMMANDS)


if __name__ == "__main__":
    unittest.main()
