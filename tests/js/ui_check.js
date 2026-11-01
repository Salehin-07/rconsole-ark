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
    if (url.startsWith("/api/dir")) return ok({ path: "/root", parent: "/", truncated: false, max_upload_mb: 1, entries: [
      { name: "proj", path: "/root/proj", is_dir: true, size: 0, mtime: 0 },
      { name: "a b.txt", path: "/root/a b.txt", is_dir: false, size: 12, mtime: 0 }] });
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
  w.eval(fs.readFileSync(jsPath, "utf8") + "\n;window.__t = { tabs, openPty, showUpdateOverlay, openCodeEditor };");
  await sleep(150);
  return { w, d: w.document, errors, sent, saved, fileContent };
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

    // font size
    const before = parseInt($("es-fs-val").textContent, 10);
    $("es-fs-up").click();
    check("A+ makes the text bigger", parseInt($("es-fs-val").textContent, 10) === before + 1 &&
      $("editor").style.getPropertyValue("--editor-fs") === (before + 1) + "px", $("editor").style.getPropertyValue("--editor-fs"));
    check("font size is remembered", w.localStorage.getItem("rconsole.editor.fontsize") === String(before + 1));
    $("es-fs-down").click(); $("es-fs-down").click();
    check("A- makes it smaller", parseInt($("es-fs-val").textContent, 10) === before - 1);
    for (let i = 0; i < 40; i++) $("es-fs-up").click();
    check("font size is capped", $("es-fs-up").disabled === true && parseInt($("es-fs-val").textContent, 10) === 32, $("es-fs-val").textContent);

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
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nall UI checks passed");
  process.exit(failures ? 1 : 0);
})();
