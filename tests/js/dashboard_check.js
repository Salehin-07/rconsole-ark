// Renders static/dashboard.js in jsdom against real server output (dashboard_fixtures.json):
//   cd tests/js && npm install --no-save jsdom && node dashboard_check.js
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..", "..");
const fx = JSON.parse(fs.readFileSync(path.join(__dirname, "dashboard_fixtures.json"), "utf8"));
const html = fs.readFileSync(path.join(root, "templates", "dashboard.html"), "utf8")
  .replace(/\{\{[^}]*\}\}/g, "x").replace(/\{%[^%]*%\}/g, "").replace(/<script[^>]*src=[^>]*><\/script>/g, "");
let failures = 0;
const check = (name, ok, extra = "") => { console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  (" + extra + ")" : ""}`); if (!ok) failures++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/dashboard", pretendToBeVisual: true });
  const w = dom.window, d = w.document, errors = [], posts = [];
  w.addEventListener("error", (e) => errors.push(e.message));
  const ok = (j) => ({ ok: true, status: 200, json: async () => j });
  w.fetch = async (url, opts) => {
    if (url.startsWith("/api/dashboard/data")) return ok(fx[new URL(url, "http://x").searchParams.get("section")]);
    if (url.startsWith("/api/dashboard/history")) return ok(fx.history);
    if (url.startsWith("/api/dashboard/log")) return ok(fx.log);
    if (url === "/api/dashboard/action") { posts.push(JSON.parse(opts.body)); return ok({ ok: true, message: "done" }); }
    return { ok: false, status: 404, json: async () => ({}) };
  };
  w.RCONSOLE_USER = "ark"; w.scrollTo = () => {};
  w.eval(fs.readFileSync(path.join(root, "static", "dashboard.js"), "utf8"));
  const text = () => d.getElementById("dmain").textContent;
  const go = async (name) => { d.querySelector(`#dtabs [data-tab="${name}"]`).click(); await sleep(120); };
  await sleep(150);

  const dlgBtn = (i) => { const b = d.querySelectorAll(".dlg .d-actions button"); return b[i < 0 ? b.length + i : i]; };
  check("all nine tabs exist, and the header has a menu button for phones", d.querySelectorAll("#dtabs [data-tab]").length === 9 && !!d.querySelector("#dh .dh-menu"));
  check("overview shows meters and charts", /CPU/.test(text()) && /Memory/.test(text()) && d.querySelectorAll("svg.chart").length >= 6);
  check("overview charts draw lines", d.querySelectorAll("svg.chart path").length > 0);

  check("overview: meter cards carry a trend line", d.querySelectorAll(".card.meter svg.spark path").length >= 3);
  const chartSvg = d.querySelector(".chartbox svg.chart");
  chartSvg.dispatchEvent(new w.MouseEvent("pointermove", { clientX: 10, bubbles: true }));
  check("overview: hovering a chart shows a readout", d.querySelector(".chartbox .ctip") && !d.querySelector(".chartbox .ctip").hidden);
  check("tabs have icons and labels", d.querySelectorAll("#dtabs [data-tab] .ti svg").length === 9 && d.querySelectorAll("#dtabs [data-tab] .tl").length === 9);

  await go("processes");
  check("tables label every cell (used by the phone layout)", d.querySelectorAll("#dmain table td[data-label]").length > 5);
  check("processes: service listed", /bot/.test(text()) && /running/.test(text()));
  check("processes: hostile command stays text", !w.__pwned && !d.querySelector("#dmain img") && /<img/.test(text()));
  d.querySelector("#dmain table button").click();       // first action button in the services table
  await sleep(60);
  check("processes: a button posts an action", posts.length === 1 && /^service\./.test(posts[0].action), JSON.stringify(posts));
  const logBtn = [...d.querySelectorAll("#dmain button")].find((b) => b.textContent === "Logs");
  logBtn.click(); await sleep(80);
  check("processes: log dialog shows output as text", !!d.querySelector(".modal-bg pre") && /hello/.test(d.querySelector(".modal-bg pre").textContent) && !d.querySelector(".modal-bg img"));
  d.querySelector(".modal-bg button").click();
  check("processes: dialog closes", !d.querySelector(".modal-bg"));

  await go("users");
  check("users: roles and create form", /admin/.test(text()) && /alice/.test(text()) && !!d.querySelector("form.inline"));
  const rows = [...d.querySelectorAll("#dmain tbody tr")];
  const alice = rows.find((r) => /alice/.test(r.textContent)), me = rows.find((r) => /\(you\)/.test(r.textContent));
  check("users: no actions on your own row", !me.querySelector("button"));
  posts.length = 0;
  [...alice.querySelectorAll("button")].find((b) => b.textContent === "Promote").click(); await sleep(60);
  check("users: Promote asks first in a styled dialog (not the browser's)", !!d.querySelector(".dlg") && posts.length === 0);
  dlgBtn(0).click(); await sleep(40);
  check("users: Cancel closes the dialog and posts nothing", !d.querySelector(".dlg") && posts.length === 0);
  [...alice.querySelectorAll("button")].find((b) => b.textContent === "Promote").click(); await sleep(60);
  dlgBtn(-1).click(); await sleep(60);
  check("users: Promote posts user.promote", posts[0] && posts[0].action === "user.promote" && posts[0].username === "alice");
  posts.length = 0;
  const form = d.querySelector("form.inline");
  form.elements.u.value = "newbie"; form.elements.p.value = "longenough1"; form.elements.a.checked = true;
  form.dispatchEvent(new w.Event("submit", { cancelable: true })); await sleep(60);
  check("users: create form posts user.create", posts[0] && posts[0].action === "user.create" && posts[0].admin === true && posts[0].username === "newbie");

  for (const t of ["systems", "apps", "activity", "health"]) { await go(t); check(`${t}: renders`, text().length > 20); }
  check("apps: exposure shown", /\/site\//.test((await go("apps"), text())));

  // ---- repo storage page
  await go("repo");
  check("repo: hero says where files live", !!d.querySelector(".hero h2") && /live on this host|live in your GitHub repo|finish the setup/.test(d.querySelector(".hero h2").textContent));
  check("repo: four stat tiles", d.querySelectorAll(".tiles .tile").length === 4);
  check("repo: connect form has url, token, passphrase and branch", d.querySelectorAll(".step").length >= 3 && d.querySelectorAll("#dmain input[type=password]").length === 2);
  check("repo: mode is a segmented control", d.querySelectorAll(".seg button").length === 2 && !!d.querySelector(".seg button[aria-checked=true]"));
  check("repo: encryption is a switch", !!d.querySelector("#dmain .switch[role=switch]"));
  const seg = d.querySelectorAll(".seg button"); const applyBtn = [...d.querySelectorAll("#dmain button")].find((b) => b.textContent === "Apply");
  const startedOn = [...seg].findIndex((b) => b.getAttribute("aria-checked") === "true"); seg[1 - startedOn].click();
  check("repo: picking the other mode enables Apply", !applyBtn.disabled);
  posts.length = 0; applyBtn.click(); await sleep(60);
  if (d.querySelector(".dlg")) { dlgBtn(-1).click(); await sleep(60); }
  check("repo: Apply posts repo.mode", posts.some((x) => x.action === "repo.mode"), JSON.stringify(posts));
  await go("repo");
  const formBefore = d.querySelector("#dmain input[type=password]"); formBefore.value = "typed-token";
  await sleep(5300);
  check("repo: the refresh does not wipe what you are typing", d.querySelector("#dmain input[type=password]") && d.querySelector("#dmain input[type=password]").value === "typed-token");

  // ---- settings page
  await go("settings");
  check("settings: groups are collapsible sections", d.querySelectorAll("details.group").length >= 9);
  check("settings: shows real values, not blanks", [...d.querySelectorAll("#dmain input[type=number]")].every((i) => i.value !== "") && [...d.querySelectorAll("#dmain input[type=text]")].some((i) => i.value === "rconsole-storage"));
  check("settings: booleans are switches", d.querySelectorAll("#dmain .switch").length >= 10);
  check("settings: no secret value is ever in the page", !/ghp_|github_pat_/.test(d.getElementById("dmain").innerHTML));
  check("settings: save bar is hidden until something changes", !d.getElementById("dsave") || !d.getElementById("dsave").classList.contains("show"));
  const first = d.querySelector("details.group[open] .switch");
  first.click(); await sleep(30);
  check("settings: toggling shows the save bar with a count", d.getElementById("dsave").classList.contains("show") && /1 unsaved/.test(d.getElementById("dsave").textContent));
  check("settings: the changed field is marked", !!d.querySelector(".field.changed"));
  posts.length = 0;
  d.querySelector("#dsave .b.solid").click(); await sleep(80);
  check("settings: Save posts settings.save with only the change", posts.length === 1 && posts[0].action === "settings.save" && Object.keys(posts[0].changes).length === 1, JSON.stringify(posts));
  await go("settings");
  const searchBox = d.querySelector("#dmain input[type=search]"); searchBox.value = "chunk"; searchBox.dispatchEvent(new w.Event("input", { bubbles: true }));
  const shown = () => d.querySelectorAll("#dmain .g-body.cols .field:not(.hidden)").length;
  check("settings: search narrows the list", shown() >= 1 && shown() <= 3, String(shown()));
  searchBox.value = "zzzzzz"; searchBox.dispatchEvent(new w.Event("input", { bubbles: true }));
  check("settings: nothing found says so", shown() === 0 && [...d.querySelectorAll("#dmain .card.empty")].some((e) => !e.hidden));
  check("settings: leaving the page hides the save bar", (await go("overview"), !d.getElementById("dsave").classList.contains("show")));

  // ---- phone menu drawer, and Esc on dialogs
  const menu = d.querySelector("#dh .dh-menu"); menu.click(); await sleep(30);
  check("menu: the button opens a drawer listing every page", menu.getAttribute("aria-expanded") === "true" && d.querySelectorAll(".drawer .drawer-list button").length === 9);
  check("menu: the current page is marked", !!d.querySelector(".drawer .drawer-list button.on[aria-current=page]"));
  d.querySelector(".drawer-bg").dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true })); await sleep(250);
  check("menu: Esc closes it", !d.querySelector(".drawer-bg") && menu.getAttribute("aria-expanded") === "false");
  menu.click(); await sleep(30);
  [...d.querySelectorAll(".drawer .drawer-list button")].find((b) => b.textContent === "Users").click(); await sleep(250);
  check("menu: choosing a page opens it, closes the drawer and titles the header", !d.querySelector(".drawer-bg") && d.querySelector("#dh .dh-tab").textContent === "Users");
  const dlgTest = [...d.querySelectorAll("#dmain button")].find((b) => b.textContent === "Delete");
  if (dlgTest) { dlgTest.click(); await sleep(40); d.querySelector(".dlg").dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true })); await sleep(30); }
  check("dialogs close with Esc", !d.querySelector(".dlg"));
  check("no JS errors", errors.length === 0, errors.join("; "));
  process.exit(failures ? 1 : 0);
})();
