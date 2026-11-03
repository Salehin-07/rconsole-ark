// Browser-behaviour checks for static/console.js, run in jsdom:
//   cd tests/js && npm install --no-save jsdom && node ui_check.js [path/to/console.js]
// Covers the things that only break in a real page: the inline system picker
// (must keep focus when tapped), the phone settings menu, and the ✕ window button.
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..", "..");
const jsPath = process.argv[2] || path.join(root, "static", "console.js");
const html = fs.readFileSync(path.join(root, "templates", "console.html"), "utf8")
  .replace(/\{\{[^}]*\}\}/g, "x").replace(/\{%[^%]*%\}/g, "")
  .replace(/<script[^>]*src=[^>]*><\/script>/g, "");

let failures = 0;
const check = (name, ok, extra = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  (" + extra + ")" : ""}`);
  if (!ok) failures++;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function load(phone) {
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/console", pretendToBeVisual: true,
    beforeParse(w) {
      w.matchMedia = (q) => ({ matches: q.includes("640px") ? phone : false, media: q,
        addEventListener() {}, removeEventListener() {} });
      w.Terminal = class { constructor() { this.cols = 80; this.rows = 24; } loadAddon() {} open(el) {
        const d = w.document.createElement("div"); d.className = "xterm"; el.appendChild(d); }
        onData() {} onResize() {} dispose() {} focus() {} write() {} };
      w.FitAddon = { FitAddon: class { fit() {} } };
      w.ResizeObserver = class { observe() {} disconnect() {} unobserve() {} };
      w.WebSocket = class { constructor() { this.readyState = 1; } send() {} close() {} };
    } });
  const w = dom.window;
  const errors = [];
  w.addEventListener("error", (e) => errors.push(e.message));
  const ok = (j) => ({ ok: true, status: 200, json: async () => j });
  const sent = [];
  const saved = [];
  const fileContent = { value: "" };
  const calls = [];                                   // every file-manager request, in order
  const dirState = {
    entries: [{ name: "proj", path: "/root/proj", is_dir: true, size: 0, mtime: 0 },
              { name: "a b.txt", path: "/root/a b.txt", is_dir: false, size: 12, mtime: 0 }],
    archives: { compress: [{ id: "zip", label: "ZIP", ext: ".zip", single: false },
                           { id: "tar.gz", label: "TAR + gzip", ext: ".tar.gz", single: false },
                           { id: "gz", label: "gzip (one file)", ext: ".gz", single: true }],
                extract: [".zip", ".tar.gz", ".gz", ".7z"] },
  };
  const jobReplies = [];                              // GET /api/archive/<id> answers, consumed in order
  const apiReplies = { archiveStart: { ok: true, job: "j1" }, props: null };
  const targets = { current: null, label: "choose-vm", supported: true,
    targets: [{ id: "vm:alpine", label: "Alpine", kind: "vm" }, { id: "vm:debian", label: "Debian", kind: "vm" }],
    installing: [], installable: [{ id: "kali", name: "Kali Linux", size: "~140 MB" }] };
  w.fetch = async (url, opts) => {
    if (url.startsWith("/api/file")) {
      if (opts && opts.method === "POST") { saved.push(JSON.parse(opts.body)); return ok({ ok: true }); }
      return ok({ content: fileContent.value, exists: true });
    }
    if (url.startsWith("/api/tabs")) return ok({ tabs: [{ id: "main", name: "main", cwd: "/root", is_terminal: false, alive: false, cmd: "" }] });
    if (url.startsWith("/api/targets")) return ok(targets);
    if (url.startsWith("/api/target")) return ok({ ok: true, ...targets, current: "vm:debian", label: "debian", cwds: { main: "/root" } });
    if (url.startsWith("/api/dir")) {
      calls.push({ url });
      return ok({ path: "/root", parent: "/", truncated: false, max_upload_mb: 1, entries: dirState.entries.slice(),
                  archives: dirState.archives });
    }
    if (url.startsWith("/api/props")) { calls.push({ url }); return ok(apiReplies.props || { error: "no props" }); }
    if (url === "/api/archive") { calls.push({ url, body: JSON.parse(opts.body) }); return ok(apiReplies.archiveStart); }
    if (/^\/api\/archive\/[^/]+\/cancel$/.test(url)) { calls.push({ url, method: "POST" }); return ok({ ok: true }); }
    if (url.startsWith("/api/archive/")) {
      calls.push({ url });
      return ok(jobReplies.length > 1 ? jobReplies.shift() : (jobReplies[0] || { state: "running", done: 0, total: 0 }));
    }
    if (url === "/api/fs") {
      const body = JSON.parse(opts.body);
      calls.push({ url, body });
      if (body.action === "delete_many") return ok({ ok: true, deleted: body.paths.length, failed: [] });
      return ok({ ok: true });
    }
    if (url.startsWith("/api/run")) {
      const cmd = JSON.parse(opts.body).cmd;
      sent.push(cmd);
      if (cmd === "id") return ok({ output: "Several systems…", cwd: "/root", target: "choose-vm",
        choice: { title: "Choose a system", options: [
          { label: "Alpine", cmds: ["vm use alpine", "id"] }, { label: "Debian", cmds: ["vm use debian", "id"] },
          { label: "+ Install Kali Linux", cmds: ["vm install kali"], group: "install" }] } });
      return ok({ output: "ran: " + cmd, cwd: "/root", target: "debian" });
    }
    return ok({});
  };
  w.eval(fs.readFileSync(path.join(path.dirname(jsPath), "editor-core.js"), "utf8"));
  w.eval(fs.readFileSync(path.join(path.dirname(jsPath), "files-core.js"), "utf8"));
  w.eval(fs.readFileSync(jsPath, "utf8") + "\n;window.__t = { tabs, openPty, showUpdateOverlay, openCodeEditor };");
  await sleep(150);
  return { w, d: w.document, errors, sent, saved, fileContent, calls, dirState, jobReplies, apiReplies };
}

