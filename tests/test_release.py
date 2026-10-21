"""Version, changelog and release tooling stay consistent."""
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

import helpers  # noqa: F401

from version import __version__

ROOT = Path(__file__).resolve().parent.parent


class ReleaseTests(unittest.TestCase):
    def test_version_matches_pyproject(self):
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(meta["project"]["version"], __version__)

    def test_version_is_semver(self):
        parts = __version__.split(".")
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(p.isdigit() for p in parts), __version__)

    def test_changelog_has_this_version(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        self.assertIn(f"## {__version__} —", text)

    def test_release_notes_are_complete(self):
        out = subprocess.run([sys.executable, str(ROOT / "scripts/release_notes.py"), __version__,
                              "Someone/some-repo"], capture_output=True, text=True, check=True).stdout
        self.assertIn(f"# rconsole {__version__}", out)
        self.assertIn("### Added", out)
        self.assertIn("### Fixed", out)
        self.assertIn("## Host it yourself", out)
        for asset in (f"rconsole-{__version__}.tar.gz", "docker-compose.yml", "Caddyfile",
                      ".env.example", "SHA256SUMS"):
            self.assertIn(asset, out)
        self.assertIn("releases/download/v" + __version__, out)
        self.assertIn("ghcr.io/someone/some-repo:" + __version__, out)   # image names are lowercase
        self.assertNotIn("## 0.1.0", out)                                # only this version's section

    def test_release_notes_fail_for_unknown_version(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/release_notes.py"), "99.0.0"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_release_inputs_exist(self):
        for rel in ("Dockerfile", "docker-entrypoint.sh", "requirements.txt", "LICENSE", "DEPLOY.md",
                    "COMMANDS.md", "CHANGELOG.md", "deploy/Caddyfile", "deploy/.env.example",
                    "deploy/docker-compose.image.yml", ".github/workflows/release.yml"):
            self.assertTrue((ROOT / rel).exists(), rel)

    def test_workflows_are_valid_yaml_if_pyyaml_is_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        for rel in (".github/workflows/ci.yml", ".github/workflows/release.yml",
                    "deploy/docker-compose.yml", "deploy/docker-compose.image.yml"):
            yaml.safe_load((ROOT / rel).read_text())
        release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        self.assertEqual(set(release["jobs"]), {"ci", "release", "image"})
        self.assertEqual(release["permissions"]["contents"], "write")

    def test_healthz_reports_the_version(self):
        import app as rapp
        rapp.app.config["TESTING"] = True
        body = rapp.app.test_client().get("/healthz").get_json()
        self.assertEqual(body, {"ok": True, "version": __version__})


if __name__ == "__main__":
    unittest.main()
