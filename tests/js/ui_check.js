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
  const targets = { current: null, label: "choose-vm", supported: true,
    targets: [{ id: "vm:alpine", label: "Alpine", kind: "vm" }, { id: "vm:debian", label: "Debian", kind: "vm" }],
    installing: [], installable: [{ id: "kali", name: "Kali Linux", size: "~140 MB" }] };
  w.fetch = async (url, opts) => {
    if (url.startsWith("/api/tabs")) return ok({ tabs: [{ id: "main", name: "main", cwd: "/root", is_terminal: false, alive: false, cmd: "" }] });
    if (url.startsWith("/api/targets")) return ok(targets);
    if (url.startsWith("/api/target")) return ok({ ok: true, ...targets, current: "vm:debian", label: "debian", cwds: { main: "/root" } });
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
  w.eval(fs.readFileSync(jsPath, "utf8") + "\n;window.__t = { tabs, openPty };");
  await sleep(150);
  return { w, d: w.document, errors, sent };
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
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nall UI checks passed");
  process.exit(failures ? 1 : 0);
})();
