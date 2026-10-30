"""`vm setup` profiles: every generated script must be valid and complete."""
import subprocess
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401

import cmd_vm
import devsetup
import sandbox


class ProfileTests(unittest.TestCase):
    def test_every_profile_every_family_is_valid_sh(self):
        for family in ("debian", "alpine"):
            for profile in devsetup.PROFILES:
                if profile == "kali" and family == "alpine":
                    continue                      # Kali is Debian-family only (tested below)
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

    def test_docker_profile_installs_the_client_and_is_honest_about_the_daemon(self):
        deb = set(devsetup.packages("docker", "debian"))
        for pkg in ("docker.io", "docker-compose"):
            self.assertIn(pkg, deb, pkg)
        alp = set(devsetup.packages("docker", "alpine"))
        for pkg in ("docker", "docker-cli", "docker-cli-compose"):
            self.assertIn(pkg, alp, pkg)
        for family in ("debian", "alpine"):
            text = devsetup.script("docker", family)
            self.assertIn("docker --version", text)
            self.assertIn("dockerd", text)                 # says the daemon cannot run here
            self.assertIn("DOCKER_HOST", text)             # and what to do instead
            self.assertNotIn("psycopg2", text)

    def test_docker_and_kali_are_not_part_of_all(self):
        allp = set(devsetup.packages("all", "debian"))
        self.assertNotIn("docker.io", allp)
        self.assertNotIn("kali-defaults", allp)
        self.assertNotIn("kali-linux-headless", devsetup.script("all", "debian"))
        self.assertIn("docker", devsetup.PROFILES)
        self.assertIn("kali", devsetup.PROFILES)

    def test_kali_profile_installs_the_headless_toolset(self):
        text = devsetup.script("kali", "debian")
        self.assertIn("apt-get install -y kali-linux-headless", text)
        self.assertIn("kali-tools-top10", text)            # fallback if the full set can't be installed
        self.assertIn("policy-rc.d", text)                 # no service start-up inside a VM
        self.assertIn("DEBIAN_FRONTEND=noninteractive", text)
        self.assertIn("rm -f /usr/sbin/policy-rc.d", text)
        with self.assertRaises(ValueError):
            devsetup.script("kali", "alpine")

    def test_kali_is_refused_outside_kali(self):
        self.assertIsNone(devsetup.check_target("kali", "kali"))
        for other in ("debian", "ubuntu", "alpine", None):
            self.assertIn("only for Kali", devsetup.check_target("kali", other))
        for profile in ("python", "docker", "all", "tools"):
            self.assertIsNone(devsetup.check_target(profile, "alpine"))

    def test_vm_setup_kali_refuses_a_debian_vm_and_runs_in_a_kali_one(self):
        ctx = {"sid": "alice"}
        started = []
        with mock.patch.object(sandbox, "resolve"), \
                mock.patch.object(sandbox, "family", return_value="debian"), \
                mock.patch.object(sandbox, "rootfs_path", return_value=__import__("pathlib").Path(tempfile.mkdtemp())), \
                mock.patch.object(sandbox, "record_setup") as rec, \
                mock.patch.object(cmd_vm.jobs, "start_job", side_effect=lambda *a, **k: (started.append(a) or (1, None))):
            with mock.patch.object(sandbox, "distro_info", return_value={"id": "debian"}):
                out = cmd_vm._start_setup(ctx, "debian", "kali")
            self.assertIn("only for Kali", out)
            self.assertEqual(started, [])
            rec.assert_not_called()                        # a refused profile is never re-applied after a restore
            with mock.patch.object(sandbox, "distro_info", return_value={"id": "kali"}):
                out = cmd_vm._start_setup(ctx, "kali", "kali")
            self.assertEqual(out["jobtail"], 1)
            self.assertEqual(len(started), 1)
            self.assertIn("rconsole-setup-kali.sh", started[0][1])
            rec.assert_called_once()

    def test_vm_setup_lists_the_new_profiles(self):
        listing = devsetup.describe()
        self.assertIn("docker", listing)
        self.assertIn("kali", listing)
        self.assertIn("Kali VM only", listing)

    def test_unknown_profile(self):
        with self.assertRaises(ValueError):
            devsetup.script("cobol", "debian")


if __name__ == "__main__":
    unittest.main()
