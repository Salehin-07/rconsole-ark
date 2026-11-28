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
        for family in ("debian", "alpine", "arch"):
            for profile in devsetup.PROFILES:
                if profile == "kali" and family != "debian":
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

    def test_docker_profiles_install_udocker_and_the_shim(self):
        banned = {"docker.io", "docker", "docker-engine", "docker-ce", "containerd", "runc", "iptables"}
        for family in ("debian", "alpine"):
            for profile in ("docker", "udocker"):
                pkgs = set(devsetup.packages(profile, family))
                self.assertFalse(pkgs & banned, (profile, family, pkgs & banned))    # never the daemon
                self.assertIn("tar", pkgs)
                if family == "alpine":
                    self.assertIn("findutils", pkgs)          # busybox find breaks udocker's layer fix-up
                text = devsetup.script(profile, family)
                self.assertIn(f"udocker=={devsetup.UDOCKER_VERSION}", text)
                self.assertIn("> /usr/local/bin/docker <<'RCONSOLE_DOCKER_SHIM'", text)
                self.assertIn("> /usr/local/bin/drun <<'RCONSOLE_DRUN'", text)
                self.assertIn("udocker --allow-root install", text)
                self.assertIn("STATIC", text)                     # the VM limit is spelled out
                self.assertNotRegex(text, r"\binst [^\n]*\b(docker\.io|docker-engine)\b")
        # the real client (for DOCKER_HOST) only in `docker`, and never at the shim's path
        self.assertIn("docker-cli", devsetup.packages("docker", "debian"))
        self.assertNotIn("docker-cli", devsetup.packages("udocker", "debian"))
        real = devsetup.script("docker", "debian")
        self.assertIn("download.docker.com/linux/static", real)
        self.assertIn("/usr/local/lib/docker-real/docker", real)
        self.assertNotIn("install -m 0755 /tmp/docker/docker /usr/local/bin/docker", real)
        self.assertNotIn("download.docker.com", devsetup.script("udocker", "debian"))

    def test_embedded_shim_matches_vmtools(self):
        text = devsetup.script("udocker", "alpine")
        self.assertIn((devsetup.VMTOOLS / "docker").read_text(), text)
        self.assertIn((devsetup.VMTOOLS / "drun").read_text(), text)

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


class NodeAndBrowserProfileTests(unittest.TestCase):
    """Node.js, Bun, Chromium and video (Remotion) profiles."""

    def test_new_profiles_are_known_and_expand_in_order(self):
        for name in ("node", "bun", "chromium", "video"):
            self.assertIn(name, devsetup.PROFILES)
        self.assertEqual(devsetup.expand("video"), ["node", "chromium", "video"])
        self.assertEqual(devsetup.expand("chromium"), ["node", "chromium"])
        self.assertNotIn("chromium", devsetup.expand("all"))       # heavy: never part of `all`

    def test_node_is_a_current_lts_from_nodejs_org_with_a_distro_fallback(self):
        text = devsetup.script("node", "debian")
        self.assertIn(f"latest-v{devsetup.NODE_MAJOR}.x", text)
        self.assertIn("sha256sum -c", text)                 # the download is verified
        self.assertIn("inst nodejs npm", text)              # fallback if nodejs.org is unreachable
        self.assertIn(f'-ge {devsetup.NODE_MIN}', text)
        self.assertNotIn("nodejs", devsetup.packages("node", "debian"))   # never the stale apt Node first
        for fam in ("alpine", "arch"):                      # these package a current Node themselves
            self.assertIn("nodejs", devsetup.packages("node", fam))
            self.assertNotIn("nodejs.org/dist", devsetup.script("node", fam))

    def test_npm_is_tuned_for_small_hosts_and_pnpm_is_installed(self):
        for fam in ("debian", "alpine", "arch"):
            text = devsetup.script("node", fam)
            for setting in ("prefix=/root/.npm-global", "fund=false", "progress=false", "maxsockets=4"):
                self.assertIn(setting, text)
            self.assertIn("npm install -g pnpm", text)
            self.assertIn("pnpm --version", text)

    def test_bun_profile(self):
        for fam in ("debian", "alpine", "arch"):
            self.assertIn("bash", devsetup.packages("bun", fam))        # bun's installer needs bash
            self.assertIn("https://bun.sh/install", devsetup.script("bun", fam))
        self.assertIn("libstdc++", devsetup.packages("bun", "alpine"))   # musl build needs it

    def test_chromium_wrapper_adds_the_flags_a_vm_needs(self):
        text = devsetup.script("chromium", "debian")
        self.assertIn("> /usr/local/bin/chromium <<'CHROMIUM_WRAPPER_EOF'", text)
        for flag in ("--no-sandbox", "--disable-dev-shm-usage", "--no-zygote"):
            self.assertIn(flag, text)
        self.assertIn("CHROMIUM_EXTRA_FLAGS", text)
        for var in ("PUPPETEER_EXECUTABLE_PATH", "CHROME_PATH", "PUPPETEER_SKIP_DOWNLOAD",
                    "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"):
            self.assertIn(var, text)
        self.assertIn("--dump-dom about:blank", text)                  # a launch self-test

    def test_chromium_on_ubuntu_avoids_the_snap_stub(self):
        text = devsetup.script("chromium", "debian")
        self.assertIn('[ "${ID:-}" = ubuntu ]', text)
        self.assertIn("@puppeteer/browsers install chrome@stable", text)
        self.assertIn("inst chromium", text)                           # Debian / Kali use the real package
        self.assertNotIn("chromium", devsetup.packages("chromium", "debian"))   # only fonts in the package list

    def test_chromium_packages_on_alpine_and_arch(self):
        self.assertIn("chromium", devsetup.packages("chromium", "alpine"))
        self.assertIn("chromium", devsetup.packages("chromium", "arch"))

    def test_video_has_ffmpeg_chromium_node_and_remotion_hint(self):
        for fam in ("debian", "alpine", "arch"):
            self.assertIn("ffmpeg", devsetup.packages("video", fam))
            text = devsetup.script("video", fam)
            self.assertIn("/usr/local/bin/chromium", text)
            self.assertIn("--browser-executable=/usr/local/bin/chromium", text)
            self.assertIn("--concurrency=1", text)

    def test_wrapper_script_is_valid_sh_and_finds_a_browser(self):
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/chromium"
            with open(path, "w") as f:
                f.write(devsetup._CHROMIUM_WRAPPER)
            self.assertEqual(subprocess.run(["sh", "-n", path]).returncode, 0)
            r = subprocess.run(["sh", path, "--version"], capture_output=True, text=True)
            self.assertIn(r.returncode, (0, 127))                      # 127 only when no browser is installed

    def test_new_profiles_are_documented(self):
        from pathlib import Path
        root = Path(devsetup.__file__).resolve().parent
        commands = (root / "COMMANDS.md").read_text(encoding="utf-8")
        features = (root / "FEATURES.md").read_text(encoding="utf-8")
        for name in devsetup.PROFILES:
            self.assertIn(f"| `{name}` |", commands, name)
            self.assertIn(f"`{name}`", features, name)

