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
  const w = dom.window, d = w.document, errors = [], posts = [], modelReqs = [];
  w.addEventListener("error", (e) => errors.push(e.message));
  const ok = (j) => ({ ok: true, status: 200, json: async () => j });
  w.fetch = async (url, opts) => {
    if (url.startsWith("/api/dashboard/data")) return ok(fx[new URL(url, "http://x").searchParams.get("section")]);
    if (url.startsWith("/api/dashboard/history")) return ok(fx.history);
    if (url.startsWith("/api/dashboard/log")) return ok(fx.log);
    if (url === "/api/agent/models") { const b = JSON.parse(opts.body); modelReqs.push(b); return ok({ models: ["alpha-1", "beta-2", "gamma-3"], provider: b.provider }); }
    if (url === "/api/dashboard/action") { posts.push(JSON.parse(opts.body)); return ok({ ok: true, message: "done" }); }
    return { ok: false, status: 404, json: async () => ({}) };
  };
  w.RCONSOLE_USER = "ark"; w.RCONSOLE_ADMIN = true; w.scrollTo = () => {};
  w.eval(fs.readFileSync(path.join(root, "static", "dashboard.js"), "utf8"));
  const text = () => d.getElementById("dmain").textContent;
  const go = async (name) => { d.querySelector(`#dtabs [data-tab="${name}"]`).click(); await sleep(120); };
  await sleep(150);

  const dlgBtn = (i) => { const b = d.querySelectorAll(".dlg .d-actions button"); return b[i < 0 ? b.length + i : i]; };
  check("all eleven admin tabs exist, and the header has a menu button for phones", d.querySelectorAll("#dtabs [data-tab]").length === 11 && !!d.querySelector("#dh .dh-menu"));
  check("overview shows meters and charts", /CPU/.test(text()) && /Memory/.test(text()) && d.querySelectorAll("svg.chart").length >= 6);
  check("overview charts draw lines", d.querySelectorAll("svg.chart path").length > 0);

  check("overview: no element mixes the stat-card class with the progress-bar class (v1.5.2 collision)", d.querySelectorAll(".card.meter").length === 0);
  check("overview: meter cards carry a trend line", d.querySelectorAll(".card.stat svg.spark path").length >= 3);
  const chartSvg = d.querySelector(".chartbox svg.chart");
  chartSvg.dispatchEvent(new w.MouseEvent("pointermove", { clientX: 10, bubbles: true }));
  check("overview: hovering a chart shows a readout", d.querySelector(".chartbox .ctip") && !d.querySelector(".chartbox .ctip").hidden);
  check("tabs have icons and labels", d.querySelectorAll("#dtabs [data-tab] .ti svg").length === 11 && d.querySelectorAll("#dtabs [data-tab] .tl").length === 11);

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
  check("menu: the button opens a drawer listing every page (admin: eleven)", menu.getAttribute("aria-expanded") === "true" && d.querySelectorAll(".drawer .drawer-list button").length === 11);
  check("menu: the current page is marked", !!d.querySelector(".drawer .drawer-list button.on[aria-current=page]"));
  d.querySelector(".drawer-bg").dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true })); await sleep(250);
  check("menu: Esc closes it", !d.querySelector(".drawer-bg") && menu.getAttribute("aria-expanded") === "false");
  menu.click(); await sleep(30);
  [...d.querySelectorAll(".drawer .drawer-list button")].find((b) => b.textContent === "Users").click(); await sleep(250);
  check("menu: choosing a page opens it, closes the drawer and titles the header", !d.querySelector(".drawer-bg") && d.querySelector("#dh .dh-tab").textContent === "Users");
  const dlgTest = [...d.querySelectorAll("#dmain button")].find((b) => b.textContent === "Delete");
  if (dlgTest) { dlgTest.click(); await sleep(40); d.querySelector(".dlg").dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true })); await sleep(30); }
  check("dialogs close with Esc", !d.querySelector(".dlg"));
  // ---- AI agent page
  await go("agent");
  const prov = d.querySelectorAll("#dmain details.prov");
  check("agent: a card per provider plus the add-your-own card", prov.length >= 17, String(prov.length));
  check("agent: OpenRouter and Hugging Face are offered", /OpenRouter/.test(text()) && /Hugging Face/.test(text()));
  check("agent: the saved key is never in the page, the secret header is masked", !/or-key-1234567/.test(d.getElementById("dmain").innerHTML) && !/s3cret/.test(d.getElementById("dmain").innerHTML));
  check("agent: the default provider is marked and a ready one says so", /Default/.test(text()) && /Ready/.test(text()));
  check("agent: the env variable names are listed", /HF_TOKEN/.test(text()) && /OPENROUTER_API_KEY/.test(text()));
  const hf = Array.from(prov).find((c) => /Hugging Face/.test(c.querySelector("summary").textContent));
  const keyBox = hf.querySelector("input[type=password]");
  keyBox.value = "hf_abc123"; keyBox.dispatchEvent(new w.Event("input"));
  const saveBtn = Array.from(hf.querySelectorAll(".prov-actions button")).find((b) => b.textContent === "Save");
  posts.length = 0; saveBtn.click(); await sleep(60);
  check("agent: Save posts the key and settings to the agent.save action", posts.some((p) => p.action === "agent.save" && p.id === "huggingface" && p.api_key === "hf_abc123" && /router\.huggingface\.co/.test(p.base)), JSON.stringify(posts[0] || {}));
  const orCard = Array.from(d.querySelectorAll("#dmain details.prov")).find((c) => /OpenRouter/.test(c.querySelector("summary").textContent));
  posts.length = 0; Array.from(orCard.querySelectorAll(".prov-actions button")).find((b) => b.textContent === "Test").click(); await sleep(60);
  check("agent: Test posts agent.test for that provider", posts.some((p) => p.action === "agent.test" && p.id === "openrouter"));
  // model browser: a real searchable dialog (a native datalist does not work on phones)
  const orKey = orCard.querySelector('input[type="password"]'); if (orKey) { orKey.value = "typed-not-saved"; orKey.dispatchEvent(new w.Event("input")); }
  Array.from(orCard.querySelectorAll("button")).find((b) => /Browse/.test(b.textContent)).click(); await sleep(80);
  check("model browser: opens a dialog listing the models, no datalist anywhere", d.querySelectorAll(".dlg .mp-row").length === 3 && !d.querySelector("datalist"));
  check("model browser: asks for that provider and sends the unsaved key", modelReqs.length === 1 && modelReqs[0].provider === "openrouter" && (!orKey || modelReqs[0].api_key === "typed-not-saved"), JSON.stringify(modelReqs));
  const mq = d.querySelector(".dlg .mp-q"); mq.value = "beta"; mq.dispatchEvent(new w.Event("input"));
  const rowsNow = Array.from(d.querySelectorAll(".dlg .mp-row b")).map((b) => b.textContent);
  check("model browser: filters, and offers the typed text as a custom id", rowsNow.includes("beta-2") && !rowsNow.includes("alpha-1") && rowsNow[0] === "beta");
  Array.from(d.querySelectorAll(".dlg .mp-row")).find((r) => r.textContent.includes("beta-2")).click(); await sleep(60);
  const modelInput = Array.from(orCard.querySelectorAll(".mp-field input"))[0];
  check("model browser: picking fills the model field and closes the dialog", modelInput && modelInput.value === "beta-2" && !d.querySelector(".dlg .mp"));
  const customCard = Array.from(d.querySelectorAll("#dmain details.prov")).find((c) => /My gateway/.test(c.querySelector("summary").textContent));
  check("agent: a custom provider shows its address and a Delete button", customCard && customCard.querySelector("input[value='http://localhost:8000/v1']") && Array.from(customCard.querySelectorAll("button")).some((b) => b.textContent === "Delete"));
  const addCard = Array.from(d.querySelectorAll("#dmain details.prov")).find((c) => /Add your own provider/.test(c.textContent));
  const ins = addCard.querySelectorAll("input");
  ins[0].value = "Mine"; ins[1].value = ""; ins[2].value = "https://x.example/v1"; ins[3].value = "k-123"; ins[4].value = "m1";
  posts.length = 0; Array.from(addCard.querySelectorAll("button")).find((b) => b.textContent === "Add provider").click(); await sleep(60);
  check("agent: adding a provider derives its id and posts the key", posts.some((p) => p.action === "agent.save" && p.id === "mine" && p.api_key === "k-123" && p.base === "https://x.example/v1"), JSON.stringify(posts[0] || {}));
  const optBtn = Array.from(d.querySelectorAll("#dmain button")).find((b) => b.textContent === "Save options");
  posts.length = 0; optBtn.click(); await sleep(60);
  check("agent: options save posts every number", posts.some((p) => p.action === "agent.options" && p.max_steps === "40" && p.mode === "ask"), JSON.stringify(posts[0] || {}));

  check("no JS errors", errors.length === 0, errors.join("; "));
  process.exit(failures ? 1 : 0);
})();
