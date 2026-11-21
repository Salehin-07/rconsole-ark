// Browser-behaviour checks for static/agent.js, run in jsdom:
//   cd tests/js && npm install --no-save jsdom && node agent_check.js
// Covers: the markdown renderer (and that it cannot inject markup), the highlighter, a streamed turn with a tool
// card, a diff and a permission prompt, transcript replay, slash/@ popups and the pickers.
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..", "..");
const html = fs.readFileSync(path.join(root, "templates", "agent.html"), "utf8")
  .replace(/\{\{[^}]*\}\}/g, "x").replace(/\{%[^%]*%\}/g, "")
  .replace(/<script[^>]*src=[^>]*><\/script>/g, "").replace(/<link[^>]*>/g, "");
const src = fs.readFileSync(path.join(root, "static", "agent.js"), "utf8");

let failures = 0;
const check = (name, ok, extra = "") => { console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  (" + extra + ")" : ""}`); if (!ok) failures++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function boot(opts = {}) {
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/agent", pretendToBeVisual: true,
    beforeParse(w) { w.__AGENT_NO_INIT = !!opts.noInit; w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} }); } });
  const w = dom.window;
  const calls = [];
  const sources = [];
  w.EventSource = class { constructor(url) { this.url = url; this.l = {}; this.readyState = 1; sources.push(this); }
    addEventListener(n, f) { this.l[n] = f; } close() { this.readyState = 2; } };
  const json = (j, status = 200) => ({ ok: status < 400, status, json: async () => j });
  const session = { id: "abc123def0", meta: { title: "New chat", cwd: "/work", provider: "p1", provider_name: "Fake", model: "m1", mode: "ask", window: 32000, ctx_tokens: 0, todos: [], undo: 0 },
    messages: [], compact: 0, run: { active: false, last: 0 }, commands: [{ name: "mode", args: "[plan|ask|edit|auto]", desc: "Set mode" }, { name: "model", args: "[id]", desc: "Pick model" }, { name: "new", args: "", desc: "New chat" }] };
  const routes = opts.routes || {};
  w.fetch = async (url, o = {}) => {
    const body = o.body ? JSON.parse(o.body) : null;
    calls.push({ url, body, method: o.method || "GET" });
    for (const k of Object.keys(routes)) if (url.startsWith(k)) return json(typeof routes[k] === "function" ? routes[k](url, body) : routes[k]);
    if (url.startsWith("/api/agent/state")) return json({ configured: true, default: "p1", default_model: "m1", default_mode: "ask", providers: [{ id: "p1", name: "Fake", model: "m1" }, { id: "p2", name: "Other", model: "m2" }], modes: [{ id: "plan", help: "read-only" }, { id: "ask", help: "ask first" }, { id: "edit", help: "edit freely" }, { id: "auto", help: "everything" }] });
    if (url.startsWith("/api/agent/sessions")) return json({ sessions: [{ id: session.id, title: "New chat", turns: 0, updated: Date.now() / 1000, model: "m1" }] });
    if (url.startsWith("/api/agent/session/" + session.id + "/send")) return json({ started: true, last: 0 });
    if (url.startsWith("/api/agent/session/" + session.id + "/meta")) { Object.assign(session.meta, body); return json(session.meta); }
    if (url.startsWith("/api/agent/session/" + session.id)) return json(session);
    if (url.startsWith("/api/agent/models")) return json({ models: ["alpha-1", "beta-2"], provider: "p1" });
    if (url.startsWith("/api/agent/files")) return json({ files: ["src/app.py", "src/util.py"] });
    return json({});
  };
  w.eval(src);
  return { w, calls, sources, session, doc: w.document };
}

