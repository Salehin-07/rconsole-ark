"""Cross-document drift guards: the docs must agree with the code and with each other.

Everything here compares a fact stated in the documentation with the code, a config file or another document, so a
contradiction (a wrong default, a stale version, a disk the free blueprint does not have, ...) fails CI instead of
reaching users.  Regenerate the generated tables with ``python scripts/build_docs.py --write``.
"""
import re
import sys
import unittest
from pathlib import Path

import helpers  # noqa: F401

import config

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_docs  # noqa: E402
import envvars  # noqa: E402


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


DOC_FILES = ["README.md", "FEATURES.md", "DEPLOY.md", "FREE_HOSTING.md", "COMMANDS.md", "structure.md",
             "CHANGELOG.md", "SECURITY.md"] + sorted(str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("*.md"))
SKIP_DIRS = {".git", ".venv", "node_modules", "tests", "__pycache__", "site"}
CODE_GLOBS = ("*.py", "*.sh", "*.toml", "*.yaml", "*.yml", "Dockerfile", "Caddyfile", "*.service", "*.example",
              ".env.example")


def code_files():
    out = []
    for pattern in CODE_GLOBS:
        for p in ROOT.rglob(pattern):
            rel = p.relative_to(ROOT)
            if SKIP_DIRS & set(rel.parts) or rel.as_posix() == "scripts/envvars.py" or p.name == "uv.lock":
                continue
            out.append(p)
    return out


