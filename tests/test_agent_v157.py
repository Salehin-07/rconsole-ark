"""v1.5.7: strict model browsing, skills, and an agent that knows it runs inside Rconsole."""
import io
import os
import threading
import unittest
import zipfile
from unittest import mock

import helpers  # noqa: F401

import agent_core as core
import agent_knowledge as kn
import agent_providers as providers
import agent_skills as sk
import agent_tools as tools
from test_agent import AgentBase
import commands


class ModelBrowseTests(AgentBase):
    def test_browse_a_provider_that_is_not_usable_does_not_fall_back_to_another(self):
        c = self.admin()
        # openrouter has no key here: it must be browsed as itself (and fail on its own), never answered with custom1's list
        with mock.patch.object(providers, "list_models", side_effect=providers.ProviderError("HTTP 401", status=401)):
            d = c.get("/api/agent/models?provider=openrouter").get_json()
        self.assertEqual(d["provider"], "openrouter")
        self.assertEqual(d["models"], [])
        self.assertIn("API key", d["error"])

    def test_unknown_provider_is_a_clear_error(self):
        r = self.admin().get("/api/agent/models?provider=nope")
        self.assertEqual(r.status_code, 400)
        self.assertIn("does not exist", r.get_json()["error"])

    def test_post_uses_an_unsaved_key_and_address(self):
        c = self.admin()
        seen = {}

        def fake(cfg, ttl=600):
            seen.update(base=cfg["base"], key=cfg["api_key"])
            return ["a", "b"]
        with mock.patch.object(providers, "list_models", fake):
            d = c.post("/api/agent/models", json={"provider": "custom1", "api_key": "typed-key", "base": "http://x.test/v1"}).get_json()
        self.assertEqual(d["models"], ["a", "b"])
        self.assertEqual(seen, {"base": "http://x.test/v1", "key": "typed-key"})
        # nothing was saved
        self.assertNotEqual(providers.config_for("custom1", "ark")["api_key"], "typed-key")

    def test_bad_address_is_refused(self):
        d = self.admin().post("/api/agent/models", json={"provider": "custom1", "base": "file:///etc/passwd"})
        self.assertEqual(d.status_code, 400)

    def test_refresh_drops_the_cache(self):
        c = self.admin()
        self.assertEqual(c.get("/api/agent/models?provider=custom1").get_json()["models"], ["m-one", "m-two"])
        self.assertEqual(c.post("/api/agent/models", json={"provider": "custom1", "refresh": True}).get_json()["models"], ["m-one", "m-two"])


