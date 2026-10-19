"""`vm setup` profiles: every generated script must be valid and complete."""
import subprocess
import tempfile
import unittest

import helpers  # noqa: F401

import devsetup


class ProfileTests(unittest.TestCase):
    def test_every_profile_every_family_is_valid_sh(self):
        for family in ("debian", "alpine"):
            for profile in devsetup.PROFILES:
                text = devsetup.script(profile, family)
                with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
                    f.write(text)
                    f.flush()
                    r = subprocess.run(["sh", "-n", f.name], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{family}/{profile}: {r.stderr}")

    def test_python_profile_can_build_the_usual_suspects(self):
        deb = set(devsetup.packages("python", "debian"))
        # psycopg2 -> libpq-dev, mysqlclient -> mysql dev, Pillow/lxml/cryptography headers
        for pkg in ("python3", "python3-pip", "python3-venv", "python3-dev", "build-essential",
                    "pkg-config", "libpq-dev", "default-libmysqlclient-dev", "libffi-dev",
                    "libssl-dev", "libxml2-dev", "libxslt1-dev", "zlib1g-dev", "libjpeg-dev"):
            self.assertIn(pkg, deb, pkg)
        alp = set(devsetup.packages("python", "alpine"))
        for pkg in ("python3", "py3-pip", "python3-dev", "build-base", "postgresql-dev",
                    "mariadb-dev", "libffi-dev", "openssl-dev", "libxml2-dev", "jpeg-dev",
                    "linux-headers"):
            self.assertIn(pkg, alp, pkg)

    def test_all_contains_every_profile(self):
        allp = set(devsetup.packages("all", "debian"))
        for profile in devsetup.ORDER:
            self.assertTrue(set(devsetup.packages(profile, "debian")) <= allp, profile)

    def test_no_duplicates_and_base_tools_always_present(self):
        for family in ("debian", "alpine"):
            names = devsetup.packages("python", family)
            self.assertEqual(len(names), len(set(names)))
            self.assertIn("git", names)
            self.assertIn("ca-certificates", names)

    def test_script_survives_a_missing_package(self):
        text = devsetup.script("python", "debian")
        self.assertIn("skipped", text)                 # per-package fallback
        self.assertIn("DEBIAN_FRONTEND=noninteractive", text)
        self.assertIn("apk add --no-cache", devsetup.script("python", "alpine"))

    def test_unknown_profile(self):
        with self.assertRaises(ValueError):
            devsetup.script("cobol", "debian")


if __name__ == "__main__":
    unittest.main()
