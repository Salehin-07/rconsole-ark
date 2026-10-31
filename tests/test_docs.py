"""Documentation drift guards: COMMANDS.md must cover every command in the code."""
import re
import unittest
from pathlib import Path

import helpers  # noqa: F401

import commands
import interp

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "COMMANDS.md").read_text(encoding="utf-8")
HEADINGS = [ln for ln in DOC.splitlines() if re.match(r"^#{2,4} ", ln)]


class CommandsDocTests(unittest.TestCase):
    def test_every_command_has_a_heading(self):
        """Adding a command without documenting it fails this test."""
        heading_text = "\n".join(HEADINGS)
        missing = [n for n in sorted(commands.COMMANDS)
                   if f"`{n}`" not in heading_text and f"`{n}` /" not in heading_text]
        self.assertEqual(missing, [], f"undocumented commands: {missing}")

    def test_admin_commands_are_marked_and_mention_sudo(self):
        for name in sorted(commands.PROTECTED):
            idx = next(i for i, ln in enumerate(DOC.splitlines()) if re.match(rf"^### .*`{name}`", ln))
            section = "\n".join(DOC.splitlines()[idx + 1: idx + 9])
            self.assertIn("🔒", section, f"{name} should be marked 🔒 in COMMANDS.md")
        index = DOC.split("## Quick index")[1].split("## Basics")[0]
        for name in sorted(commands.PROTECTED):
            row = next((ln for ln in index.splitlines() if f"`{name}`" in ln), "")
            self.assertIn("🔒", row, f"{name} should be marked 🔒 in the quick index")

    def test_every_interactive_program_is_listed(self):
        section = DOC.split("## Interactive programs")[1].split("## Running anything else")[0]
        missing = [p for p in sorted(interp.INTERACTIVE) if f"`{p}`" not in section]
        self.assertEqual(missing, [], f"interactive programs not documented: {missing}")

    def test_every_vm_subcommand_is_documented(self):
        for sub in ("status", "list", "install", "shell", "run", "use", "mode", "reset", "remove",
                    "setup", "doctor", "help"):
            self.assertIn(f"`vm {sub}", DOC, sub)

    def test_every_theme_is_documented(self):
        import cmd_basic
        for theme in cmd_basic.THEMES:
            self.assertIn(f"`{theme}`", DOC, theme)

    def test_internal_anchors_resolve(self):
        def slug(h):
            t = re.sub(r"^#+\s*", "", h).lower()
            t = re.sub(r"[^\w\s/-]", "", t, flags=re.U).replace("/", "").strip()
            return re.sub(r"\s", "-", t)
        anchors = {slug(h) for h in HEADINGS}
        broken = [a for a in re.findall(r"\]\(#([^)]+)\)", DOC) if a not in anchors]
        self.assertEqual(broken, [], f"broken links in COMMANDS.md: {broken}")


if __name__ == "__main__":
    unittest.main()


class FeaturesDocTests(unittest.TestCase):
    """FEATURES.md, the README and the version must not drift from the code."""

    FEATURES = (ROOT / "FEATURES.md").read_text(encoding="utf-8")

    def test_every_command_is_in_features(self):
        missing = [n for n in sorted(commands.COMMANDS)
                   if not re.search(rf"(?<![\w-])`?{re.escape(n)}\b", self.FEATURES)]
        self.assertEqual(missing, [], f"commands missing from FEATURES.md: {missing}")

    def test_every_setting_is_in_features(self):
        names = set(re.findall(r"RCONSOLE_[A-Z_]+", (ROOT / "config.py").read_text(encoding="utf-8")))
        names -= {"RCONSOLE_"}
        missing = sorted(n for n in names if n not in self.FEATURES and not n.endswith("_"))
        self.assertEqual(missing, [], f"settings missing from FEATURES.md: {missing}")

    def test_version_is_the_same_everywhere(self):
        from version import __version__
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"**Version {__version__}**", readme)
        self.assertIn("rconsole features", self.FEATURES)
        self.assertIn(f"Every feature of rconsole {__version__}", self.FEATURES)
        self.assertTrue((ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split("## ")[1].startswith(__version__))

    def test_every_documented_file_exists(self):
        structure = (ROOT / "structure.md").read_text(encoding="utf-8")
        for name in ("updater.py", "cmd_update.py", "_pathfix.py", "FEATURES.md"):
            self.assertIn(name, structure)
            self.assertTrue((ROOT / name).is_file(), name)

    def test_internal_links_in_features_resolve(self):
        slugs = set()
        for ln in self.FEATURES.splitlines():
            m = re.match(r"^#{2,4} (.+)$", ln)
            if m:
                t = re.sub(r"[`*]", "", m.group(1)).lower()
                t = re.sub(r"[^\w\s-]", "", t)
                slugs.add(re.sub(r"\s", "-", t.strip()))
        for anchor in re.findall(r"\]\(#([^)]+)\)", self.FEATURES):
            self.assertIn(anchor, slugs, f"broken anchor #{anchor}")

