"""Arch Linux as a VM system: resolver, proot tweaks, setup profiles, update and dependency restore."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

import devsetup
import distros
import sandbox
import vmdeps
import vmupdate

SHA = "a" * 64
LXC = "https://images.linuxcontainers.org/images/archlinux/current/amd64/default/"


def fake_http(table):
    def _get(url, timeout=25):
        if url in table:
            if isinstance(table[url], Exception):
                raise table[url]
            return table[url]
        raise distros.DistroError(f"not found: {url}")
    return _get


class CatalogTests(unittest.TestCase):
    def test_arch_is_listed_with_its_aliases(self):
        d, rel = distros.parse_choice("arch")
        self.assertEqual((d.id, d.family, d.pkg, rel), ("arch", "arch", "pacman", "current"))
        for alias in ("archlinux", "Arch-Linux"):
            self.assertEqual(distros.parse_choice(alias)[0].id, "arch")
        self.assertEqual(distros.parse_choice("arch:rolling")[1], "rolling")

    def test_resolves_the_newest_build_with_its_published_checksum(self):
        build = LXC + "20261105_04%3A18/"
        table = {LXC: '<a href="20261101_04%3A18/">x</a><a href="20261105_04%3A18/">y</a>',
                 build + "SHA256SUMS": f"{SHA}  rootfs.tar.xz\n"}
        d, rel = distros.parse_choice("arch")
        with mock.patch("distros._get", fake_http(table)), \
                mock.patch("distros.machine", return_value="x86_64"):
            src = distros.resolve(d, rel)
        self.assertEqual(src.url, build + "rootfs.tar.xz")
        self.assertEqual(src.sha256, SHA)
        self.assertEqual(src.label, "Arch Linux (current)")
        self.assertIn("archlinux", src.filename)

    def test_arm64_uses_the_arm64_image(self):
        base = LXC.replace("amd64", "arm64")
        build = base + "20261105_04%3A18/"
        table = {base: '<a href="20261105_04%3A18/">y</a>', build + "SHA256SUMS": f"{SHA}  rootfs.tar.xz\n"}
        d, rel = distros.parse_choice("arch")
        with mock.patch("distros._get", fake_http(table)), \
                mock.patch("distros.machine", return_value="aarch64"):
            self.assertTrue(distros.resolve(d, rel).url.startswith(base))

    def test_there_is_no_numbered_release(self):
        d, _ = distros.parse_choice("arch:2024.01")
        with self.assertRaises(distros.DistroError) as cm:
            distros.resolve(d, "2024.01")
        self.assertIn("rolling", str(cm.exception))

    def test_hints_use_pacman(self):
        self.assertIn("pacman -Syu --noconfirm", distros.python_setup_hint("arch"))
        self.assertIn("python-pip", distros.python_setup_hint("arch"))


class ProotTweakTests(unittest.TestCase):
    CONF = ("[options]\nHoldPkg = pacman glibc\n#DownloadUser = alpm\nDownloadUser = alpm\n"
            "CheckSpace\n#DisableSandbox\nSigLevel = Required DatabaseOptional\n"
            "[core]\nInclude = /etc/pacman.d/mirrorlist\n")

    def make_root(self, mirrors="## comment\n#Server = https://x/$repo\n"):
        root = Path(tempfile.mkdtemp())
        (root / "etc/pacman.d").mkdir(parents=True)
        (root / "etc/pacman.conf").write_text(self.CONF)
        (root / "etc/pacman.d/mirrorlist").write_text(mirrors)
        return root

    def test_pacman_is_made_to_work_without_a_real_kernel(self):
        root = self.make_root()
        sandbox._arch_post_setup(root)
        conf = (root / "etc/pacman.conf").read_text().splitlines()
        self.assertIn("#CheckSpace", conf)
        self.assertIn("#DownloadUser = alpm", conf)
        self.assertNotIn("DownloadUser = alpm", conf)
        self.assertIn("DisableSandbox", conf)
        self.assertIn("SigLevel = Required DatabaseOptional", conf)      # signatures stay ON
        self.assertIn("Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch",
                      (root / "etc/pacman.d/mirrorlist").read_text())

    def test_is_idempotent_and_keeps_a_working_mirrorlist(self):
        root = self.make_root("Server = https://mine.example/$repo/os/$arch\n")
        sandbox._arch_post_setup(root)
        first = (root / "etc/pacman.conf").read_text()
        sandbox._arch_post_setup(root)
        self.assertEqual(first, (root / "etc/pacman.conf").read_text())
        self.assertEqual((root / "etc/pacman.d/mirrorlist").read_text(),
                         "Server = https://mine.example/$repo/os/$arch\n")

    def test_arm_gets_the_arm_mirror(self):
        root = self.make_root("")
        with mock.patch("distros.machine", return_value="aarch64"):
            sandbox._arch_post_setup(root)
        self.assertIn("mirror.archlinuxarm.org", (root / "etc/pacman.d/mirrorlist").read_text())

    def test_systemd_hooks_become_no_ops(self):
        root = self.make_root()
        sandbox._arch_post_setup(root)
        for name in sandbox.PACMAN_NOOP_HOOKS:
            text = (root / "etc/pacman.d/hooks" / name).read_text()
            self.assertIn("Exec = /usr/bin/true", text)
            self.assertIn("[Trigger]", text)

    def test_post_setup_is_chosen_by_family(self):
        root = self.make_root()
        sandbox._post_setup(root, distros.CATALOG["arch"])
        self.assertTrue((root / "etc/pacman.d/hooks").is_dir())
        self.assertFalse((root / "etc/apt").exists())                    # no Debian tweaks on Arch

    def test_pacman_shim_is_valid_sh_and_inits_keys_only_when_installing(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
            f.write(sandbox.PACMAN_SHIM_TEXT)
            f.flush()
            self.assertEqual(subprocess.run(["sh", "-n", f.name]).returncode, 0)
        text = sandbox.PACMAN_SHIM_TEXT
        self.assertIn("-S*|-U*", text)
        self.assertNotIn("-Q", text.split("case")[1].split("esac")[0])    # queries never touch keys
        self.assertTrue(text.rstrip().endswith('exec /usr/bin/pacman "$@"'))


class SetupProfileTests(unittest.TestCase):
    def test_python_profile_has_the_headers_for_the_usual_suspects(self):
        pk = set(devsetup.packages("python", "arch"))
        for pkg in ("python", "python-pip", "base-devel", "postgresql-libs", "mariadb-libs",
                    "libffi", "openssl", "libxml2", "libxslt", "libjpeg-turbo", "zlib"):
            self.assertIn(pkg, pk)

    def test_script_upgrades_first_and_never_asks(self):
        text = devsetup.script("tools", "arch")
        self.assertIn("pacman -Syu --noconfirm", text)
        self.assertIn("pacman -S --noconfirm --needed", text)
        self.assertNotIn("apt-get", text)
        self.assertIn("skipped $p", text)                 # one missing package never sinks the rest

    def test_docker_profile_does_not_pull_in_a_daemon(self):
        pk = set(devsetup.packages("docker", "arch"))
        self.assertNotIn("docker", pk)
        self.assertIn("findutils", pk)                    # udocker needs GNU find
        self.assertIn("download.docker.com", devsetup.script("docker", "arch"))   # static client

    def test_kali_stays_kali_only(self):
        with self.assertRaises(ValueError):
            devsetup.script("kali", "arch")
        self.assertIn("kali", devsetup.check_target("kali", "arch"))
        self.assertIsNone(devsetup.check_target("python", "arch"))


class UpdateTests(unittest.TestCase):
    def test_commands_are_a_full_non_interactive_upgrade(self):
        c = vmupdate.commands_for("arch", "arch")
        self.assertEqual(c["refresh"], "pacman -Sy --noconfirm")
        self.assertIn("pacman -Su --noconfirm", c["upgrade"])
        self.assertIn("db.lck", c["health"])
        self.assertIn("pacman -Dk", c["verify"])

    def test_count_lines_of_pacman_qu(self):
        out = "glibc 2.42-1 -> 2.43-1\nlinux 6.1.arch1-1 -> 6.2.arch1-1 [ignored]\n"
        self.assertEqual(vmupdate.parse_count("arch", out), 2)
        self.assertEqual(vmupdate.parse_count("arch", ""), 0)

    def test_count_command_treats_nothing_to_upgrade_as_success(self):
        cmd = vmupdate.commands_for("arch")["count"]
        for code, ok in ((0, True), (1, True), (2, False)):
            r = subprocess.run(["sh", "-c", f"pacman() {{ return {code}; }}; {cmd}"],
                               capture_output=True)
            self.assertEqual(r.returncode == 0, ok, code)

    def test_pacman_database_is_snapshotted_as_real_copies(self):
        self.assertIn("var/lib/pacman", vmupdate.COPY_DIRS)

    def test_arch_has_no_release_to_move_to(self):
        with mock.patch.object(sandbox, "distro_info", return_value={"family": "arch"}):
            self.assertIsNone(vmupdate.plan_release("alice", "arch"))


class DependencyTests(unittest.TestCase):
    CAPTURED = "@@captured\n@@pacman\nnano\nbase-devel\n@@pyversion\n3.13.1\n@@pip\nrequests==2.32.0\n"

    def test_manifest_records_pacman_packages(self):
        man = vmdeps.parse(self.CAPTURED)
        self.assertEqual(man["family"], "arch")
        self.assertEqual(man["system"], {"manager": "pacman", "packages": ["base-devel", "nano"]})

    def test_restore_reinstalls_them_with_pacman(self):
        script = vmdeps.restore_script(vmdeps.parse(self.CAPTURED))
        self.assertIn("pacman -Syu --noconfirm --needed base-devel nano", script)
        self.assertNotIn("apt-get", script)
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
            f.write(script)
            f.flush()
            self.assertEqual(subprocess.run(["sh", "-n", f.name]).returncode, 0)

    def test_capture_script_asks_pacman_for_native_explicit_packages(self):
        self.assertIn("pacman -Qqen", vmdeps.CAPTURE)


if __name__ == "__main__":
    unittest.main()
