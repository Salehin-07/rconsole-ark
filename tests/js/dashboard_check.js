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
  w.confirm = () => true;
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

  check("all seven tabs exist", d.querySelectorAll("#dtabs button").length === 7);
  check("overview shows meters and charts", /CPU/.test(text()) && /Memory/.test(text()) && d.querySelectorAll("svg.chart").length >= 6);
  check("overview charts draw lines", d.querySelectorAll("svg.chart path").length > 0);

  check("overview: meter cards carry a trend line", d.querySelectorAll(".card.meter svg.spark path").length >= 3);
  const chartSvg = d.querySelector(".chartbox svg.chart");
  chartSvg.dispatchEvent(new w.MouseEvent("pointermove", { clientX: 10, bubbles: true }));
  check("overview: hovering a chart shows a readout", d.querySelector(".chartbox .ctip") && !d.querySelector(".chartbox .ctip").hidden);
  check("tabs have icons and labels", d.querySelectorAll("#dtabs .ti").length === 7 && d.querySelectorAll("#dtabs .tl").length === 7);

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
  check("users: Promote posts user.promote", posts[0] && posts[0].action === "user.promote" && posts[0].username === "alice");
  posts.length = 0;
  const form = d.querySelector("form.inline");
  form.elements.u.value = "newbie"; form.elements.p.value = "longenough1"; form.elements.a.checked = true;
  form.dispatchEvent(new w.Event("submit", { cancelable: true })); await sleep(60);
  check("users: create form posts user.create", posts[0] && posts[0].action === "user.create" && posts[0].admin === true && posts[0].username === "newbie");

  for (const t of ["systems", "apps", "activity", "health"]) { await go(t); check(`${t}: renders`, text().length > 20); }
  check("apps: exposure shown", /\/site\//.test((await go("apps"), text())));
  check("no JS errors", errors.length === 0, errors.join("; "));
  process.exit(failures ? 1 : 0);
})();