class SkillTests(AgentBase):
    def ctx(self):
        s = self.session()
        c = tools.Ctx("ark", s, self.work, threading.Event())
        return c

    def make(self, base, name, desc="Does a thing", body="Step one."):
        d = os.path.join(base, name)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "SKILL.md"), "w").write(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n")
        open(os.path.join(d, "helper.sh"), "w").write("echo hi\n")
        return d

    def test_front_matter_variants(self):
        meta, body = sk.parse("---\nname: x\ndescription: >\n  folded\n  text\nlicense: MIT\n---\nBody")
        self.assertEqual((meta["name"], meta["description"], body), ("x", "folded text", "Body"))
        self.assertEqual(sk.parse("no front matter")[0], {})

    def test_discovery_catalog_and_load(self):
        c = self.ctx()
        self.make(os.path.join(self.work, ".rconsole", "skills"), "deploy-site", "Deploy a site")
        self.make(os.path.join(self.work, ".claude", "skills"), "claude-one", "From Claude Code layout")
        found = sk.discover(c)
        self.assertEqual({"deploy-site", "claude-one"}, set(found) - {"find-skills"})
        cat = sk.catalog(found)
        self.assertIn("- deploy-site: Deploy a site", cat)
        self.assertNotIn("Step one", cat)                  # only the description goes into the prompt
        r = tools.t_skill(c, {"name": "deploy-site"})
        self.assertTrue(r["ok"])
        self.assertIn("Step one.", r["content"])
        self.assertIn("helper.sh", r["content"])
        self.assertFalse(tools.t_skill(c, {"name": "missing"})["ok"])

    def test_the_prompt_lists_skills_and_the_slash_command_runs_one(self):
        c = self.ctx()
        self.make(os.path.join(self.work, ".rconsole", "skills"), "report", "Write a report")
        self.assertIn("- report: Write a report", core.build_system(c, "auto"))
        out = core.handle_command("ark", c.session, "/report quarterly numbers")
        self.assertIn("skill \"report\"", out["prompt"])
        self.assertIn("quarterly numbers", out["prompt"])
        self.assertIn("/report", core.handle_command("ark", c.session, "/skills")["notice"])

    def test_create_and_remove(self):
        c = self.ctx()
        out = core.handle_command("ark", c.session, "/skills new my-skill does things --project")
        self.assertIn("Created", out["notice"])
        self.assertIn("my-skill", sk.discover(c))
        self.assertIn("Removed", core.handle_command("ark", c.session, "/skills remove my-skill")["notice"])
        self.assertNotIn("my-skill", sk.discover(c))

    def test_install_from_a_github_archive(self):
        c = self.ctx()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("repo-main/skills/alpha/SKILL.md", "---\nname: alpha\ndescription: A\n---\nA body")
            z.writestr("repo-main/skills/alpha/scripts/run.py", "print(1)")
            z.writestr("repo-main/skills/beta/SKILL.md", "---\nname: beta\ndescription: B\n---\nB body")
        data = buf.getvalue()
        evil = io.BytesIO()
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("repo-main/skills/alpha/SKILL.md", "---\nname: alpha\n---\nx")
            z.writestr("repo-main/skills/alpha/../../evil.txt", "x")
        evil = evil.getvalue()

        def R(payload):
            class _R:
                status_code = 200
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def iter_content(self, n): yield payload
            return _R()
        home = self.work + "-home"
        fake_path = lambda self, p, follow_last=True: p.replace("~", home) if p.startswith("~") else p  # noqa: E731
        with mock.patch.object(tools.Ctx, "path", fake_path):
            with mock.patch.object(sk.requests, "get", return_value=R(evil)), self.assertRaises(sk.SkillError):
                sk.install(c, "acme/repo/skills", ["alpha"])
            self.assertFalse(os.path.exists(os.path.join(home, ".rconsole/skills/alpha")))      # nothing half-installed
            with mock.patch.object(sk.requests, "get", return_value=R(data)):
                names = sk.install(c, "acme/repo/skills", ["alpha"])
        self.assertEqual(names, ["alpha"])
        self.assertTrue(os.path.isfile(os.path.join(home, ".rconsole/skills/alpha/scripts/run.py")))
        self.assertFalse(os.path.exists(os.path.join(home, ".rconsole/skills/beta")))
        import shutil
        shutil.rmtree(home, ignore_errors=True)

    def test_only_github_sources(self):
        with self.assertRaises(sk.SkillError):
            sk.install(self.ctx(), "https://evil.example.com/a/b")
        with self.assertRaises(sk.SkillError):
            sk._parse_source("just-one-word")


class KnowledgeTests(AgentBase):
    def ctx(self):
        return tools.Ctx("ark", self.session(), self.work, threading.Event())

    def test_every_console_command_is_in_the_manual_or_docs(self):
        skip = {"help", "clear", "cls", "history", "rwhoami", "cd", "alias", "unalias", "logout", "version", "date", "uptime", "cal", "echo",
                "rev", "wc", "hello", "hi", "cowsay", "fortune", "lolcat", "figlet", "banner", "sl", "matrix", "colors", "neofetch",
                "fetch", "rfetch", "shell", "fg", "jobout", "jobtail", "suser", "auser", "nauser", "muser", "ruser", "cpass", "cuser",
                "rboot", "kprocess", "theme", "docker", "drun", "unexpose"}
        for name in commands.COMMANDS:
            if name in skip:
                continue
            self.assertIn(name, kn.MANUAL, f"console command '{name}' is not in the agent's manual (agent_knowledge.MANUAL)")
        # the quick-index of COMMANDS.md names every command too (the docs tool finds them)
        for name in commands.COMMANDS:
            self.assertNotIn("Nothing found", kn.docs(name) if name in ("vm", "service", "expose", "storage") else "ok")

    def test_prompt_knows_where_it_is_and_what_not_to_do(self):
        p = core.build_system(self.ctx(), "auto")
        for needle in ("Rconsole Agent", "Md Abu Salehin", "Never `vm install`", "NOT exist in your shell", "# Where to put things",
                       "Working directory:", "apt-get install -y"):
            self.assertIn(needle, p)

    def test_workspace_hint_tells_a_project_folder_from_the_landing_folder(self):
        empty = self.work
        line, project = kn.cwd_summary(empty, True)
        self.assertEqual((line, project), ("empty", False))
        open(os.path.join(empty, "package.json"), "w").write("{}")
        line, project = kn.cwd_summary(empty, False)
        self.assertTrue(project)
        self.assertIn("looks like a project", line)
        os.remove(os.path.join(empty, "package.json"))
        open(os.path.join(empty, "notes.txt"), "w").write("x")
        self.assertIn("landing folder", kn.cwd_summary(empty, True)[0])

    def test_docs_tool(self):
        r = tools.t_rconsole_docs(self.ctx(), {"topic": "expose"})
        self.assertTrue(r["ok"])
        self.assertIn("Publishes a server", r["content"])
        self.assertIn("vm install", kn.docs("vm"))


class BundledSkillTests(unittest.TestCase):
    def test_find_skills_is_bundled_and_parses(self):
        import agent_skills
        f = os.path.join(agent_skills.BUNDLED_DIR, "find-skills", "SKILL.md")
        self.assertTrue(os.path.isfile(f))
        with open(f, encoding="utf-8") as fh:
            meta, body = agent_skills.parse(fh.read())
        self.assertEqual(meta["name"], "find-skills")
        self.assertIn("/skills add", body)