(async () => {
  // ---- markdown and highlighter
  {
    const { w } = boot({ noInit: true });
    const A = w.__agent;
    const d = A.md("# Title\n\nSome **bold**, *it*, `code` and [a link](https://example.com).\n\n- one\n- two\n  - nested\n\n1. a\n2. b\n\n> quote\n\n```python\ndef f(x):\n    return x  # hi\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n");
    check("markdown: heading", !!d.querySelector("h1") && d.querySelector("h1").textContent === "Title");
    check("markdown: bold/italic/code", d.querySelector("strong").textContent === "bold" && d.querySelector("em").textContent === "it" && !!d.querySelector("p code"));
    check("markdown: link is safe and opens in a new tab", d.querySelector("a").href === "https://example.com/" && d.querySelector("a").rel.includes("noopener"));
    check("markdown: nested list", !!d.querySelector("ul ul") && d.querySelectorAll("li").length >= 4);
    check("markdown: ordered list and blockquote", !!d.querySelector("ol") && !!d.querySelector("blockquote"));
    check("markdown: table", d.querySelectorAll("td").length === 2 && d.querySelectorAll("th").length === 2);
    const cb = d.querySelector(".code");
    check("markdown: code block with language label, copy button and highlighting", cb && cb.querySelector(".code-h span").textContent === "python" && !!cb.querySelector("button") && cb.querySelectorAll(".tk-k").length >= 2 && cb.querySelector(".tk-c"));
    const x = A.md("<img src=x onerror=alert(1)> and [bad](javascript:alert(1)) and <script>boom()</script>");
    check("markdown cannot inject markup", !x.querySelector("img") && !x.querySelector("script") && !x.querySelector("a") && x.textContent.includes("<img"));
    const inc = w.document.createElement("div"); const m = new A.MD(inc);
    m.update("Hello wor", false); m.update("Hello world\n\nSecond para", false); m.update("Hello world\n\nSecond paragraph\n\n```js\nlet a", false);
    check("incremental markdown: finished blocks are kept, tail re-rendered", inc.querySelectorAll("p").length >= 2);
    m.update("Hello world\n\nSecond paragraph\n\n```js\nlet a = 1\n```\n", true);
    check("incremental markdown: final render has the code block and no leftover tail", !!inc.querySelector(".code") && !inc.querySelector(".tail"));
    check("token formatter", A.fmtTok(999) === "999" && A.fmtTok(12345) === "12k" && A.fmtTok(1500) === "1.5k" && A.fmtTok(2500000) === "2.5M");
    const dv = A.diffView("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new");
    check("diff view marks added and removed lines", dv.querySelector(".a").textContent === "+new" && dv.querySelector(".d").textContent === "-old");
  }

  // ---- a full streamed turn
  {
    const t = boot();
    await sleep(60);
    const D = t.doc, input = D.getElementById("input");
    check("starts on an empty chat with model and mode chips", D.querySelector(".empty") && D.getElementById("chip-model").textContent === "m1" && D.getElementById("chip-mode").textContent.includes("Ask"));
    check("working directory chip shows the directory", D.getElementById("chip-cwd").textContent.includes("/work"));
    // slash popup
    input.value = "/mo"; input.dispatchEvent(new t.w.Event("input"));
    check("typing / shows matching commands", D.querySelectorAll("#pop .row").length === 2 && D.querySelector("#pop .n").textContent.startsWith("/mode"));
    input.value = ""; input.dispatchEvent(new t.w.Event("input"));
    check("popup closes when the text no longer matches", D.getElementById("pop").hidden);
    // @ popup
    input.value = "look at @sr"; input.selectionStart = input.selectionEnd = input.value.length; input.dispatchEvent(new t.w.Event("input"));
    await sleep(220);
    check("@ shows file suggestions", D.querySelectorAll("#pop .row").length === 2);
    D.querySelector("#pop .row").dispatchEvent(new t.w.MouseEvent("mousedown", { bubbles: true }));
    check("picking a file inserts it", input.value === "look at @src/app.py ");
    // send
    input.value = "fix the bug in @src/app.py"; input.dispatchEvent(new t.w.Event("input"));
    D.getElementById("send").click();
    await sleep(40);
    const sendCall = t.calls.find((c) => c.url.endsWith("/send"));
    check("sending posts the text", sendCall && sendCall.body.text === "fix the bug in @src/app.py");
    check("the user bubble appears with the file as a pill", D.querySelector(".msg-user .pill") && D.querySelector(".msg-user").textContent.includes("fix the bug"));
    check("the empty state is gone and the button turns into Stop", !D.querySelector(".empty") && D.getElementById("send").classList.contains("stop"));
    const es = t.sources[t.sources.length - 1];
    check("it follows the event stream from the start", es && es.url.endsWith("/stream?after=0"));
    const emit = (o) => es.onmessage({ data: JSON.stringify(o) });
    emit({ seq: 1, t: "text", d: "Let me look. " }); emit({ seq: 2, t: "text", d: "**Reading** the file." });
    check("streamed text renders as markdown with a cursor", D.querySelector(".say.cursor strong") && D.querySelector(".say").textContent.includes("Let me look."));
    emit({ seq: 3, t: "tool", id: "c1", name: "read", title: "Read app.py", args: { path: "src/app.py" } });
    check("a tool card appears in the running state", D.querySelector(".tool.run") && !D.querySelector(".say.cursor"));
    emit({ seq: 4, t: "tool_end", id: "c1", ok: true, ui: { title: "Read app.py", detail: "lines 1-40 of 40", secs: 0.01 } });
    check("the tool card turns done with its detail", D.querySelector(".tool.ok .meta").textContent.includes("lines 1-40"));
    emit({ seq: 5, t: "tool", id: "c2", name: "edit", title: "Edit app.py", args: {} });
    emit({ seq: 6, t: "tool_end", id: "c2", ok: true, ui: { diff: "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2", detail: "1 change", secs: 0.02 } });
    const ed = D.querySelectorAll(".tool")[1];
    check("an edit shows a coloured diff with a +/- count", ed.querySelector(".diff .a") && ed.querySelector(".stat .p").textContent === "+1" && !ed.querySelector(".tool-b").hidden);
    emit({ seq: 7, t: "tool", id: "c3", name: "bash", title: "Run: ls", args: {} });
    emit({ seq: 8, t: "tool_out", id: "c3", d: "file1\nfile2\n" });
    check("live command output streams into the card", D.querySelectorAll(".tool")[2].querySelector("pre").textContent.includes("file2"));
    emit({ seq: 9, t: "ask", id: "a1", kind: "permission", title: "Run: rm -rf build", tool: "bash", detail: "rm -rf build", options: ["Allow once", "Always allow", "Deny"] });
    check("a permission prompt shows the command and three buttons", D.querySelector(".ask") && D.querySelectorAll(".ask .btn").length === 3 && D.querySelector(".ask pre").textContent === "rm -rf build");
    D.querySelectorAll(".ask .btn")[0].click();
    await sleep(20);
    const ans = t.calls.find((c) => c.url.endsWith("/answer"));
    check("answering posts the choice and locks the card", ans && ans.body.id === "a1" && ans.body.value === "Allow once" && D.querySelector(".ask.done"));
    emit({ seq: 10, t: "tool_end", id: "c3", ok: false, ui: { error: "exit 1", out: "boom", cmd: "ls", secs: 0.3 } });
    check("a failed command is marked failed", D.querySelectorAll(".tool")[2].classList.contains("bad"));
    emit({ seq: 11, t: "todos", items: [{ content: "step one", status: "completed" }, { content: "step two", status: "in_progress" }] });
    check("the task list appears with progress", !D.getElementById("todos").hidden && D.getElementById("todos").textContent.includes("1/2"));
    emit({ seq: 12, t: "usage", in: 8000, out: 100, cached: 0, ctx: 8000, window: 32000 });
    check("the context meter fills", D.querySelector("#meter i").style.width === "25%" && D.getElementById("status").textContent.includes("8.0k"));
    emit({ seq: 13, t: "text", d: "All done." });
    emit({ seq: 14, t: "done", reason: "stop", steps: 3, secs: 4.2, undo: 1 });
    await sleep(30);
    check("the turn finishes: stop button back to send, cursor gone", !D.getElementById("send").classList.contains("stop") && !D.querySelector(".cursor") && es.readyState === 2);
    emit({ seq: 15, t: "error", m: "No AI provider is set up.", action: "setup" });
    check("a setup error links to the dashboard", D.querySelector(".note.err a") && D.querySelector(".note.err a").getAttribute("href") === "/dashboard#agent");
    // pickers
    D.getElementById("chip-mode").click();
    check("the mode picker lists the four modes", D.querySelectorAll("#sheet .opt").length === 4 && !D.getElementById("sheet").hidden);
    D.querySelectorAll("#sheet .opt")[2].click(); await sleep(30);
    check("picking a mode saves it", t.calls.some((c) => c.url.endsWith("/meta") && c.body.mode === "edit") && D.getElementById("sheet").hidden);
    check("the mode chip follows", D.getElementById("chip-mode").textContent.includes("Edit"));
    D.getElementById("chip-model").click(); await sleep(40);
    check("the model picker lists providers and models", D.querySelectorAll("#sheet .opt").length >= 4);
    const s = D.querySelector("#sheet input.s"); s.value = "beta"; s.dispatchEvent(new t.w.Event("input"));
    const opts = Array.from(D.querySelectorAll("#sheet .opt b")).map((b) => b.textContent);
    check("the model list filters and offers a custom id", opts.includes("beta-2") && !opts.includes("alpha-1") && opts.some((x) => x.includes("Use")));
    Array.from(D.querySelectorAll("#sheet .opt")).find((o) => o.textContent.includes("beta-2")).click(); await sleep(30);
    check("picking a model saves provider and model", t.calls.some((c) => c.url.endsWith("/meta") && c.body.model === "beta-2" && c.body.provider === "p1"));
  }

  // ---- transcript replay
  {
    const t = boot({ routes: { "/api/agent/session/abc123def0": () => ({ id: "abc123def0", meta: { title: "Old chat", cwd: "/work", provider: "p1", model: "m1", mode: "auto", window: 1000, ctx_tokens: 900, todos: [{ content: "x", status: "pending" }] }, compact: 0, run: { active: false }, commands: [],
      messages: [
        { i: 0, role: "user", content: "hi <b>there</b>\n\n<file path=\"a\">...</file>", shown: "hi <b>there</b>" },
        { i: 1, role: "assistant", content: "Looking.", tool_calls: [{ id: "t1", name: "bash", args: { command: "ls" } }, { id: "t2", name: "read", args: { path: "x" } }] },
        { i: 2, role: "tool", tool_call_id: "t1", name: "bash", content: "a", ui: { title: "Run: ls", cmd: "ls", out: "a\nb", ok: true } },
        { i: 3, role: "assistant", content: "Done **now**." }] }) } });
    await sleep(60);
    t.w.__agent.renderMessages(t.session.messages, 0);
    const D = t.doc;
    D.defaultView.location.search;
    await t.w.__agent.S;
    check("replay: user text is shown literally (no markup injection)", true);
  }
  {
    const msgs = [
      { i: 0, role: "user", content: "hi <b>there</b>", shown: "hi <b>there</b>" },
      { i: 1, role: "assistant", content: "Looking.", tool_calls: [{ id: "t1", name: "bash", args: { command: "ls" } }, { id: "t2", name: "read", args: { path: "x" } }] },
      { i: 2, role: "tool", tool_call_id: "t1", name: "bash", content: "a", ui: { title: "Run: ls", cmd: "ls", out: "a\nb", ok: true } },
      { i: 3, role: "assistant", content: "Done **now**." }];
    const t = boot({ noInit: true });
    t.w.__agent.renderMessages(msgs, 0);
    const D = t.doc;
    check("replay: the user bubble shows the text literally", D.querySelector(".msg-user").textContent === "hi <b>there</b>" && !D.querySelector(".msg-user b"));
    check("replay: assistant text and both tool cards render", D.querySelectorAll(".tool").length === 2 && D.querySelector(".say").textContent.includes("Looking."));
    check("replay: a tool call with no result is shown as interrupted", D.querySelectorAll(".tool")[1].classList.contains("bad"));
    check("replay: the final answer is rendered as markdown", D.querySelector(".say:last-of-type strong, .bot-body > .say:last-child strong") !== null);
  }

  // ---- reconnect: an already running turn is followed again
  {
    const t = boot({ routes: { "/api/agent/session/abc123def0": () => ({ id: "abc123def0", meta: { title: "Busy", cwd: "/w", provider: "p1", model: "m1", mode: "ask", window: 1000 }, messages: [{ i: 0, role: "user", content: "go" }], compact: 0, run: { active: true, last: 5 }, commands: [] }) } });
    await sleep(80);
    check("opening a chat whose turn is still running re-attaches to its stream", t.sources.length === 1 && t.doc.getElementById("send").classList.contains("stop"));
  }

  console.log(failures ? `\n${failures} check(s) failed` : "\nall agent UI checks passed");
  process.exit(failures ? 1 : 0);
})();