class EnvironmentVariableTests(unittest.TestCase):
    def test_registry_matches_the_code_exactly(self):
        used = set()
        for p in code_files():
            used |= set(re.findall(r"\bRCONSOLE_[A-Z0-9_]*[A-Z0-9]\b", p.read_text(encoding="utf-8", errors="ignore")))
        used -= envvars.INTERNAL
        listed = {n for n in envvars.names() if n.startswith("RCONSOLE_")}
        self.assertEqual(sorted(used - listed), [], "read by the code but missing from scripts/envvars.py")
        self.assertEqual(sorted(listed - used), [], "listed in scripts/envvars.py but never read")

    def test_non_prefixed_variables_exist_in_the_code(self):
        blob = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in code_files()
                         ) + "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                                       for p in (ROOT / "vmtools").glob("*"))
        for name in envvars.names():
            if not name.startswith("RCONSOLE_"):
                self.assertRegex(blob, rf"\b{name}\b", f"{name} is documented but not used")

    def test_documents_name_only_real_variables(self):
        real = set(envvars.names()) | envvars.INTERNAL
        for rel in DOC_FILES:
            if rel == "CHANGELOG.md":                 # history may mention names that were later renamed
                continue
            for name in re.findall(r"\bRCONSOLE_[A-Z0-9_]*[A-Z0-9_]\b", read(rel)):
                if name in real:
                    continue
                prefix = name.rstrip("_")
                self.assertTrue(name.endswith("_") and any(r.startswith(name) for r in real),
                                f"{rel} mentions unknown variable {name} ({prefix})")

    def test_generated_blocks_are_current(self):
        self.assertEqual(build_docs.stale_files(), [], "run: python scripts/build_docs.py --write")

    def test_every_registry_variable_has_a_default_and_description(self):
        for area, name, default, desc, scope in envvars.VARS:
            self.assertTrue(default and desc, name)
            self.assertIn(scope, {"all", "docker", "compose", "serve", "vm"}, name)
            self.assertIn(area, dict(envvars.AREAS), name)

    def test_documented_defaults_match_config(self):
        """Spot-check the numbers people rely on against the real defaults."""
        text = envvars.full_reference()
        checks = {
            "RCONSOLE_MAX_USERS": config.MAX_USERS, "RCONSOLE_BACKUP_INTERVAL": config.BACKUP_INTERVAL,
            "RCONSOLE_BACKUP_MAX_MB": config.BACKUP_MAX_MB,
            "RCONSOLE_MAX_UPLOAD_MB": config.MAX_UPLOAD_MB,
        }
        for name, value in checks.items():
            row = next(line for line in text.splitlines() if line.startswith(f"| `{name}`"))
            self.assertIn(f"`{value}`", row, f"{name} default in the docs differs from config ({value})")

    def test_secret_list_in_docs_matches_the_code(self):
        doc = read("docs/configuration.md")
        section = doc.split("## Secrets that never reach your commands")[1].split("Other secrets")[0]
        documented = set(re.findall(r"`(RCONSOLE_[A-Z_]+)`", section))
        self.assertEqual(documented, set(config.SECRET_ENV_VARS))

    def test_secrets_are_stripped_from_child_environments(self):
        import os
        import shellutil
        saved = {k: os.environ.get(k) for k in config.SECRET_ENV_VARS}
        try:
            for k in config.SECRET_ENV_VARS:
                os.environ[k] = "leak"
            env = shellutil.build_env()
            for k in config.SECRET_ENV_VARS:
                self.assertNotIn(k, env, f"{k} must not reach user processes")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class FactsTests(unittest.TestCase):
    def test_version_everywhere(self):
        from version import __version__
        self.assertIn(f'version = "{__version__}"', read("pyproject.toml"))
        self.assertIn(f"**Version {__version__}**", read("README.md"))
        self.assertIn(f"rconsole {__version__}", read("COMMANDS.md"))
        self.assertTrue(read("CHANGELOG.md").split("## ")[1].startswith(__version__))
        lock = read("uv.lock")
        self.assertRegex(lock, rf'name = "rconsole"\nversion = "{re.escape(__version__)}"')

    def test_python_version(self):
        req = re.search(r'requires-python = ">=(\d+\.\d+)"', read("pyproject.toml")).group(1)
        self.assertEqual(read(".python-version").strip().rsplit(".", 0)[0][:len(req)], req)
        self.assertIn(f"python:{req}", read("Dockerfile"))
        self.assertIn(f">= {req}", read("README.md"))
        self.assertIn(req, read("docs/compatibility.md"))
        for rel in ("README.md", "FEATURES.md", "docs/compatibility.md"):
            self.assertNotRegex(read(rel), r"pass(es)? on 3\.12", rel)

    def test_render_blueprints_match_what_the_docs_say(self):
        free, paid = read("render.yaml"), read("render.paid.yaml")
        self.assertIn("plan: free", free)
        self.assertNotRegex(free, r"(?m)^\s*disk:")
        self.assertRegex(paid, r"(?m)^\s*disk:")
        self.assertIn("mountPath: /data", paid)
        self.assertIn("sizeGB: 5", paid)
        deploy = read("DEPLOY.md")
        self.assertNotIn("and a 5 GB disk at `/data`", deploy)
        self.assertIn("render.paid.yaml", deploy)

    def test_backup_limits_in_docs(self):
        import backup
        import lifecycle
        doc = read("docs/backup.md")
        self.assertEqual(backup.MAX_FILE_BYTES, 20 * 1024 * 1024)
        self.assertIn("20 MB", doc)
        self.assertIn(f"{config.BACKUP_MAX_MB} MB", doc)
        self.assertIn(f"{config.BACKUP_INTERVAL // 60} minutes", doc)
        self.assertIn(f"about {lifecycle.FINAL_BACKUP_TIMEOUT} seconds", doc)
        free = read("FREE_HOSTING.md")
        self.assertIn("20 MB", free)
        self.assertIn(f"{config.BACKUP_MAX_MB} MB", free)

    def test_per_user_limits_in_docs(self):
        import exposure
        import services
        arch = read("docs/architecture.md")
        self.assertIn(f"{services.MAX_PER_USER} services and {exposure.MAX_PER_USER} exposures", arch)

    def test_release_assets_are_named_consistently(self):
        wf = read(".github/workflows/release.yml")
        for asset in ("docker-compose.yml", "Caddyfile", "env.example", "SHA256SUMS"):
            self.assertIn(asset, wf)
            self.assertIn(asset, read("scripts/release_notes.py"))
        self.assertIn("env.example", read("DEPLOY.md"))
        self.assertIn("env.example", read("docs/configuration.md"))
        self.assertTrue((ROOT / "deploy" / ".env.example").is_file())

    def test_no_overpromises_survive(self):
        banned = ["never sleeps", "always-on server", "as many as you like", "shared PID namespace",
                  "share the server's uid and PID", "Alpine rootfs", "Services survive redeploys",
                  "doesn't fall asleep", "Open terminals reconnect"]
        for rel in DOC_FILES:
            if rel == "CHANGELOG.md":
                continue
            text = read(rel)
            for phrase in banned:
                self.assertNotIn(phrase, text, f"{rel} still says: {phrase!r}")

    def test_pid_namespace_is_documented_where_it_matters(self):
        for rel in ("README.md", "docs/security.md", "FEATURES.md"):
            self.assertIn("PID namespace", read(rel), rel)
        self.assertIn("RCONSOLE_VM_PIDNS", envvars.full_reference())


class SiteTests(unittest.TestCase):
    def test_site_builds_without_broken_links(self):
        try:
            import markdown  # noqa: F401
        except ImportError:
            self.skipTest("the markdown package is a dev dependency")
        pages = build_docs.build_pages()
        broken = build_docs.rewrite_links(pages)
        self.assertEqual(broken, [], "broken documentation links")
        self.assertEqual(len(pages), len(build_docs.PAGES))

    def test_every_page_source_exists(self):
        for _slug, _title, _group, src, _summary in build_docs.PAGES:
            self.assertTrue((ROOT / build_docs.source_label(src)).is_file(), src)


if __name__ == "__main__":
    unittest.main()
