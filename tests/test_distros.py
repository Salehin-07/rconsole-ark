"""Distro catalog: choices, aliases and the mirror resolvers (HTTP is faked)."""
import unittest
from unittest import mock

import helpers  # noqa: F401

import distros

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def fake_http(table):
    """Return a replacement for distros._get serving ``table`` (url -> text)."""
    def _get(url, timeout=25):
        for prefix, body in table.items():
            if url == prefix:
                if isinstance(body, Exception):
                    raise body
                return body
        raise distros.DistroError(f"not found: {url}")
    return _get


class ChoiceTests(unittest.TestCase):
    def test_default_and_release(self):
        d, rel = distros.parse_choice(None)
        self.assertEqual((d.id, rel), ("debian", "trixie"))          # Debian is the default
        self.assertEqual(distros.DEFAULT_DISTRO, "debian")
        self.assertEqual(distros.parse_choice("alpine")[1], "latest")
        self.assertEqual(list(distros.CATALOG)[0], "debian")         # listed first
        d, rel = distros.parse_choice("Debian:Bookworm")
        self.assertEqual((d.id, rel), ("debian", "bookworm"))
        self.assertEqual(distros.parse_choice("ubuntu")[1], "lts")
        self.assertEqual(distros.parse_choice("ubuntu:24.04")[1], "24.04")

    def test_aliases_including_bengali(self):
        for alias in ("kali", "Kali-Linux", "kalilinux", "কালী"):
            self.assertEqual(distros.parse_choice(alias)[0].id, "kali", alias)
        self.assertEqual(distros.parse_choice("deb")[0].id, "debian")

    def test_unknown_lists_available(self):
        with self.assertRaises(distros.DistroError) as cm:
            distros.parse_choice("windows")
        for name in ("alpine", "debian", "ubuntu", "kali"):
            self.assertIn(name, str(cm.exception))

    def test_rejects_weird_release(self):
        for bad in ("debian:../../etc", "debian:a b", "debian:" + "x" * 40):
            with self.assertRaises(distros.DistroError):
                distros.parse_choice(bad)

    def test_catalog_is_complete(self):
        self.assertEqual(set(distros.CATALOG), {"alpine", "debian", "ubuntu", "kali"})
        for d in distros.CATALOG.values():
            self.assertIn(d.family, ("alpine", "debian"))
            self.assertTrue(d.summary and d.size and d.release_help)


class ResolverTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch("distros.machine", return_value="x86_64")
        p.start()
        self.addCleanup(p.stop)

    def resolve(self, text, table):
        d, rel = distros.parse_choice(text)
        with mock.patch("distros._get", fake_http(table)):
            return distros.resolve(d, rel)

    def test_sums_parser(self):
        text = f"{SHA_A}  one.tar.gz\n{SHA_B} *two.tar.xz\nnot a line\n"
        self.assertEqual(distros.parse_sums(text),
                         {"one.tar.gz": SHA_A, "two.tar.xz": SHA_B})

    def test_alpine_exact_point_release(self):
        """Regression: `vm install alpine:3.23.5` asked for a branch called v3.23.5 and failed."""
        base = "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/x86_64/"
        name = "alpine-minirootfs-3.23.5-x86_64.tar.gz"
        src = self.resolve("alpine:3.23.5", {base + name + ".sha256": f"{SHA_A}  {name}\n"})
        self.assertEqual(src.url, base + name)
        self.assertEqual(src.sha256, SHA_A)
        self.assertEqual(src.label, "Alpine Linux 3.23.5")
        src = self.resolve("alpine:v3.24.1", {
            "https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/x86_64/"
            "alpine-minirootfs-3.24.1-x86_64.tar.gz.sha256": f"{SHA_B}  x\n"})
        self.assertEqual(src.filename, "alpine-minirootfs-3.24.1-x86_64.tar.gz")

    def test_alpine_release_that_does_not_exist_says_so(self):
        with self.assertRaises(distros.DistroError) as cm:
            self.resolve("alpine:3.23.99", {})
        self.assertIn("does not exist", str(cm.exception))
        self.assertIn("alpine:3.23", str(cm.exception))

    def test_alpine_numbers_that_cannot_exist_get_a_useful_hint(self):
        """Regression: `alpine:1.24.1` said "try alpine:1.24"; Alpine releases are 3.x."""
        for bad, hint in (("alpine:1.24.1", "alpine:3.24.1"), ("alpine:1.22", "alpine:3.22"),
                          ("alpine:v1.12", "alpine:3.12")):
            with self.assertRaises(distros.DistroError) as cm:
                self.resolve(bad, {})
            self.assertIn("there is no Alpine", str(cm.exception))
            self.assertIn(hint, str(cm.exception))
            self.assertIn("numbered 3.x", str(cm.exception))

    def test_alpine_branch_that_is_not_published_explains_the_numbering(self):
        with self.assertRaises(distros.DistroError) as cm:
            self.resolve("alpine:3.99", {})
        self.assertIn("does not exist", str(cm.exception))
        self.assertIn("alpine:3.24", str(cm.exception))

    def test_a_version_after_a_system_name_belongs_to_that_system(self):
        j = distros.join_release_args
        self.assertEqual(j(["alpine", "3.23.5"]), ["alpine:3.23.5"])
        self.assertEqual(j(["debian", "alpine", "v3.24"]), ["debian", "alpine:v3.24"])
        self.assertEqual(j(["alpine:3.22", "3.23"]), ["alpine:3.22", "3.23"])      # already has one
        self.assertEqual(j(["3.23.5"]), ["3.23.5"])                                # nothing to attach to
        self.assertEqual(j(["all"]), ["all"])
        self.assertEqual(j([]), [])

    def test_alpine_latest_version(self):
        yaml = """---
-
  file: alpine-minirootfs-3.24.3-x86_64.tar.gz
  sha256: %s
  flavor: alpine-minirootfs
""" % SHA_A
        base = "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64/"
        with mock.patch.object(distros, "_get", fake_http({base + "latest-releases.yaml": yaml})), \
                mock.patch.object(distros, "machine", return_value="x86_64"):
            self.assertEqual(distros.alpine_latest_version(), "3.24.3")

    def test_alpine(self):
        yaml = f"""---
-
  file: alpine-minirootfs-3.24.2-x86_64.tar.gz
  sha256: {SHA_A}
  flavor: alpine-minirootfs
"""
        base = "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64/"
        src = self.resolve("alpine", {base + "latest-releases.yaml": yaml})
        self.assertEqual(src.sha256, SHA_A)
        self.assertEqual(src.url, base + "alpine-minirootfs-3.24.2-x86_64.tar.gz")
        self.assertIn("3.24.2", src.label)
        pinned = "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/"
        src = self.resolve("alpine:3.21", {pinned + "latest-releases.yaml": yaml})
        self.assertTrue(src.url.startswith(pinned))

    def test_ubuntu_lts_falls_back_when_newest_is_missing(self):
        old = "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/"
        sums = (f"{SHA_A} *ubuntu-base-24.04.3-base-arm64.tar.gz\n"
                f"{SHA_B} *ubuntu-base-24.04.3-base-amd64.tar.gz\n")
        src = self.resolve("ubuntu", {old + "SHA256SUMS": sums})   # 26.04 -> 404 -> 24.04
        self.assertEqual(src.sha256, SHA_B)
        self.assertEqual(src.filename, "ubuntu-base-24.04.3-base-amd64.tar.gz")
        self.assertEqual(src.label, "Ubuntu 24.04")

    def test_ubuntu_prefers_newest_lts(self):
        new = "https://cdimage.ubuntu.com/ubuntu-base/releases/26.04/release/"
        src = self.resolve("ubuntu", {new + "SHA256SUMS": f"{SHA_C} *ubuntu-base-26.04-base-amd64.tar.gz\n"})
        self.assertEqual(src.label, "Ubuntu 26.04")

    def test_ubuntu_explicit_release_does_not_fall_back(self):
        with self.assertRaises(distros.DistroError):
            self.resolve("ubuntu:20.04", {})

    def test_debian_picks_latest_build_and_encodes_colon(self):
        base = "https://images.linuxcontainers.org/images/debian/trixie/amd64/default/"
        listing = ('<a href="../">..</a><a href="20260918_05%3A24/">x</a>'
                   '<a href="20260919_05%3A24/">y</a><a href="current/">z</a>')
        build = base + "20260919_05%3A24/"
        src = self.resolve("debian", {
            base: listing,
            build + "SHA256SUMS": f"{SHA_A}  rootfs.tar.xz\n{SHA_B}  lxd.tar.xz\n",
        })
        self.assertEqual(src.url, build + "rootfs.tar.xz")
        self.assertEqual(src.sha256, SHA_A)
        self.assertIn("20260919", src.filename)
        self.assertEqual(src.label, "Debian (trixie)")

    def test_debian_uses_second_mirror(self):
        mirror2 = "https://uk.lxd.images.canonical.com/images/debian/bookworm/amd64/default/"
        build = mirror2 + "20260101_00%3A01/"
        src = self.resolve("debian:bookworm", {
            mirror2: '<a href="20260101_00%3A01/">b</a>',
            build + "SHA256SUMS": f"{SHA_C}  rootfs.tar.xz\n",
        })
        self.assertTrue(src.url.startswith(mirror2))

    def test_kali_minimal_and_legacy_names(self):
        base = "https://kali.download/nethunter-images/current/rootfs/"
        new = f"{SHA_A}  kali-nethunter-rootfs-minimal-amd64.tar.xz\n{SHA_B}  kali-nethunter-rootfs-full-amd64.tar.xz\n"
        src = self.resolve("kali", {base + "SHA256SUMS": new})
        self.assertEqual(src.sha256, SHA_A)
        legacy = f"{SHA_C}  kalifs-amd64-minimal.tar.xz\n"
        self.assertEqual(self.resolve("kali", {base + "SHA256SUMS": legacy}).sha256, SHA_C)
        full = self.resolve("kali:full", {base + "SHA256SUMS": new})
        self.assertEqual(full.sha256, SHA_B)

    def test_kali_falls_back_to_lxc_image(self):
        lxc = "https://images.linuxcontainers.org/images/kali/current/amd64/default/"
        build = lxc + "20260920_00%3A00/"
        src = self.resolve("kali", {
            "https://kali.download/nethunter-images/current/rootfs/SHA256SUMS":
                distros.DistroError("down"),
            lxc: '<a href="20260920_00%3A00/">b</a>',
            build + "SHA256SUMS": f"{SHA_A}  rootfs.tar.xz\n",
        })
        self.assertTrue(src.url.startswith(lxc))

    def test_unsupported_cpu(self):
        with mock.patch("distros.machine", return_value="riscv64"):
            d, rel = distros.parse_choice("debian")
            with self.assertRaises(distros.DistroError):
                distros.resolve(d, rel)

    def test_arm64_names(self):
        with mock.patch("distros.machine", return_value="aarch64"):
            base = "https://cdimage.ubuntu.com/ubuntu-base/releases/26.04/release/"
            sums = f"{SHA_A} *ubuntu-base-26.04-base-arm64.tar.gz\n{SHA_B} *ubuntu-base-26.04-base-amd64.tar.gz\n"
            d, rel = distros.parse_choice("ubuntu")
            with mock.patch("distros._get", fake_http({base + "SHA256SUMS": sums})):
                self.assertEqual(distros.resolve(d, rel).sha256, SHA_A)

    def test_hints_match_package_manager(self):
        self.assertIn("apk add", distros.python_setup_hint("alpine"))
        self.assertIn("apt update", distros.python_setup_hint("debian"))
        self.assertIn("python3-pip", distros.python_setup_hint("debian"))


if __name__ == "__main__":
    unittest.main()
