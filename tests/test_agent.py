"""The AI agent: providers (streaming, keys), tools, sessions, the engine loop, permissions, the HTTP API."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers  # noqa: F401

import agent_core as core
import agent_providers as providers
import agent_store as store
import agent_tools as tools
import app as rapp
import config
import sandbox
from test_services import Base


class Fake(ThreadingHTTPServer):
    """A tiny OpenAI-compatible server: pops one scripted reply per chat request."""
    daemon_threads = True

    def __init__(self):
        self.script, self.requests = [], []
        super().__init__(("127.0.0.1", 0), self.Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"data": [{"id": "m-one"}, {"id": "m-two"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            self.server.requests.append((self.path, self.headers.get("Authorization"), body))
            step = self.server.script.pop(0) if self.server.script else {"text": "done"}
            if self.path.endswith("/v1/messages"):
                return self.anthropic(step)
            if step.get("status"):
                self.send_response(step["status"])
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": step.get("message", "boom")}}).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send(obj):
                self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                self.wfile.flush()
            for piece in step.get("chunks") or ([step["text"]] if step.get("text") else []):
                send({"choices": [{"delta": {"content": piece}}]})
            for i, tc in enumerate(step.get("calls") or []):
                args = json.dumps(tc["args"])
                send({"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"c{i}-{len(self.server.requests)}",
                                                              "function": {"name": tc["name"], "arguments": args[:5]}}]}}]})
                send({"choices": [{"delta": {"tool_calls": [{"index": i, "function": {"arguments": args[5:]}}]}}]})
            send({"choices": [{"delta": {}, "finish_reason": "tool_calls" if step.get("calls") else "stop"}],
                  "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
            self.wfile.write(b"data: [DONE]\n\n")


def _anthropic(self, step):
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream")
    self.end_headers()

    def ev(name, obj):
        self.wfile.write(f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode())
        self.wfile.flush()
    ev("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 50, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 0}}})
    i = 0
    for piece in step.get("chunks") or ([step["text"]] if step.get("text") else []):
        ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
        ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": piece}})
        i += 1
    for tc in step.get("calls") or []:
        ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": "tu1", "name": tc["name"], "input": {}}})
        raw = json.dumps(tc["args"])
        ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": raw[:4]}})
        ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": raw[4:]}})
        i += 1
    ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use" if step.get("calls") else "end_turn"}, "usage": {"output_tokens": 9}})
    ev("message_stop", {"type": "message_stop"})


Fake.Handler.anthropic = _anthropic


class AgentBase(Base):
    @classmethod
    def setUpClass(cls):
        cls.fake = Fake()

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.fake.server_close()

    def setUp(self):
        super().setUp()
        self.fake.script, self.fake.requests = [], []
        core.SHUTTING.clear()
        providers._path().unlink(missing_ok=True)
        providers.save_provider("custom1", {"name": "Fake", "kind": "openai", "base": self.fake.base,
                                            "model": "fake-model", "api_key": "sk-test-key-123456", "needs_key": True})
        providers.set_default("custom1")
        self.work = tempfile.mkdtemp(prefix="agent-work-")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self._mode = sandbox.state.get_mode("ark")
        sandbox.state.set_mode("ark", "host")

    def tearDown(self):
        super().tearDown()
        tools.kill_background()

    def session(self, mode="auto"):
        return store.create("ark", self.work, provider="custom1", model="fake-model", mode=mode)

    def turn(self, sess, text, answers=None, timeout=20):
        """Run a turn to the end; auto-answer questions from ``answers`` (list). Returns the events."""
        run = core.start("ark", sess, text)
        evs, seq, end = [], 0, time.time() + timeout
        answers = list(answers or [])
        while time.time() < end:
            new, done, _r = run.follow(seq, 0.5)
            for e in new:
                seq = e["seq"]
                evs.append(e)
                if e["t"] == "ask":
                    core.answer("ark", sess.id, e["id"], answers.pop(0) if answers else "Deny")
            if done:
                break
        return evs


class ProviderTests(AgentBase):
    def test_catalogue_has_the_requested_providers(self):
        ids = {r["id"] for r in providers.public_table("ark")}
        for want in ("openrouter", "huggingface", "openai", "anthropic", "groq", "gemini", "deepseek", "ollama", "opencode"):
            self.assertIn(want, ids)
        hf = next(r for r in providers.public_table("ark") if r["id"] == "huggingface")
        self.assertIn("HF_TOKEN", hf["env"])
        self.assertTrue(hf["base"].startswith("https://router.huggingface.co"))

    def test_keys_never_leave_the_server(self):
        rows = providers.public_table("ark")
        self.assertNotIn("sk-test-key-123456", json.dumps(rows))
        row = next(r for r in rows if r["id"] == "custom1")
        self.assertTrue(row["has_key"])
        self.assertEqual(row["key_source"], "dashboard")

    def test_streams_text_and_tool_calls(self):
        self.fake.script = [{"chunks": ["Hel", "lo"], "calls": [{"name": "ls", "args": {"path": "."}}]}]
        seen = []
        cfg = providers.resolve("ark")
        res = providers.stream_chat(cfg, "sys", [{"role": "user", "content": "hi"}], tools.specs("ask"),
                                    on_text=seen.append)
        self.assertEqual("".join(seen), "Hello")
        self.assertEqual(res["content"], "Hello")
        self.assertEqual(res["tool_calls"][0]["name"], "ls")
        self.assertEqual(res["tool_calls"][0]["args"], {"path": "."})
        self.assertEqual(res["usage"]["input"], 100)
        self.assertEqual(self.fake.requests[0][1], "Bearer sk-test-key-123456")

    def test_error_text_is_explained_and_scrubbed(self):
        self.fake.script = [{"status": 401, "message": "bad key sk-test-key-123456"}]
        with self.assertRaises(providers.ProviderError) as cm:
            providers.stream_chat(providers.resolve("ark"), "", [{"role": "user", "content": "x"}], [])
        self.assertNotIn("sk-test-key-123456", str(cm.exception))
        self.assertIn("key", str(cm.exception).lower())

    def test_retries_a_busy_server(self):
        self.fake.script = [{"status": 503}, {"text": "ok"}]
        old = providers.time.sleep
        providers.time.sleep = lambda s: old(0.01)
        try:
            res = providers.stream_chat(providers.resolve("ark"), "", [{"role": "user", "content": "x"}], [])
        finally:
            providers.time.sleep = old
        self.assertEqual(res["content"], "ok")

    def test_env_key_makes_a_preset_usable(self):
        os.environ["OPENROUTER_API_KEY"] = "or-key-abcdef123"
        try:
            row = next(r for r in providers.public_table("ark") if r["id"] == "openrouter")
            self.assertTrue(row["usable"])
            self.assertEqual(row["key_source"], "environment")
        finally:
            del os.environ["OPENROUTER_API_KEY"]

    def test_save_validates_and_removes(self):
        with self.assertRaises(ValueError):
            providers.save_provider("bad id!", {"base": "ftp://x"})
        with self.assertRaises(ValueError):
            providers.save_provider("mine", {"base": "not a url"})
        providers.save_provider("huggingface", {"api_key": "hf_abcdef123456", "model": "Qwen/Qwen3-Coder-30B-A3B-Instruct:fastest"})
        self.assertTrue(next(r for r in providers.public_table("ark") if r["id"] == "huggingface")["usable"])
        self.assertTrue(providers.remove_provider("custom1"))

    def test_list_models_and_test(self):
        cfg = providers.resolve("ark")
        self.assertEqual(providers.list_models(cfg, ttl=0), ["m-one", "m-two"])
        self.fake.script = [{"text": "ok"}]
        r = providers.test(cfg)
        self.assertTrue(r["ok"], r)

    def test_anthropic_streaming_tools_headers_and_caching(self):
        providers.save_provider("anthropic", {"api_key": "sk-ant-test-123456", "base": self.fake.base[:-3], "model": "claude-test"})
        cfg = providers.resolve("ark", "anthropic")
        self.assertEqual(cfg["kind"], "anthropic")
        self.fake.script = [{"chunks": ["Hi", " there"], "calls": [{"name": "ls", "args": {"path": "."}}]}]
        seen = []
        res = providers.stream_chat(cfg, "SYS", [{"role": "user", "content": "go"}], tools.specs("ask"), on_text=seen.append)
        self.assertEqual("".join(seen), "Hi there")
        self.assertEqual(res["tool_calls"][0]["args"], {"path": "."})
        self.assertEqual(res["usage"], {"input": 80, "output": 9, "cached": 30})
        path, auth, body = self.fake.requests[-1]
        self.assertTrue(path.endswith("/v1/messages"))
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["messages"][-1]["content"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertIn("input_schema", body["tools"][0])
        self.assertIsNone(auth)                                  # Anthropic uses x-api-key, not a Bearer token

    def test_anthropic_message_conversion_merges_tool_results(self):
        msgs = [{"role": "user", "content": "a"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "ls", "args": {}}, {"id": "2", "name": "ls", "args": {}}]},
                {"role": "tool", "tool_call_id": "1", "name": "ls", "content": "x"},
                {"role": "tool", "tool_call_id": "2", "name": "ls", "content": "y"}]
        out = providers._an_messages(msgs)
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user"])
        self.assertEqual(len(out[2]["content"]), 2)


class ToolTests(AgentBase):
    def ctx(self):
        s = self.session()
        c = tools.Ctx("ark", s, self.work, threading.Event())
        return c

    def test_read_write_edit_flow(self):
        c = self.ctx()
        p = os.path.join(self.work, "a.txt")
        self.assertTrue(tools.t_write(c, {"path": p, "content": "one\ntwo\nthree\n"})["ok"])
        r = tools.t_read(c, {"path": p, "offset": 2, "limit": 1})
        self.assertIn("two", r["content"])
        self.assertNotIn("one", r["content"])
        self.assertTrue(tools.t_edit(c, {"path": p, "old_string": "two", "new_string": "2"})["ok"])
        self.assertEqual(open(p).read(), "one\n2\nthree\n")
        bad = tools.t_edit(c, {"path": p, "old_string": "nope", "new_string": "x"})
        self.assertFalse(bad["ok"])
        self.assertIn("not found", bad["content"])

    def test_edit_needs_a_prior_read_and_unique_match(self):
        c = self.ctx()
        p = os.path.join(self.work, "b.txt")
        open(p, "w").write("x\nx\n")
        self.assertIn("first", tools.t_edit(c, {"path": p, "old_string": "x", "new_string": "y"})["content"])
        tools.t_read(c, {"path": p})
        r = tools.t_edit(c, {"path": p, "old_string": "x", "new_string": "y"})
        self.assertIn("2 places", r["content"])
        self.assertTrue(tools.t_edit(c, {"path": p, "old_string": "x", "new_string": "y", "replace_all": True})["ok"])
        self.assertEqual(open(p).read(), "y\ny\n")

    def test_overwrite_needs_a_read_and_stale_edit_is_refused(self):
        c = self.ctx()
        p = os.path.join(self.work, "c.txt")
        open(p, "w").write("hello\n")
        self.assertFalse(tools.t_write(c, {"path": p, "content": "new"})["ok"])
        tools.t_read(c, {"path": p})
        time.sleep(0.02)
        open(p, "w").write("changed behind its back\n")
        r = tools.t_edit(c, {"path": p, "old_string": "changed", "new_string": "x"})
        self.assertIn("changed since", r["content"])

    def test_multi_edit_is_atomic(self):
        c = self.ctx()
        p = os.path.join(self.work, "d.txt")
        open(p, "w").write("alpha beta gamma\n")
        tools.t_read(c, {"path": p})
        r = tools.t_edit(c, {"path": p, "edits": [{"old_string": "alpha", "new_string": "A"}, {"old_string": "zzz", "new_string": "Z"}]})
        self.assertFalse(r["ok"])
        self.assertEqual(open(p).read(), "alpha beta gamma\n")
        r = tools.t_edit(c, {"path": p, "edits": [{"old_string": "alpha", "new_string": "A"}, {"old_string": "gamma", "new_string": "G"}]})
        self.assertTrue(r["ok"])
        self.assertEqual(open(p).read(), "A beta G\n")

    def test_crlf_is_preserved(self):
        c = self.ctx()
        p = os.path.join(self.work, "e.txt")
        open(p, "wb").write(b"a\r\nb\r\n")
        tools.t_read(c, {"path": p})
        self.assertTrue(tools.t_edit(c, {"path": p, "old_string": "a\nb", "new_string": "A\nB"})["ok"])
        self.assertEqual(open(p, "rb").read(), b"A\r\nB\r\n")

    def test_grep_glob_ls(self):
        c = self.ctx()
        os.makedirs(os.path.join(self.work, "src", "node_modules"))
        open(os.path.join(self.work, "src", "m.py"), "w").write("def foo():\n    return 1\n")
        open(os.path.join(self.work, "src", "node_modules", "x.py"), "w").write("def foo(): pass\n")
        g = tools.t_grep(c, {"pattern": r"def \w+", "path": self.work})
        self.assertIn("src/m.py:1:def foo", g["content"])
        self.assertNotIn("node_modules", g["content"])
        self.assertIn("src/m.py", tools.t_glob(c, {"pattern": "**/*.py", "path": self.work})["content"])
        self.assertIn("src/", tools.t_ls(c, {"path": self.work})["content"])
        self.assertEqual(tools.t_grep(c, {"pattern": "(", "path": self.work})["ok"], False)

    def test_binary_and_missing_files(self):
        c = self.ctx()
        p = os.path.join(self.work, "bin")
        open(p, "wb").write(b"\x00\x01\x02")
        self.assertIn("binary", tools.t_read(c, {"path": p})["content"])
        self.assertFalse(tools.t_read(c, {"path": os.path.join(self.work, "nope")})["ok"])

    def test_server_secrets_are_off_limits(self):
        c = self.ctx()
        for name in ("secret.key", "users.json"):
            with self.assertRaises(tools.ToolError):
                c.path(str(config.DATA_DIR / name))
        with self.assertRaises(tools.ToolError):
            c.path(str(config.DATA_DIR / "agent" / "providers.json"))

    def test_bash_output_exit_code_and_cwd(self):
        c = self.ctx()
        r = tools.t_bash(c, {"command": "echo hi; echo err >&2; exit 3"})
        self.assertFalse(r["ok"])
        self.assertIn("hi", r["content"])
        self.assertIn("err", r["content"])
        self.assertIn("exit code 3", r["content"])
        sub = os.path.join(self.work, "sub")
        os.mkdir(sub)
        r = tools.t_bash(c, {"command": "cd sub && pwd"})
        self.assertTrue(r["ok"])
        self.assertEqual(os.path.realpath(c.cwd), os.path.realpath(sub))
        self.assertNotIn("__RCPWD_", r["content"])

    def test_bash_timeout_and_cancel(self):
        c = self.ctx()
        t0 = time.time()
        r = tools.t_bash(c, {"command": "sleep 30", "timeout": 1})
        self.assertFalse(r["ok"])
        self.assertIn("killed", r["content"])
        self.assertLess(time.time() - t0, 10)
        c2 = self.ctx()
        threading.Timer(0.5, c2.cancel.set).start()
        t0 = time.time()
        r = tools.t_bash(c2, {"command": "sleep 30"})
        self.assertIn("stopped", r["content"])
        self.assertLess(time.time() - t0, 10)

    def test_bash_large_output_is_clipped(self):
        c = self.ctx()
        r = tools.t_bash(c, {"command": "seq 1 60000"})
        self.assertTrue(r["ok"])
        self.assertLess(len(r["content"]), tools.RESULT_CAP + 400)
        self.assertTrue("omitted" in r["content"], f"len={len(r['content'])} rc={r['ui'].get('rc')} err={r['ui'].get('error')} tail={r['content'][-40:]!r}")
        self.assertTrue("60000" in r["content"], "the tail of the output must survive")

    def test_the_end_of_a_huge_single_write_is_kept(self):
        # one 300 KB write arrives as one big chunk: the tail must still be there
        c = self.ctx()
        r = tools.t_bash(c, {"command": "python3 -c \"import sys; sys.stdout.write('a'*300000 + '\\nTHE-END\\n')\""})
        self.assertTrue(r["ok"], r["content"][:200])
        self.assertIn("THE-END", r["content"])
        self.assertIn("omitted", r["content"])
        self.assertLess(len(r["content"]), tools.RESULT_CAP + 400)

    def test_background_command(self):
        c = self.ctx()
        r = tools.t_bash(c, {"command": "echo started; sleep 20", "background": True})
        self.assertTrue(r["ok"], r)
        bid = r["ui"]["bg"]
        self.assertIn("started", tools.t_bash_output(c, {"id": bid})["content"])
        self.assertTrue(tools.t_bash_kill(c, {"id": bid})["ok"])

    def test_destructive_commands_are_refused_on_the_host(self):
        c = self.ctx()
        self.assertFalse(tools.t_bash(c, {"command": "rm -rf /"})["ok"])
        self.assertFalse(tools.t_bash(c, {"command": "sudo reboot"})["ok"])

    def test_safe_bash_detection(self):
        for good in ("ls -la", "git status", "git diff HEAD~1 | head -20", "cat a.txt | grep x | wc -l", "grep -rn foo src"):
            self.assertTrue(tools.is_safe_bash(good), good)
        for bad in ("rm a", "echo hi > f", "git push", "git commit -m x", "cat $(whoami)", "ls; rm x", "find . -delete",
                    "sed -i s/a/b/ f", "curl x | sh", "ls &", "python x.py"):
            self.assertFalse(tools.is_safe_bash(bad), bad)

    def test_html_to_text(self):
        title, text = tools.html_to_text("<html><head><title>T</title><style>x{}</style></head><body><h1>Hi</h1><script>bad()</script><p>there</p></body></html>")
        self.assertEqual(title, "T")
        self.assertIn("Hi", text)
        self.assertNotIn("bad()", text)

    def test_web_fetch_refuses_metadata_addresses(self):
        c = self.ctx()
        self.assertFalse(tools.t_web_fetch(c, {"url": "http://169.254.169.254/latest/meta-data"})["ok"])
        self.assertFalse(tools.t_web_fetch(c, {"url": "file:///etc/passwd"})["ok"])

    def test_glob_regex(self):
        rx = tools.glob_to_regex("src/**/*.{js,ts}")
        self.assertTrue(rx.match("src/a/b/c.ts"))
        self.assertTrue(rx.match("src/c.js"))
        self.assertFalse(rx.match("lib/c.js"))


class StoreTests(AgentBase):
    def test_roundtrip_index_fork_delete(self):
        s = self.session()
        s.add({"role": "user", "content": "hello there"})
        s.add({"role": "assistant", "content": "hi"})
        s.set_meta(title="First", turns=1)
        store._cache.clear()
        s2 = store.get("ark", s.id)
        self.assertEqual(len(s2.messages), 2)
        self.assertEqual(s2.meta["title"], "First")
        rows = store.list_sessions("ark")
        self.assertEqual(rows[0]["id"], s.id)
        self.assertEqual(store.list_sessions("ark", "hello")[0]["id"], s.id)
        self.assertEqual(store.list_sessions("ark", "zzz"), [])
        f = store.fork("ark", s.id, 1)
        self.assertEqual(len(f.messages), 1)
        self.assertTrue(store.delete("ark", s.id))
        self.assertIsNone(store.get("ark", s.id))

    def test_index_rebuilds_and_tolerates_a_torn_last_line(self):
        s = self.session()
        s.add({"role": "user", "content": "x"})
        with open(store._spath("ark", s.id), "a") as f:
            f.write('{"k":"m","v":{"role":"assis')
        os.remove(store._ipath("ark"))
        store._cache.clear()
        self.assertEqual(store.list_sessions("ark")[0]["id"], s.id)
        self.assertEqual(len(store.get("ark", s.id).messages), 1)

    def test_ids_cannot_escape(self):
        self.assertFalse(store.valid_id("../../etc/passwd"))
        self.assertIsNone(store.get("ark", "../x"))
        self.assertFalse(store.delete("ark", "../x"))

    def test_other_users_cannot_see_a_chat(self):
        s = self.session()
        self.assertIsNone(store.get("alice", s.id))
        self.assertEqual(store.list_sessions("alice"), [])


class EngineTests(AgentBase):
    def test_a_plain_answer_streams_and_is_saved(self):
        self.fake.script = [{"chunks": ["Hello ", "world"]}]
        s = self.session()
        evs = self.turn(s, "say hi")
        self.assertEqual("".join(e["d"] for e in evs if e["t"] == "text"), "Hello world")
        self.assertEqual(evs[-1]["t"], "done")
        self.assertEqual(evs[-1]["reason"], "stop")
        self.assertEqual([m["role"] for m in s.messages], ["user", "assistant"])
        self.assertEqual(s.meta["title"], "say hi")
        self.assertEqual(s.meta["tokens_in"], 100)

    def test_tool_loop_writes_a_file_and_undo_restores_it(self):
        target = os.path.join(self.work, "new.txt")
        self.fake.script = [{"calls": [{"name": "write", "args": {"path": target, "content": "hello\n"}}]},
                            {"text": "created it"}]
        s = self.session("auto")
        evs = self.turn(s, "make a file")
        self.assertEqual(open(target).read(), "hello\n")
        kinds = [e["t"] for e in evs]
        self.assertIn("tool", kinds)
        self.assertIn("tool_end", kinds)
        self.assertEqual(evs[-1]["undo"], 1)
        # the second request contains the tool result
        second = self.fake.requests[1][2]["messages"]
        self.assertEqual(second[-1]["role"], "tool")
        self.assertIn("Created", second[-1]["content"])
        out = core.undo("ark", s)
        self.assertFalse(os.path.exists(target))
        self.assertIn("removed", out)

    def test_ask_mode_requires_approval_and_deny_is_reported(self):
        target = os.path.join(self.work, "x.txt")
        self.fake.script = [{"calls": [{"name": "write", "args": {"path": target, "content": "x"}}]}, {"text": "ok, not writing"}]
        s = self.session("ask")
        evs = self.turn(s, "write x", answers=["Deny"])
        self.assertTrue(any(e["t"] == "ask" and e["kind"] == "permission" for e in evs))
        self.assertFalse(os.path.exists(target))
        self.assertIn("denied", self.fake.requests[1][2]["messages"][-1]["content"])

    def test_always_allow_remembers_the_command(self):
        self.fake.script = [{"calls": [{"name": "bash", "args": {"command": "touch one"}}]},
                            {"calls": [{"name": "bash", "args": {"command": "touch two"}}]}, {"text": "done"}]
        s = self.session("ask")
        evs = self.turn(s, "run", answers=["Always allow"])
        asks = [e for e in evs if e["t"] == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(sum(1 for e in evs if e["t"] == "tool_end" and e["ok"]), 2)

    def test_read_only_commands_need_no_approval(self):
        self.fake.script = [{"calls": [{"name": "bash", "args": {"command": "echo hi | wc -c"}}]}, {"text": "done"}]
        s = self.session("ask")
        evs = self.turn(s, "count")
        self.assertFalse(any(e["t"] == "ask" for e in evs))

    def test_plan_mode_is_read_only_and_plan_approval_switches_mode(self):
        self.fake.script = [{"calls": [{"name": "bash", "args": {"command": "touch no"}}]},
                            {"calls": [{"name": "exit_plan_mode", "args": {"plan": "1. do it"}}]}, {"text": "starting"}]
        s = self.session("plan")
        evs = self.turn(s, "plan it", answers=["Start, edit files automatically"])
        names = [t["function"]["name"] for t in self.fake.requests[0][2]["tools"]]
        self.assertNotIn("bash", names)
        self.assertIn("exit_plan_mode", names)
        self.assertFalse(os.path.exists(os.path.join(self.work, "no")))
        self.assertEqual(s.meta["mode"], "edit")
        self.assertTrue(any(e["t"] == "mode" for e in evs))

    def test_ask_user_gets_the_answer(self):
        self.fake.script = [{"calls": [{"name": "ask_user", "args": {"question": "which?", "options": ["a", "b"]}}]}, {"text": "ok"}]
        s = self.session()
        self.turn(s, "go", answers=["b"])
        self.assertIn("b", self.fake.requests[1][2]["messages"][-1]["content"])

    def test_todos_are_saved_and_emitted(self):
        self.fake.script = [{"calls": [{"name": "todo_write", "args": {"todos": [{"content": "a", "status": "in_progress"}]}}]}, {"text": "ok"}]
        s = self.session()
        evs = self.turn(s, "go")
        self.assertTrue(any(e["t"] == "todos" for e in evs))
        self.assertEqual(s.meta["todos"][0]["content"], "a")

    def test_stop_cancels_a_running_command(self):
        self.fake.script = [{"calls": [{"name": "bash", "args": {"command": "sleep 30"}}]}, {"text": "x"}]
        s = self.session("auto")
        run = core.start("ark", s, "sleep")
        time.sleep(1.5)
        run.cancel.set()
        t0 = time.time()
        while not run.done and time.time() - t0 < 12:
            time.sleep(0.1)
        self.assertTrue(run.done)
        self.assertEqual(run.events[-1]["reason"], "cancel")
        # tool results stay paired with their calls so the chat can go on
        roles = [m["role"] for m in s.messages]
        self.assertEqual(roles.count("tool"), sum(len(m.get("tool_calls") or []) for m in s.messages))

    def test_provider_error_is_reported_not_raised(self):
        self.fake.script = [{"status": 401, "message": "nope"}]
        s = self.session()
        evs = self.turn(s, "hi")
        err = next(e for e in evs if e["t"] == "error")
        self.assertIn("401", err["m"])
        self.assertEqual(evs[-1]["reason"], "error")

    def test_no_provider_gives_setup_hint(self):
        providers.remove_provider("custom1")
        s = store.create("ark", self.work, mode="auto")
        evs = self.turn(s, "hi")
        self.assertEqual(next(e for e in evs if e["t"] == "error")["action"], "setup")

    def test_repeated_identical_calls_are_stopped(self):
        call = {"calls": [{"name": "ls", "args": {"path": "."}}]}
        self.fake.script = [call] * 8
        s = self.session()
        evs = self.turn(s, "loop")
        self.assertIn(evs[-1]["reason"], ("loop", "stop"))
        self.assertLess(len(self.fake.requests), 8)

    def test_mentioned_files_are_inlined(self):
        open(os.path.join(self.work, "note.txt"), "w").write("secret plan\n")
        self.fake.script = [{"text": "read it"}]
        s = self.session()
        self.turn(s, "summarise @note.txt")
        sent = self.fake.requests[0][2]["messages"][-1]["content"]
        self.assertIn("secret plan", sent)
        self.assertEqual(s.messages[0]["shown"], "summarise @note.txt")

    def test_project_instructions_reach_the_system_prompt(self):
        open(os.path.join(self.work, "AGENTS.md"), "w").write("Always use tabs.\n")
        self.fake.script = [{"text": "ok"}]
        s = self.session()
        self.turn(s, "hi")
        self.assertIn("Always use tabs.", self.fake.requests[0][2]["messages"][0]["content"])

    def test_context_is_pruned_then_compacted(self):
        s = self.session()
        big = "x" * 4000
        for i in range(30):
            s.add({"role": "user", "content": f"task {i} " + big})
            s.add({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}", "name": "ls", "args": {}}]})
            s.add({"role": "tool", "tool_call_id": f"t{i}", "name": "ls", "content": big})
        providers.save_provider("custom1", {"context": 20000})
        self.fake.script = [{"text": "SUMMARY of the work"}, {"text": "final"}]
        evs = self.turn(s, "continue")
        self.assertTrue(any(e["t"] == "compacted" for e in evs), [e["t"] for e in evs])
        self.assertIsNotNone(s.compact)
        sent = self.fake.requests[-1][2]["messages"]
        self.assertTrue(any("SUMMARY of the work" in (m.get("content") or "") for m in sent))
        self.assertLess(len(sent), 40)
        self.assertGreaterEqual(len(s.messages), 91)                # the transcript itself is never rewritten

    def test_slash_commands(self):
        s = self.session("ask")
        self.assertIn("Commands", core.handle_command("ark", s, "/help")["notice"])
        self.assertEqual(core.handle_command("ark", s, "/mode auto")["refresh"], True)
        self.assertEqual(s.meta["mode"], "auto")
        self.assertEqual(core.handle_command("ark", s, "/new")["action"], "new")
        self.assertIn("Unknown", core.handle_command("ark", s, "/nope")["notice"])
        self.assertIsNone(core.handle_command("ark", s, "/usr/bin/python is broken"))
        self.assertIsNone(core.handle_command("ark", s, "hello"))
        self.assertTrue(core.handle_command("ark", s, "/init")["prompt"])
        os.makedirs(os.path.join(self.work, ".rconsole", "commands"))
        open(os.path.join(self.work, ".rconsole", "commands", "review.md"), "w").write("# Review code\nReview $ARGUMENTS carefully.")
        self.assertEqual(core.handle_command("ark", s, "/review src/x.py")["prompt"].strip().split("\n")[-1], "Review src/x.py carefully.")

    def test_concurrency_limit(self):
        providers.set_options({"max_runs": 1})
        self.fake.script = [{"calls": [{"name": "bash", "args": {"command": "sleep 5"}}]}, {"text": "x"}]
        s1, s2 = self.session("auto"), self.session("auto")
        r1 = core.start("ark", s1, "a")
        with self.assertRaises(core.Busy):
            core.start("ark", s2, "b")
        with self.assertRaises(core.Busy):
            core.start("ark", s1, "again")
        r1.cancel.set()
        t0 = time.time()
        while not r1.done and time.time() - t0 < 12:
            time.sleep(0.1)


class ApiTests(AgentBase):
    def test_admins_only(self):
        self.assertEqual(rapp.app.test_client().get("/api/agent/state").status_code, 401)
        self.assertEqual(self.user().get("/api/agent/state").status_code, 403)
        self.assertEqual(self.user().get("/agent").status_code, 403)
        self.assertEqual(rapp.app.test_client().get("/agent").status_code, 302)
        self.assertEqual(self.admin().get("/agent").status_code, 200)

    def test_new_chat_send_stream_and_transcript(self):
        c = self.admin()
        st = c.get("/api/agent/state").get_json()
        self.assertTrue(st["configured"])
        sid = c.post("/api/agent/session", json={"cwd": self.work}).get_json()["id"]
        self.fake.script = [{"chunks": ["Hi ", "there"]}]
        r = c.post(f"/api/agent/session/{sid}/send", json={"text": "hello"}).get_json()
        self.assertTrue(r["started"])
        body = c.get(f"/api/agent/session/{sid}/stream?after=0").get_data(as_text=True)
        self.assertIn('"t": "text"', body)
        self.assertIn("event: end", body)
        # reconnect from the middle: only later events
        seqs = [int(x.split(":")[1]) for x in body.splitlines() if x.startswith("id:")]
        again = c.get(f"/api/agent/session/{sid}/stream?after={seqs[-2]}").get_data(as_text=True)
        self.assertIn('"t": "done"', again)
        self.assertNotIn('"t": "text"', again)
        d = c.get(f"/api/agent/session/{sid}").get_json()
        self.assertEqual([m["role"] for m in d["messages"]], ["user", "assistant"])
        self.assertEqual(d["messages"][1]["content"], "Hi there")
        self.assertEqual(d["meta"]["title"], "hello")
        self.assertTrue(any(x["name"] == "undo" for x in d["commands"]))
        lst = c.get("/api/agent/sessions").get_json()["sessions"]
        self.assertEqual(lst[0]["id"], sid)

    def test_the_stream_is_never_gzipped(self):
        # a browser sends Accept-Encoding: gzip; compressing an event stream would hold it back until the turn ended
        c = self.admin()
        sid = c.post("/api/agent/session", json={"cwd": self.work}).get_json()["id"]
        self.fake.script = [{"text": "hi"}]
        c.post(f"/api/agent/session/{sid}/send", json={"text": "hello"})
        r = c.get(f"/api/agent/session/{sid}/stream?after=0", headers={"Accept-Encoding": "gzip, deflate"})
        self.assertEqual(r.mimetype, "text/event-stream")
        self.assertIsNone(r.headers.get("Content-Encoding"))
        self.assertIn("event: end", r.get_data(as_text=True))

    def test_slash_command_through_the_api_and_busy_state(self):
        c = self.admin()
        sid = c.post("/api/agent/session", json={"cwd": self.work}).get_json()["id"]
        r = c.post(f"/api/agent/session/{sid}/send", json={"text": "/mode plan"}).get_json()
        self.assertFalse(r["started"])
        self.assertEqual(c.get(f"/api/agent/session/{sid}").get_json()["meta"]["mode"], "plan")
        self.fake.script = [{"calls": [{"name": "ask_user", "args": {"question": "q?"}}]}, {"text": "x"}]
        c.post(f"/api/agent/session/{sid}/meta", json={"mode": "auto"})
        c.post(f"/api/agent/session/{sid}/send", json={"text": "go"})
        time.sleep(0.8)
        self.assertEqual(c.post(f"/api/agent/session/{sid}/send", json={"text": "again"}).status_code, 409)
        run = core.get_run("ark", sid)
        ask = next(e for e in run.events if e["t"] == "ask")
        self.assertTrue(c.post(f"/api/agent/session/{sid}/answer", json={"id": ask["id"], "value": "yes"}).get_json()["ok"])
        run.thread.join(10)
        self.assertTrue(run.done)

    def test_meta_rename_cwd_delete_fork_export(self):
        c = self.admin()
        sid = c.post("/api/agent/session", json={"cwd": self.work}).get_json()["id"]
        m = c.post(f"/api/agent/session/{sid}/meta", json={"title": "Renamed", "cwd": self.work}).get_json()
        self.assertEqual(m["title"], "Renamed")
        self.assertEqual(c.post(f"/api/agent/session/{sid}/meta", json={"cwd": "/definitely/not/here"}).status_code, 400)
        self.assertEqual(c.post(f"/api/agent/session/{sid}/meta", json={"mode": "bogus"}).status_code, 400)
        self.assertEqual(c.post(f"/api/agent/session/{sid}/meta", json={"provider": "huggingface"}).status_code, 400)
        fid = c.post(f"/api/agent/session/{sid}/fork", json={}).get_json()["id"]
        self.assertNotEqual(fid, sid)
        ex = c.get(f"/api/agent/session/{sid}/export")
        self.assertIn("attachment", ex.headers["Content-Disposition"])
        self.assertTrue(c.post(f"/api/agent/session/{sid}/delete").get_json()["ok"])
        self.assertEqual(c.get(f"/api/agent/session/{sid}").status_code, 404)

    def test_models_and_file_suggestions(self):
        c = self.admin()
        self.assertEqual(c.get("/api/agent/models").get_json()["models"], ["m-one", "m-two"])
        sid = c.post("/api/agent/session", json={"cwd": self.work}).get_json()["id"]
        open(os.path.join(self.work, "readme.md"), "w").write("x")
        self.assertIn("readme.md", c.get(f"/api/agent/files?s={sid}&q=read").get_json()["files"])

    def test_console_command_opens_the_page(self):
        c = self.admin()
        out = c.post("/api/run", json={"cmd": "sudo agent fix the bug", "tab": "main"}).get_json()
        self.assertTrue(out["redirect"].startswith("/agent?"), out)
        self.assertIn("q=fix%20the%20bug", out["redirect"])
        out = c.post("/api/run", json={"cmd": "sudo agent providers", "tab": "main"}).get_json()
        self.assertIn("custom1", out["output"])

    def test_headless_print_mode(self):
        c = self.admin()
        self.fake.script = [{"text": "42"}]
        out = c.post("/api/run", json={"cmd": "sudo agent -p what is six times seven", "tab": "main"}).get_json()
        self.assertIn("42", out["output"])


class DashboardTests(AgentBase):
    def act(self, c, action, **kw):
        r = c.post("/api/dashboard/action", json={"action": action, **kw})
        return r.status_code, r.get_json()

    def test_view_lists_providers_and_never_a_key(self):
        c = self.admin()
        d = c.get("/api/dashboard/data?section=agent").get_json()
        ids = [p["id"] for p in d["providers"]]
        for want in ("openrouter", "huggingface", "custom1"):
            self.assertIn(want, ids)
        self.assertNotIn("sk-test-key-123456", json.dumps(d))
        self.assertEqual(d["default"], "custom1")
        self.assertEqual(d["options"]["mode"], "ask")
        self.assertEqual(self.user().get("/api/dashboard/data?section=agent").status_code, 403)

    def test_save_a_key_for_openrouter_and_huggingface(self):
        c = self.admin()
        for pid, key in (("openrouter", "or-secret-key-999"), ("huggingface", "hf_secret_token_999")):
            code, j = self.act(c, "agent.save", id=pid, api_key=key, model="some/model")
            self.assertEqual(code, 200, j)
            self.assertNotIn(key, json.dumps(j))
        d = c.get("/api/dashboard/data?section=agent").get_json()
        rows = {p["id"]: p for p in d["providers"]}
        self.assertTrue(rows["openrouter"]["usable"] and rows["huggingface"]["usable"])
        self.assertEqual(rows["huggingface"]["model"], "some/model")
        self.assertNotIn("hf_secret_token_999", json.dumps(d))
        self.assertEqual(oct(os.stat(providers._path()).st_mode & 0o777), "0o600")
        code, _j = self.act(c, "agent.default", id="openrouter")
        self.assertEqual(code, 200)
        self.assertEqual(providers.resolve("ark")["id"], "openrouter")
        self.assertEqual(self.act(c, "agent.default", id="groq")[0], 400)               # no key
        # a saved key can be replaced, kept (omitted) or removed
        self.act(c, "agent.save", id="openrouter", model="other/model")
        self.assertEqual(providers.resolve("ark", "openrouter")["api_key"], "or-secret-key-999")
        self.act(c, "agent.save", id="openrouter", clear_key=True)
        self.assertFalse(next(r for r in providers.public_table("ark") if r["id"] == "openrouter")["usable"])

    def test_custom_provider_and_validation(self):
        c = self.admin()
        code, j = self.act(c, "agent.save", id="mine", name="Mine", kind="openai", base="http://localhost:9/v1", model="x", needs_key=False)
        self.assertEqual(code, 200, j)
        self.assertEqual(self.act(c, "agent.save", id="bad", base="nope")[0], 400)
        self.assertEqual(self.act(c, "agent.save", id="", base="http://x")[0], 400)
        self.assertEqual(self.act(c, "agent.save", id="mine", headers="{not json")[0], 400)
        self.assertEqual(self.act(c, "agent.remove", id="mine")[0], 200)

    def test_secret_headers_are_masked_and_kept(self):
        c = self.admin()
        self.act(c, "agent.save", id="custom1", headers='{"X-Api-Token": "very-secret", "X-Team": "blue"}')
        d = c.get("/api/dashboard/data?section=agent").get_json()
        row = next(p for p in d["providers"] if p["id"] == "custom1")
        self.assertEqual(row["headers"], {"X-Api-Token": "***", "X-Team": "blue"})
        self.act(c, "agent.save", id="custom1", headers=json.dumps(row["headers"]))
        self.assertEqual(providers.load()["providers"]["custom1"]["headers"]["X-Api-Token"], "very-secret")

    def test_test_action_and_options(self):
        c = self.admin()
        self.fake.script = [{"text": "ok"}]
        code, j = self.act(c, "agent.test", id="custom1")
        self.assertEqual(code, 200, j)
        self.assertIn("works", j["message"])
        self.assertNotIn("sk-test-key-123456", j["message"])
        self.assertEqual(self.act(c, "agent.test", id="groq")[0], 400)
        code, _j = self.act(c, "agent.options", mode="edit", max_steps=12, max_runs=3, compact_pct=70, bash_timeout=30, instructions="Be brief.")
        self.assertEqual(code, 200)
        o = providers.options()
        self.assertEqual((o["mode"], o["max_steps"], o["max_runs"], o["compact_pct"], o["bash_timeout"], o["instructions"]), ("edit", 12, 3, 70, 30, "Be brief."))
        self.assertEqual(self.act(c, "agent.options", max_steps="lots")[0], 400)
        providers.set_options({"max_steps": 9999, "max_runs": 0})
        self.assertEqual((providers.options()["max_steps"], providers.options()["max_runs"]), (200, 1))      # always clamped

    def test_instructions_reach_the_prompt_and_actions_are_audited(self):
        c = self.admin()
        self.act(c, "agent.options", instructions="Reply in Bengali.")
        self.fake.script = [{"text": "ok"}]
        s = self.session()
        self.turn(s, "hi")
        self.assertIn("Reply in Bengali.", self.fake.requests[0][2]["messages"][0]["content"])
        self.act(c, "agent.save", id="custom1", api_key="sk-rotated-key-777")
        import dashboard
        audit = json.dumps(dashboard.recent_audit(50))
        self.assertIn("agent.save", audit)
        self.assertNotIn("sk-rotated-key-777", audit)

    def test_non_admins_cannot_use_agent_actions(self):
        self.assertEqual(self.act(self.user(), "agent.save", id="openrouter", api_key="x" * 12)[0], 403)


if __name__ == "__main__":
    unittest.main()
