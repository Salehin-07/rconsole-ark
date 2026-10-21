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