(async () => {
  // ---- the inline picker -----------------------------------------------------------------
  {
    const { w, d, errors, sent } = await load(false);
    d.getElementById("cmd").value = "id";
    d.getElementById("promptform").dispatchEvent(new w.Event("submit", { cancelable: true }));
    await sleep(80);
    const pick = d.querySelector(".choice-select");
    check("picker is rendered with its options", !!pick && pick.querySelectorAll("option").length >= 4);

    // The bug: a click on the select bubbled to #screen, whose handler focused the
    // command box — which closes a native dropdown instantly.
    pick.focus();
    pick.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
    check("tapping the picker keeps focus on it", d.activeElement === pick,
      "focus is on " + (d.activeElement && (d.activeElement.id || d.activeElement.className)));
    d.getElementById("output").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
    check("tapping empty output still focuses the prompt", d.activeElement === d.getElementById("cmd"));

    pick.value = "1";
    pick.dispatchEvent(new w.Event("change"));
    await sleep(150);
    check("choosing runs the option's commands in order", JSON.stringify(sent) === JSON.stringify(["id", "vm use debian", "id"]),
      JSON.stringify(sent));
    check("picker is disabled after use", pick.disabled === true);
    check("no JS errors (picker)", errors.length === 0, errors.join("; "));
  }
  // ---- the update lock screen -------------------------------------------------------------------
  {
    const { w, d, errors } = await load(false);
    check("no update screen normally", !d.getElementById("updateOverlay"));
    w.__t.showUpdateOverlay();
    const ov = d.getElementById("updateOverlay");
    check("update screen covers the console", !!ov && !!ov.querySelector(".update-fill") && /updating/i.test(ov.textContent));
    w.__t.showUpdateOverlay();
    await sleep(100);   // the mocked status says "not updating": a stale 503, so the screen goes away again
    check("a stale lock screen removes itself", !d.getElementById("updateOverlay"));
    w.__t.showUpdateOverlay(); w.__t.showUpdateOverlay();
    check("it is only shown once", d.querySelectorAll("#updateOverlay").length === 1);
    check("no JS errors (update screen)", errors.length === 0, errors.join("; "));
  }
  // ---- file browser: download + upload buttons -------------------------------------------------
  {
    const { w, d, errors } = await load(false);
    d.getElementById("filesbtn").click();
    await sleep(120);
    const rows = d.querySelectorAll(".files-row");
    check("file browser lists the entries", rows.length === 2, "rows=" + rows.length);
    const dls = d.querySelectorAll(".files-dl");
    check("every row has a download button", dls.length === 2);
    check("folder download is labelled .zip", /zip/i.test(dls[0].title) && !/zip/i.test(dls[1].title), dls[0].title + " | " + dls[1].title);
    const hrefs = [];
    d.addEventListener("click", (ev) => {
      const a = ev.target.closest && ev.target.closest("a[download]");
      if (a) { hrefs.push(a.getAttribute("href")); ev.preventDefault(); }
    }, true);
    dls[1].click();
    dls[0].click();
    check("download points at /api/download with the encoded path",
      hrefs[0] === "/api/download?path=%2Froot%2Fa%20b.txt" && hrefs[1] === "/api/download?path=%2Froot%2Fproj", JSON.stringify(hrefs));
    check("download does not open the file in the editor", !d.getElementById("editor") || d.getElementById("editor").hidden);

    check("upload files + upload folder buttons exist", !!d.getElementById("files-upfiles") && !!d.getElementById("files-upfolder"));
    check("folder picker is a directory picker", d.getElementById("files-pickdir").hasAttribute("webkitdirectory"));
    check("file picker allows several files", d.getElementById("files-pick").multiple === true);

    // fake XHR so we can see what the browser would send
    const posts = [];
    let conflictOnce = true;
    w.XMLHttpRequest = class {
      open(m, u) { this.m = m; this.u = u; this.upload = {}; }
      send(fd) {
        const rec = { m: this.m, u: this.u, dir: fd.get("dir"), rel: fd.get("rel"), over: fd.get("overwrite"), name: fd.get("file").name };
        posts.push(rec);
        const clash = conflictOnce && rec.rel === "proj/b.txt" && !rec.over;
        setTimeout(() => {
          this.status = clash ? 409 : 200;
          this.responseText = JSON.stringify(clash ? { ok: false, exists: true, error: "exists" } : { ok: true });
          this.onload();
        }, 5);
      }
    };
    w.confirm = () => true;
    const mk = (name, rel, size) => { const f = new w.File(["x".repeat(size)], name); if (rel) Object.defineProperty(f, "webkitRelativePath", { value: rel }); return f; };
    const input = d.getElementById("files-pickdir");
    Object.defineProperty(input, "files", { value: [mk("a.txt", "proj/a.txt", 3), mk("b.txt", "proj/b.txt", 3), mk("big.bin", "proj/big.bin", 1048576 + 5)], configurable: true });
    input.dispatchEvent(new w.Event("change"));
    await sleep(250);
    const rels = posts.map((p) => p.rel + (p.over ? "!" : "")).sort();
    check("folder upload keeps relative paths and sends to the current folder",
      posts.every((p) => p.dir === "/root" && p.m === "POST" && p.u === "/api/upload") && rels.includes("proj/a.txt") && rels.includes("proj/b.txt"), JSON.stringify(rels));
    check("existing file is re-sent only after confirming overwrite", rels.includes("proj/b.txt!"), JSON.stringify(rels));
    check("a file over the limit is not sent", !rels.some((r) => r.startsWith("proj/big")), JSON.stringify(rels));
    const st = d.getElementById("files-status").textContent;
    check("status reports the result", /Uploaded 2 file/.test(st) && /1 failed/.test(st) && /too large/.test(st), st);
    check("buttons are enabled again afterwards", d.getElementById("files-upfiles").disabled === false);
    check("no JS errors (file browser)", errors.length === 0, errors.join("; "));
  }
  // ---- phone vs desktop layout + ✕ ---------------------------------------------------------
  for (const phone of [true, false]) {
    const { w, d, errors } = await load(phone);
    const bar = d.getElementById("tabbar"), menu = d.getElementById("settingsmenu");
    const where = (id) => (bar.contains(d.getElementById(id)) ? "bar" : menu.contains(d.getElementById(id)) ? "menu" : "?");
    const label = phone ? "phone" : "desktop";
    check(`${label}: theme button is in the ${phone ? "menu" : "bar"}`, where("themebtn") === (phone ? "menu" : "bar"));
    check(`${label}: system dropdown is in the ${phone ? "menu" : "bar"}`, where("targetsel") === (phone ? "menu" : "bar"));
    if (phone) {
      d.getElementById("settingsbtn").click();
      check("phone: ⚙ opens the menu", menu.hidden === false);
      d.body.click();
      check("phone: tapping outside closes it", menu.hidden === true);
    }
    const t = w.__t.tabs[0];
    w.__t.openPty("@vm:debian", t.id);
    await sleep(120);
    const x = d.querySelector(".term-close");
    check(`${label}: interactive window has a ✕ button`, !!x);
    if (x) { x.click(); await sleep(60); }
    check(`${label}: ✕ closes the window`, !d.querySelector(".term-host") && !w.__t.tabs[0].ptyActive);
    check(`no JS errors (${label})`, errors.length === 0, errors.join("; "));
  }
  // ---- code editor: auto-indent, auto-close, shortcuts, find/replace, font size ----------------
  {
    const { w, d, errors, saved, fileContent } = await load(false);
    fileContent.value = "def f(x):\n    return x\n";
    await w.__t.openCodeEditor("/root/a.py");
    await sleep(120);
    const ta = d.getElementById("editor-input");
    const $ = (id) => d.getElementById(id);
    const caret = (a, b) => ta.setSelectionRange(a, b === undefined ? a : b);
    const set = (text, a, b) => { ta.value = text; caret(a === undefined ? text.length : a, b); };
    const fire = (type, init) => { const ev = new w.InputEvent(type, Object.assign({ bubbles: true, cancelable: true }, init)); return !ta.dispatchEvent(ev); };
    // a keystroke the way a browser delivers it: beforeinput first, native insert only if not cancelled
    const typeCh = (ch) => { if (!fire("beforeinput", { inputType: "insertText", data: ch })) { ta.setRangeText(ch, ta.selectionStart, ta.selectionEnd, "end"); ta.dispatchEvent(new w.Event("input", { bubbles: true })); } };
    const typeStr = (str) => { for (const c of str) typeCh(c); };
    const enter = () => fire("beforeinput", { inputType: "insertLineBreak" });
    const back = () => fire("beforeinput", { inputType: "deleteContentBackward" });
    const key = (k, extra) => { const ev = new w.KeyboardEvent("keydown", Object.assign({ key: k, bubbles: true, cancelable: true }, extra)); ta.dispatchEvent(ev); return ev.defaultPrevented; };

    check("editor opens with the file's content", !$("editor").hidden && ta.value === fileContent.value);
    check("indentation is detected from the file", $("es-indent").textContent === "Spaces: 4", $("es-indent").textContent);
    await sleep(50);
    check("status bar shows the cursor position", /^Ln 1, Col 1/.test($("es-pos").textContent) && /3 lines/.test($("es-lines").textContent),
      $("es-pos").textContent + " | " + $("es-lines").textContent);
    check("symbol bar has the symbols", $("ek-syms").querySelectorAll("button").length === 28, String($("ek-syms").querySelectorAll("button").length));

    set("", 0);
    typeStr("def g(");
    check("typing ( adds the closing )", ta.value === "def g()" && ta.selectionStart === 6, JSON.stringify(ta.value) + "@" + ta.selectionStart);
    typeStr("):");
    check("typing ) steps over the closer", ta.value === "def g():" && ta.selectionStart === 8, JSON.stringify(ta.value));
    const prevented = enter();
    check("Enter after ':' indents the next line", prevented && ta.value === "def g():\n    " && ta.selectionStart === 13, JSON.stringify(ta.value));
    typeStr("x = {");
    check("auto-close curly after indent", ta.value.endsWith("x = {}") && ta.selectionStart === ta.value.length - 1, JSON.stringify(ta.value));
    enter();
    check("Enter between {} opens an indented block", ta.value.endsWith("x = {\n        \n    }"), JSON.stringify(ta.value));
    set("s = ", 4);
    typeStr('"');
    check("opening quote is auto-closed", ta.value === 's = ""' && ta.selectionStart === 5, JSON.stringify(ta.value));
    set("a(|)".replace("|", ""), 2);
    back();
    check("Backspace between a pair removes both", ta.value === "a" && ta.selectionStart === 1, JSON.stringify(ta.value));
    set("don", 3);
    typeCh("'");
    check("an apostrophe after a word is not paired", ta.value === "don'", JSON.stringify(ta.value));

    set("x = 1", 5);
    key("/", { ctrlKey: true });
    check("Ctrl+/ comments the line", ta.value === "# x = 1", JSON.stringify(ta.value));
    key("/", { ctrlKey: true });
    check("Ctrl+/ again uncomments it", ta.value === "x = 1", JSON.stringify(ta.value));

    set("a\nb\nc", 0);
    key("ArrowDown", { altKey: true });
    check("Alt+Down moves the line", ta.value === "b\na\nc", JSON.stringify(ta.value));
    set("a\nb", 0);
    key("ArrowDown", { altKey: true, shiftKey: true });
    check("Alt+Shift+Down duplicates the line", ta.value === "a\na\nb", JSON.stringify(ta.value));

    set("ab", 2);
    const tabPrevented = key("Tab");
    check("Tab inserts to the next tab stop and keeps focus", tabPrevented && ta.value === "ab  ", JSON.stringify(ta.value));
    set("    x", 5);
    key("Tab", { shiftKey: true });
    check("Shift+Tab outdents", ta.value === "x", JSON.stringify(ta.value));
    set("a\nb", 0, 3);
    key("Tab");
    check("Tab indents every selected line", ta.value === "    a\n    b", JSON.stringify(ta.value));
    set("  x", 3);
    key("Home");
    check("Home goes to the first non-blank character", ta.selectionStart === 2, String(ta.selectionStart));
    key("Home");
    check("Home again goes to column 0", ta.selectionStart === 0, String(ta.selectionStart));

    // symbol bar button goes through the same auto-close logic
    set("f", 1);
    const parenBtn = [...$("ek-syms").querySelectorAll("button")].find((b) => b.dataset.ins === "(");
    parenBtn.click();
    check("symbol-bar ( button auto-closes too", ta.value === "f()" && ta.selectionStart === 2, JSON.stringify(ta.value));

    // find / replace
    set("foo bar foo\nFOO", 0);
    key("f", { ctrlKey: true });
    check("Ctrl+F opens the find bar", !$("editor-find").hidden);
    $("ef-query").value = "foo";
    $("ef-query").dispatchEvent(new w.Event("input", { bubbles: true }));
    check("find counts matches (case-insensitive) and selects the first",
      /1 of 3/.test($("ef-count").textContent) && ta.selectionStart === 0 && ta.selectionEnd === 3, $("ef-count").textContent);
    $("ef-next").click();
    check("next moves to the second match", ta.selectionStart === 8 && /2 of 3/.test($("ef-count").textContent), ta.selectionStart + " " + $("ef-count").textContent);
    $("ef-case").click();
    check("match-case narrows the results", /of 2|2 found/.test($("ef-count").textContent), $("ef-count").textContent);
    $("ef-case").click();
    key("h", { ctrlKey: true });
    check("Ctrl+H shows the replace row", !$("ef-repl-row").hidden);
    $("ef-repl").value = "baz";
    $("ef-all").click();
    check("Replace All replaces every match", ta.value === "baz bar baz\nbaz" && /Replaced 3/.test($("ef-count").textContent), JSON.stringify(ta.value));
    $("ef-regex").click();
    $("ef-query").value = "(";
    $("ef-query").dispatchEvent(new w.Event("input", { bubbles: true }));
    check("an invalid regex is reported, not thrown", /Invalid/.test($("ef-count").textContent), $("ef-count").textContent);
    key("Escape");
    check("Esc closes the find bar first (and not the editor)", $("editor-find").hidden && !$("editor").hidden);

    // font size: just "-" and "+" in the top bar, next to Save and Close
    const bar = $("editor-bar");
    check("the - and + buttons sit in the top bar with Save and Close",
      bar.contains($("es-fs-down")) && bar.contains($("es-fs-up")) && bar.contains($("editor-save")) &&
      $("es-fs-down").textContent === "\u2212" && $("es-fs-up").textContent === "+", $("es-fs-down").textContent + $("es-fs-up").textContent);
    check("the status bar no longer carries font controls", !$("editor-statusbar").querySelector("#es-fs-up, #es-fs"));
    const px = () => parseInt($("editor").style.getPropertyValue("--editor-fs"), 10);
    const before = px();
    $("es-fs-up").click();
    check("+ makes the text bigger", px() === before + 1, $("editor").style.getPropertyValue("--editor-fs"));
    check("font size is remembered", w.localStorage.getItem("rconsole.editor.fontsize") === String(before + 1));
    check("the tooltip shows the size", /\(now \d+ px\)/.test($("es-fs-up").title), $("es-fs-up").title);
    $("es-fs-down").click(); $("es-fs-down").click();
    check("\u2212 makes it smaller", px() === before - 1);
    for (let i = 0; i < 40; i++) $("es-fs-up").click();
    check("font size is capped", $("es-fs-up").disabled === true && px() === 32, String(px()));

    // indentation switch + save
    $("es-indent").click();
    check("the indent button cycles the indentation", $("es-indent").textContent === "Tabs", $("es-indent").textContent);
    set("x", 1);
    typeCh("y");
    key("s", { ctrlKey: true });
    await sleep(60);
    check("Ctrl+S saves the edited text", saved.length === 1 && saved[0].path === "/root/a.py" && saved[0].content === "xy", JSON.stringify(saved));
    check("no JS errors (editor)", errors.length === 0, errors.join("; "));
  }
  // ---- file manager: select (click / ctrl / shift / drag / long-press), compress, extract, properties, delete ----
  {
    const { w, d, errors, calls, dirState, jobReplies, apiReplies } = await load(false);
    const $ = (id) => d.getElementById(id);
    const waitFor = async (cond, ms = 4000) => { const end = Date.now() + ms; while (Date.now() < end) { if (cond()) return true; await sleep(20); } return false; };
    // jsdom has no layout: give every row a 30px slot so the rectangle logic has geometry
    Object.defineProperty(w.HTMLElement.prototype, "offsetTop", { configurable: true,
      get() { const p = this.parentElement; return p ? Array.prototype.indexOf.call(p.children, this) * 30 : 0; } });
    Object.defineProperty(w.HTMLElement.prototype, "offsetHeight", { configurable: true,
      get() { return this.classList && this.classList.contains("files-row") ? 30 : 0; } });
    const names = ["docs", "a.txt", "b.zip", "c.tar.gz", "notes.md"];
    dirState.entries = names.map((n, i) => ({ name: n, path: "/root/" + n, is_dir: i === 0, size: i === 0 ? 0 : 10 * i, mtime: 0 }));
    const list = $("files-list");
    const rows = () => [...list.querySelectorAll(".files-row[data-path]")];
    const selected = () => rows().filter((r) => r.classList.contains("sel")).map((r) => r.dataset.path.replace("/root/", ""));
    const fillOf = (i) => rows()[i].querySelector(".files-fill");
    const nameBtn = (i) => rows()[i].querySelector(".files-name");
    const click = (el, init, touch) => {
      const ev = new w.MouseEvent("click", Object.assign({ bubbles: true, cancelable: true }, init));
      if (touch) Object.defineProperty(ev, "pointerType", { value: "touch" });
      el.dispatchEvent(ev);
      return ev;
    };
    const key = (el, k, init) => el.dispatchEvent(new w.KeyboardEvent("keydown", Object.assign({ key: k, bubbles: true, cancelable: true }, init)));
    const touchEv = (type, x, y) => { const ev = new w.Event(type, { bubbles: true, cancelable: true });
      Object.defineProperty(ev, "touches", { value: type === "touchend" ? [] : [{ clientX: x, clientY: y }] }); return ev; };

    $("filesbtn").click();
    await waitFor(() => rows().length === 5);
    check("rows carry their path and start unselected", rows().length === 5 && selected().length === 0 && $("files-sel").hidden === true);
    check("the first-run tip is shown", /^Tip: /.test($("files-status").textContent), $("files-status").textContent);

    // -- mouse: blank space selects, the name opens
    click(fillOf(2));
    check("clicking the blank space of a row selects only that row", JSON.stringify(selected()) === '["b.zip"]', JSON.stringify(selected()));
    check("the selection bar appears", $("files-sel").hidden === false && /1 item selected/.test($("fsel-count").textContent), $("fsel-count").textContent);
    check("Properties is offered for exactly one item", $("fsel-props").hidden === false);
    check("Extract is offered for an archive", $("fsel-extract").hidden === false);
    click(fillOf(1));
    check("clicking another row moves the selection", JSON.stringify(selected()) === '["a.txt"]');
    check("Extract is hidden for a non-archive", $("fsel-extract").hidden === true);
    click(fillOf(3), { ctrlKey: true });
    check("ctrl-click adds a row", JSON.stringify(selected()) === '["a.txt","c.tar.gz"]');
    check("Properties is hidden once two items are selected", $("fsel-props").hidden === true && /2 items selected/.test($("fsel-count").textContent));
    click(fillOf(3), { ctrlKey: true });
    check("ctrl-click again removes it", JSON.stringify(selected()) === '["a.txt"]');
    click(fillOf(1));                                   // plain click sets the anchor
    click(fillOf(4), { shiftKey: true });
    check("shift-click selects the range from the anchor", JSON.stringify(selected()) === '["a.txt","b.zip","c.tar.gz","notes.md"]', JSON.stringify(selected()));
    click(fillOf(2), { ctrlKey: true });
    click(fillOf(0), { ctrlKey: true });
    click(fillOf(2), { shiftKey: true });
    check("ctrl-click moves the anchor (like Explorer): shift then ranges from the last ctrl-clicked row",
      JSON.stringify(selected()) === '["docs","a.txt","b.zip"]', JSON.stringify(selected()));
    click(fillOf(1));
    click(fillOf(4), { shiftKey: true });
    click(nameBtn(4), { ctrlKey: true });
    check("ctrl-click on a name selects instead of opening", selected().length === 3 && $("editor").hidden !== false);
    click(list);
    check("clicking the empty area clears the selection", selected().length === 0 && $("files-sel").hidden === true);

    // -- select all: no permanent button; the checkbox shows up with the selection bar
    check("no select-all button in the toolbar", !$("files-selall") && !$("fsel-all"));
    check("the select-all checkbox is hidden while nothing is selected", $("files-sel").hidden === true);
    click(fillOf(1));
    check("one selected row: checkbox shown, partially checked", $("files-sel").hidden === false
      && $("fsel-check").indeterminate === true && $("fsel-check").checked === false);
    $("fsel-check").click();
    check("the checkbox selects every row", selected().length === 5 && $("fsel-check").checked === true
      && $("fsel-check").indeterminate === false);
    $("fsel-check").click();
    check("clicking it again clears (and hides the bar)", selected().length === 0 && $("files-sel").hidden === true);
    key(list, "a", { ctrlKey: true });
    check("Ctrl+A selects everything", selected().length === 5);
    check("Extract is hidden when a folder / plain files are included", $("fsel-extract").hidden === true);
    key(list, "Escape");
    check("Esc clears the selection first", selected().length === 0 && !$("files").hidden);
    key(list, "a", { ctrlKey: true });
    check("Ctrl+A selects everything again", selected().length === 5);
    $("fsel-clear").click();
    check("the bar's cross clears it", selected().length === 0);

    // -- name opens (plain click), double-click on blank space opens
    const dirCallsBefore = calls.filter((c) => c.url.startsWith("/api/dir")).length;
    click(nameBtn(0));
    await waitFor(() => calls.filter((c) => c.url.startsWith("/api/dir")).length > dirCallsBefore);
    check("a plain click on a folder name opens it", /path=%2Froot%2Fdocs/.test(calls.filter((c) => c.url.startsWith("/api/dir")).pop().url));
    await waitFor(() => rows().length === 5);

    // -- drag a rectangle
    const down = (y, init) => rows()[0].querySelector(".files-fill").dispatchEvent(new w.MouseEvent("pointerdown",
      Object.assign({ bubbles: true, button: 0, clientX: 80, clientY: y }, init)));
    const move = (y) => d.dispatchEvent(new w.MouseEvent("pointermove", { bubbles: true, clientX: 80, clientY: y }));
    const up = () => d.dispatchEvent(new w.MouseEvent("pointerup", { bubbles: true }));
    down(35);                                           // inside row 1 (y 30..60)
    move(36);
    check("a tiny move does not start a rectangle", !list.querySelector("#files-band"));
    move(100);                                          // down to row 3 (y 90..120)
    check("dragging draws the rectangle", !!list.querySelector("#files-band"));
    check("and selects the rows it touches", JSON.stringify(selected()) === '["a.txt","b.zip","c.tar.gz"]', JSON.stringify(selected()));
    move(50);
    check("shrinking the rectangle unselects rows", JSON.stringify(selected()) === '["a.txt"]', JSON.stringify(selected()));
    up();
    check("releasing removes the rectangle and keeps the selection", !list.querySelector("#files-band") && selected().length === 1);
    click(fillOf(4));
    check("the click that ends a drag does not re-select", JSON.stringify(selected()) === '["a.txt"]', JSON.stringify(selected()));
    await sleep(120);
    click(fillOf(4), { ctrlKey: true });
    down(95, { ctrlKey: true }); move(125); up();
    check("ctrl + drag adds to the selection", JSON.stringify(selected()) === '["a.txt","c.tar.gz","notes.md"]', JSON.stringify(selected()));
    $("fsel-clear").click();

    // -- phones: long-press selects, a drag sweeps, taps then toggle
    d.body.classList.add("touch");
    const row2 = rows()[2];
    d.elementFromPoint = (x, y) => rows()[Math.max(0, Math.min(4, Math.floor(y / 30)))];
    click(fillOf(2), {}, true);
    check("a plain tap on blank space does nothing on a phone", selected().length === 0);
    row2.querySelector(".files-fill").dispatchEvent(touchEv("touchstart", 60, 75));
    await sleep(520 + 80);
    check("a long-press selects the row", JSON.stringify(selected()) === '["b.zip"]', JSON.stringify(selected()));
    list.dispatchEvent(touchEv("touchmove", 60, 130));          // finger now over row 4
    check("dragging after the long-press sweeps over more rows", JSON.stringify(selected()) === '["b.zip","c.tar.gz","notes.md"]', JSON.stringify(selected()));
    list.dispatchEvent(touchEv("touchmove", 60, 70));           // and back up to the start row
    check("dragging back shrinks the sweep", JSON.stringify(selected()) === '["b.zip"]', JSON.stringify(selected()));
    list.dispatchEvent(touchEv("touchend", 0, 0));
    click(fillOf(2), {}, true);
    check("the click after lift-off is swallowed", JSON.stringify(selected()) === '["b.zip"]');
    await sleep(750);
    click(fillOf(1), {}, true);
    check("with a selection, a tap on a row toggles it", JSON.stringify(selected()) === '["a.txt","b.zip"]', JSON.stringify(selected()));
    const openedBefore = $("editor").hidden;
    click(nameBtn(1), {}, true);
    check("with a selection, tapping a name toggles instead of opening", JSON.stringify(selected()) === '["b.zip"]' && $("editor").hidden === openedBefore);
    // scrolling must not select: finger moves before the timer
    $("fsel-clear").click();
    fillOf(3).dispatchEvent(touchEv("touchstart", 60, 105));
    list.dispatchEvent(touchEv("touchmove", 60, 160));
    await sleep(520 + 80);
    list.dispatchEvent(touchEv("touchend", 0, 0));
    check("moving before the timer is a scroll, not a long-press", selected().length === 0);
    d.body.classList.remove("touch");

    // -- compress dialog
    $("fsel-clear").click();
    click(fillOf(0)); click(fillOf(1), { ctrlKey: true });
    $("fsel-compress").click();
    check("Compress opens a dialog", $("files-dialog").hidden === false && /Compress 2 items/.test($("fd-title").textContent), $("fd-title").textContent);
    const fmt = $("fd-format");
    check("formats come from the server, one-file formats are greyed out for 2 items",
      [...fmt.options].map((o) => o.value + (o.disabled ? "!" : "")).join(",") === "zip,tar.gz,gz!", [...fmt.options].map((o) => o.value + (o.disabled ? "!" : "")).join(","));
    check("the default name is the current folder and the extension follows the format", $("fd-name").value === "root" && /\.zip$/.test($("fd-name").parentNode.textContent));
    $("fd-name").value = "a/b";
    $("fd-actions").querySelector(".primary").click();
    check("a name with a slash is refused without calling the server", !calls.some((c) => c.url === "/api/archive") && /without slashes/.test($("fd-body").textContent));
    $("fd-name").value = "bundle";
    jobReplies.length = 0;
    jobReplies.push({ state: "running", done: 50, total: 100, note: "compressing" }, { state: "done", result: { name: "bundle.zip", size: 2048, path: "/root/bundle.zip" } });
    dirState.entries.push({ name: "bundle.zip", path: "/root/bundle.zip", is_dir: false, size: 2048, mtime: 0 });
    $("fd-actions").querySelector(".primary").click();
    await waitFor(() => calls.some((c) => c.url === "/api/archive"));
    const start = calls.find((c) => c.url === "/api/archive").body;
    check("the request names the items, format and name",
      start.op === "compress" && start.format === "zip" && start.name === "bundle" && JSON.stringify(start.items) === '["/root/docs","/root/a.txt"]', JSON.stringify(start));
    check("a progress bar replaces the form", !!$("fd-body").querySelector("progress"));
    await waitFor(() => $("files-dialog").hidden === true, 5000);
    check("the dialog closes when the job is done", $("files-dialog").hidden === true);
    await waitFor(() => /Created bundle.zip/.test($("files-status").textContent));
    check("a message says what was created", /Created bundle\.zip \(2\.0 KB\)/.test($("files-status").textContent) || /Created bundle\.zip/.test($("files-status").textContent), $("files-status").textContent);
    check("the new archive is selected", JSON.stringify(selected()) === '["bundle.zip"]', JSON.stringify(selected()));

    // single file + one-file format keeps the full name
    calls.length = 0;
    click(fillOf(1));
    $("fsel-compress").click();
    $("fd-format").value = "gz";
    $("fd-format").dispatchEvent(new w.Event("change", { bubbles: true }));
    check("one-file formats keep the full file name", $("fd-name").value === "a.txt" && /\.gz$/.test($("fd-name").parentNode.textContent), $("fd-name").value);
    $("fd-format").value = "zip";
    $("fd-format").dispatchEvent(new w.Event("change", { bubbles: true }));
    check("switching back re-suggests the name without its extension", $("fd-name").value === "a");
    $("fd-actions").querySelectorAll("button")[1].click();
    check("Cancel closes the dialog", $("files-dialog").hidden === true);

    // server refuses
    click(fillOf(1));
    $("fsel-compress").click();
    apiReplies.archiveStart = { ok: false, error: "another file operation is still running" };
    $("fd-actions").querySelector(".primary").click();
    await waitFor(() => /still running/.test($("fd-body").textContent));
    check("a refusal is shown in the dialog", /still running/.test($("fd-body").textContent) && $("fd-actions").textContent === "Close");
    $("fd-actions").querySelector("button").click();
    apiReplies.archiveStart = { ok: true, job: "j1" };

    // cancel a running job
    calls.length = 0;
    jobReplies.length = 0;
    jobReplies.push({ state: "running", done: 1, total: 100 });
    click(fillOf(1));
    $("fsel-compress").click();
    $("fd-actions").querySelector(".primary").click();
    await waitFor(() => !!$("fd-body").querySelector("progress") && calls.some((c) => c.url === "/api/archive"));
    await waitFor(() => calls.some((c) => c.url.startsWith("/api/archive/j1")));
    $("fd-actions").querySelector("button").click();
    await waitFor(() => calls.some((c) => c.url === "/api/archive/j1/cancel"));
    check("Cancel on a running job calls the server", calls.some((c) => c.url === "/api/archive/j1/cancel"));
    jobReplies.length = 0;
    jobReplies.push({ state: "cancelled", error: "cancelled" });
    await waitFor(() => $("files-dialog").hidden === true, 4000);
    check("a cancelled job closes the dialog", $("files-dialog").hidden === true && /Cancelled/.test($("files-status").textContent),
      "hidden=" + $("files-dialog").hidden + " status=" + JSON.stringify($("files-status").textContent) + " body=" + JSON.stringify($("fd-body").textContent));

    // a job that fails shows the server's message (and does not retry as if the connection was lost)
    calls.length = 0;
    jobReplies.length = 0;
    jobReplies.push({ state: "error", error: "disk is full", done: 0, total: 0 });
    click(fillOf(1));
    $("fsel-compress").click();
    $("fd-actions").querySelector(".primary").click();
    await waitFor(() => /disk is full/.test($("fd-body").textContent), 3000);
    check("a failed job shows its own message promptly", /disk is full/.test($("fd-body").textContent) && $("fd-actions").textContent === "Close",
      $("fd-body").textContent);
    check("it did not poll over and over", calls.filter((c) => /^\/api\/archive\/j1$/.test(c.url)).length <= 2, String(calls.length));
    $("fd-actions").querySelector("button").click();

    // -- extract
    calls.length = 0;
    jobReplies.length = 0;
    jobReplies.push({ state: "done", result: { path: "/root", names: ["b"], files: 3, skipped: 1 } });
    click(fillOf(2));
    $("fsel-extract").click();
    check("Extract opens a dialog with the two modes", /Extract b\.zip/.test($("fd-title").textContent) && $("fd-body").querySelectorAll("input[type=radio]").length === 2);
    check("extracting to a new folder is the default", $("fd-body").querySelector("input[value=folder]").checked);
    const here = $("fd-body").querySelector("input[value=here]");
    here.checked = true;
    here.dispatchEvent(new w.Event("change", { bubbles: true }));
    $("fd-actions").querySelector(".primary").click();
    await waitFor(() => calls.some((c) => c.url === "/api/archive"));
    const ex = calls.find((c) => c.url === "/api/archive").body;
    check("the extract request carries items and mode", ex.op === "extract" && ex.mode === "here" && JSON.stringify(ex.items) === '["/root/b.zip"]', JSON.stringify(ex));
    await waitFor(() => /Extracted 3 file/.test($("files-status").textContent));
    check("the result message mentions skipped entries", /Extracted 3 file\(s\) · 1 skipped/.test($("files-status").textContent), $("files-status").textContent);

    // -- properties (rendered as text, never as markup)
    calls.length = 0;
    apiReplies.props = { name: "<img src=x onerror=alert(1)>", path: "/root/b.zip", kind: "file", size: 2048, disk: 4096, mtime: 1700000000, atime: 1700000000,
      ctime: 1700000000, mode: "0644", mode_text: "-rw-r--r--", links: 1, inode: 5, readable: true, writable: true, mime: "application/zip",
      archive: { entries: 4, uncompressed: 9000 }, owner: { uid: 1000, gid: 1000, user: "ark", group: "ark" } };
    click(fillOf(2));
    $("fsel-props").click();
    await waitFor(() => !!$("fd-body").querySelector("table"));
    const txt = $("fd-body").textContent;
    check("Properties asks the server for the selected path", calls.some((c) => /^\/api\/props\?path=%2Froot%2Fb\.zip$/.test(c.url)), JSON.stringify(calls));
    check("Properties lists size, permissions, owner and archive info",
      /2\.00 KB/.test(txt) && /-rw-r--r--/.test(txt) && /ark \(1000\)/.test(txt) && /4 entries/.test(txt) && /application\/zip/.test(txt), txt);
    check("hostile file names are shown as text", !$("fd-body").querySelector("img") && !$("fd-title").querySelector("img") && /<img/.test($("fd-title").textContent));
    $("fd-actions").querySelector("button").click();
    check("Close dismisses it", $("files-dialog").hidden === true);

    // -- delete
    calls.length = 0;
    let asked = "";
    w.confirm = (m) => { asked = m; return false; };
    click(fillOf(1)); click(fillOf(0), { ctrlKey: true });
    $("fsel-delete").click();
    await sleep(40);
    check("declining the confirmation deletes nothing", !calls.some((c) => c.url === "/api/fs") && /2 items.*1 folder/.test(asked), asked);
    w.confirm = () => true;
    $("fsel-delete").click();
    await waitFor(() => calls.some((c) => c.url === "/api/fs"));
    const del = calls.find((c) => c.url === "/api/fs").body;
    check("Delete sends one batch request", del.action === "delete_many" && JSON.stringify(del.paths) === '["/root/docs","/root/a.txt"]', JSON.stringify(del));
    await waitFor(() => /Deleted 2 items/.test($("files-status").textContent));
    check("and reports the result", /Deleted 2 items/.test($("files-status").textContent) && selected().length === 0, $("files-status").textContent);
    key(list, "Delete");
    await sleep(30);
    check("the Delete key with nothing selected does nothing", calls.filter((c) => c.url === "/api/fs").length === 1);

    // -- Esc ladder
    click(fillOf(1));
    key($("files"), "Escape");
    check("Esc clears the selection, a second Esc closes the drawer", selected().length === 0 && !$("files").hidden);
    key($("files"), "Escape");
    check("second Esc closes the drawer", $("files").hidden === true);
    check("no JS errors (file manager)", errors.length === 0, errors.join("; "));
  }
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nall UI checks passed");
  process.exit(failures ? 1 : 0);
})();
