"use strict";
// Admin dashboard: plain DOM, no libraries. All text is inserted with textContent (never as markup),
// so usernames, commands and log output can never inject markup.
(() => {
  const ME = window.RCONSOLE_USER;
  // [id, label, icon, shown in the phone's bottom bar, short label for it]
  const TABS = [["overview", "Overview", "grid", 1, "Overview"], ["systems", "Systems", "server", 1, "Systems"],
                ["repo", "Repo storage", "cloud", 1, "Repo"], ["settings", "Settings", "sliders", 1, "Settings"],
                ["processes", "Processes", "list", 0], ["users", "Users", "users", 0], ["apps", "Apps", "arrows", 0],
                ["activity", "Activity", "pulse", 0], ["health", "Health", "heart", 0]];
  const cacheV = {};
  const main = document.getElementById("dmain");
  const tabsEl = document.getElementById("dtabs");
  const liveEl = document.getElementById("dh-live");
  let tab = "overview", minutes = 15, timer = null, busy = false, paused = false;

  // ---------------------------------------------------------------- helpers
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const NA = "n/a";
  const bytes = (n) => {
    if (n == null) return NA;
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i];
  };
  const pct = (n) => (n == null ? NA : n.toFixed(n < 10 ? 1 : 0) + "%");
  const dur = (s) => {
    s = Math.max(0, Math.round(s || 0));
    const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d}d ${hh}h` : hh ? `${hh}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`;
  };
  const ago = (t) => (t ? dur(Date.now() / 1000 - t) + " ago" : "never");
  const clock = (t) => new Date(t * 1000).toLocaleString();
  const level = (p) => (p >= 90 ? "bad" : p >= 70 ? "warn" : "");

  // ---------------------------------------------------------------- icons, toast, dialogs
  const ICONS = {
    check: ["M5 13l4 4L19 7"], x: ["M6 6l12 12M18 6L6 18"], info: ["M12 16v-4M12 8h.01", "M22 12a10 10 0 11-20 0 10 10 0 0120 0z"],
    alert: ["M12 9v4M12 17h.01", "M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"],
    cloud: ["M18 10a6 6 0 00-11.5-1.5A4.5 4.5 0 007 18h11a4 4 0 000-8z"],
    server: ["M4 4h16v6H4zM4 14h16v6H4z", "M8 7h.01M8 17h.01"], lock: ["M6 11V8a6 6 0 0112 0v3", "M5 11h14v10H5z"],
    sliders: ["M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3", "M1 14h6M9 8h6M17 16h6"],
    refresh: ["M20 11a8 8 0 00-14.9-3M4 4v4h4", "M4 13a8 8 0 0014.9 3M20 20v-4h-4"], chevron: ["M6 9l6 6 6-6"],
    search: ["M21 21l-4.3-4.3", "M17 10a7 7 0 11-14 0 7 7 0 0114 0z"], more: ["M5 12h.01M12 12h.01M19 12h.01"],
    grid: ["M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z"], list: ["M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"],
    users: ["M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2", "M9 11a4 4 0 100-8 4 4 0 000 8z", "M23 21v-2a4 4 0 00-3-3.9M16 3.1a4 4 0 010 7.8"],
    arrows: ["M4 8h13M13 4l4 4-4 4", "M20 16H7M11 12l-4 4 4 4"], pulse: ["M22 12h-4l-3 9L9 3l-3 9H2"],
    heart: ["M20.8 4.6a5.5 5.5 0 00-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 00-7.8 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 000-7.8z"],
    folder: ["M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"],
    box: ["M21 8l-9-5-9 5v8l9 5 9-5z", "M3 8l9 5 9-5M12 13v8"], download: ["M12 3v12m0 0l-4-4m4 4l4-4", "M4 21h16"],
    back: ["M15 18l-6-6 6-6"], menu: ["M4 6h16M4 12h16M4 18h16"], trash: ["M4 7h16M10 11v6M14 11v6", "M6 7l1 13h10l1-13M9 7V4h6v3"],
  };
  function icon(name, cls) {
    const el = svg("svg", { class: "ico" + (cls ? " " + cls : ""), viewBox: "0 0 24 24", "aria-hidden": "true" });
    for (const d of ICONS[name] || []) el.append(svg("path", { d }));
    return el;
  }
  let toastTimer;
  function toast(msg, bad) {
    const el = document.getElementById("dtoast");
    el.replaceChildren(icon(bad ? "alert" : "check"), h("span", {}, msg));
    el.className = "show" + (bad ? " bad" : "");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.className = ""; }, bad ? 6000 : 3800);
  }

  // A dialog that resolves with whatever the chosen action returns (null on Esc / backdrop). On phones it is a
  // bottom sheet. Focus moves into it and is restored afterwards; Tab stays inside.
  function dialog({ ico = "info", title, body, danger = false, field = null, actions, content = null }) {
    return new Promise((resolve) => {
      const prev = document.activeElement;
      const bg = h("div", { class: "modal-bg", role: "presentation" });
      const box = h("div", { class: "modal-box dlg" + (danger ? " danger" : ""), role: "dialog", "aria-modal": "true", "aria-labelledby": "dlg-title" });
      const close = (v) => { bg.remove(); document.body.classList.remove("noscroll"); if (prev && prev.focus) prev.focus(); resolve(v); };
      const input = field ? h("input", { type: field.type || "text", placeholder: field.placeholder || "", autocomplete: "off", spellcheck: "false",
                                         "aria-label": field.label || title, onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); main_.click(); } } }) : null;
      const btns = actions.map((a) => h("button", { type: "button", class: "b " + (a.cls || ""), onclick: () => close(a.value === "$input" ? input.value : a.value) }, a.label));
      const main_ = btns[btns.length - 1];
      box.append(h("div", { class: "d-ico" }, icon(ico)), h("h3", { id: "dlg-title" }, title),
                 body ? h("div", { class: "d-body" }, body) : null, typeof content === "function" ? content(close) : content,
                 input ? h("div", { class: "d-field" }, input) : null, h("div", { class: "d-actions" }, btns));
      bg.append(box);
      bg.addEventListener("mousedown", (e) => { if (e.target === bg) close(null); });
      bg.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { e.stopPropagation(); close(null); return; }
        if (e.key !== "Tab") return;
        const f = [...box.querySelectorAll("button, input, [tabindex='0']")].filter((x) => !x.disabled);
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      });
      document.body.append(bg);
      document.body.classList.add("noscroll");
      (input || main_).focus();
    });
  }
  const DANGER_WORDS = /delete|remove|kill|stop|reset|clear|wipe|revoke|restart|disable|signs? out|sigterm|back to/i;
  async function ask(opts) {
    const o = typeof opts === "string" ? { body: opts } : opts;
    const danger = o.danger != null ? o.danger : DANGER_WORDS.test((o.title || "") + " " + (o.body || ""));
    const r = await dialog({ ico: danger ? "alert" : "info", title: o.title || "Are you sure?", body: o.body, danger,
      actions: [{ label: o.cancel || "Cancel", value: false, cls: "ghost" }, { label: o.ok || "Continue", value: true, cls: danger ? "danger" : "solid" }] });
    return r === true;
  }
  async function askText({ title, body, type = "text", placeholder, ok = "Save" }) {
    const r = await dialog({ ico: "lock", title, body, field: { type, placeholder, label: title },
      actions: [{ label: "Cancel", value: null, cls: "ghost" }, { label: ok, value: "$input", cls: "solid" }] });
    return r;
  }

  async function api(url, opts) {
    const r = await fetch(url, opts);
    if (r.status === 401) { location = "/login"; throw new Error("unauthorized"); }
    if (r.status === 403 || r.status === 404) { location = "/console"; throw new Error("not allowed"); }
    return r;
  }
  async function act(body, { confirm: confirmOpts, refresh = true } = {}) {
    if (confirmOpts && !(await ask(confirmOpts))) return false;
    try {
      const r = await api("/api/dashboard/action", { method: "POST", headers: { "Content-Type": "application/json" },
                                                   body: JSON.stringify(body) });
      const d = await r.json();
      toast(d.message || (d.ok ? "done" : "failed"), !d.ok);
      if (refresh) load();
      return d.ok;
    } catch (e) { toast("request failed", true); return false; }
  }

  // ---------------------------------------------------------------- charts (inline SVG)
  const NS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
  }
  // series: [{key, color}], rows: samples; max: fixed top of the scale (e.g. 100) or null for auto
  function chart(rows, series, { max = null, fmt = (v) => String(v) } = {}) {
    const W = 600, H = 120, L = 36, B = 16;
    const box = h("div", { class: "chartbox" });
    const s = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img" });
    box.append(s);
    const vals = rows.flatMap((r) => series.map((x) => r[x.key]).filter((v) => v != null));
    if (rows.length < 2 || !vals.length) {
      const t = svg("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }); t.textContent = "collecting data…";
      s.append(t); return box;
    }
    let top = max != null ? max : Math.max(...vals, 1) * 1.15;
    const t0 = rows[0].t, t1 = rows[rows.length - 1].t || t0 + 1;
    const X = (t) => L + (t - t0) / Math.max(1, t1 - t0) * (W - L - 4);
    const Y = (v) => H - B - Math.min(v, top) / top * (H - B - 6);
    for (const f of [0, .5, 1]) {
      s.append(svg("line", { class: "grid", x1: L, x2: W, y1: Y(top * f), y2: Y(top * f) }));
      const t = svg("text", { x: L - 4, y: Y(top * f) + 3, "text-anchor": "end" }); t.textContent = fmt(top * f); s.append(t);
    }
    const t = svg("text", { x: W - 2, y: H - 2, "text-anchor": "end" }); t.textContent = "now"; s.append(t);
    const t2 = svg("text", { x: L, y: H - 2 }); t2.textContent = dur(t1 - t0) + " ago"; s.append(t2);
    for (const x of series) {
      const pts = rows.filter((r) => r[x.key] != null).map((r) => [X(r.t), Y(r[x.key])]);
      if (pts.length < 2) continue;
      const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
      s.append(svg("path", { d: line + ` L${pts[pts.length - 1][0].toFixed(1)} ${Y(0)} L${pts[0][0].toFixed(1)} ${Y(0)} Z`,
                             fill: x.color, "fill-opacity": ".16" }));
      s.append(svg("path", { d: line, fill: "none", stroke: x.color, "stroke-width": "1.6", "vector-effect": "non-scaling-stroke" }));
    }
    // hover / touch: a cursor line and a small readout of the nearest sample
    const cur = svg("line", { class: "cursor", y1: 6, y2: H - B, x1: 0, x2: 0, visibility: "hidden" });
    s.append(cur);
    const tip = h("div", { class: "ctip", hidden: true });
    box.append(tip);
    const hide = () => { tip.hidden = true; cur.setAttribute("visibility", "hidden"); };
    s.addEventListener("pointermove", (e) => {
      const r = s.getBoundingClientRect();
      const t = t0 + ((e.clientX - r.left) / r.width * W - L) / (W - L - 4) * (t1 - t0);
      let best = rows[0];
      for (const row of rows) if (Math.abs(row.t - t) < Math.abs(best.t - t)) best = row;
      const x = X(best.t);
      cur.setAttribute("x1", x); cur.setAttribute("x2", x); cur.setAttribute("visibility", "visible");
      tip.replaceChildren(h("b", {}, new Date(best.t * 1000).toLocaleTimeString()),
        ...series.map((q) => h("div", {}, h("i", { style: `background:${q.color}` }), `${q.name || q.key} `, best[q.key] == null ? NA : fmt(best[q.key]))));
      tip.hidden = false;
      const frac = x / W;
      tip.style.left = frac > 0.6 ? "auto" : `${(frac * 100).toFixed(1)}%`;
      tip.style.right = frac > 0.6 ? `${((1 - frac) * 100).toFixed(1)}%` : "auto";
    });
    s.addEventListener("pointerleave", hide);
    return box;
  }
  // a tiny trend line for the meter cards
  function spark(rows, key, color) {
    const pts = rows.filter((r) => r[key] != null);
    const s = svg("svg", { class: "spark", viewBox: "0 0 100 28", preserveAspectRatio: "none", "aria-hidden": "true" });
    if (pts.length < 2) return s;
    const hi = Math.max(...pts.map((r) => r[key]), 1), t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    const P = pts.map((r) => [(r.t - t0) / Math.max(1, t1 - t0) * 100, 26 - r[key] / hi * 24]);
    const line = P.map((q, i) => (i ? "L" : "M") + q[0].toFixed(1) + " " + q[1].toFixed(1)).join(" ");
    s.append(svg("path", { d: line + " L100 28 L0 28 Z", fill: color, "fill-opacity": ".15" }));
    s.append(svg("path", { d: line, fill: "none", stroke: color, "stroke-width": "1.4", "vector-effect": "non-scaling-stroke" }));
    return s;
  }
  const legend = (items) => h("div", { class: "legend" }, items.map(([c, n]) => h("span", {}, h("b", { style: `background:${c}` }), n)));

  const C = { green: "#5ee08a", blue: "#6db3ff", amber: "#ffcb6b", red: "#ff6b6b" };

  function meter(label, value, sub, p, trend) {
    return h("div", { class: "card meter " + (p != null ? level(p) : "") }, h("h3", {}, label), h("div", { class: "big" }, value),
             sub ? h("div", { class: "sub" }, sub) : null,
             p != null ? h("div", { class: "bar " + level(p) }, h("i", { style: `width:${Math.min(100, p)}%` })) : null,
             trend || null);
  }

  function table(head, rows, empty) {
    if (!rows.length) return h("div", { class: "card empty" }, empty || "Nothing here.");
    const labels = head.map((x) => x.replace(/^#/, ""));
    for (const tr of rows) [...tr.children].forEach((cell, i) => { if (labels[i]) cell.setAttribute("data-label", labels[i]); });
    return h("div", { class: "tablewrap" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, head.map((x) => h("th", { class: x.startsWith("#") ? "num" : "" }, x.replace(/^#/, ""))))),
      h("tbody", {}, rows)));
  }
  const td = (v, cls) => h("td", { class: cls || "" }, v);
  const btn = (label, fn, cls) => h("button", { class: "b " + (cls || ""), type: "button", onclick: fn }, label);
  const statePill = (st) => h("span", { class: "pill " + ({ running: "ok", ready: "ok", stopped: "", starting: "warn", backoff: "bad",
    restarting: "warn", failed: "bad", installing: "warn", updating: "warn", restoring: "warn" }[st] || "") }, st);

  async function showLog(kind, owner, id, title) {
    const bg = h("div", { class: "modal-bg", onclick: (e) => { if (e.target === bg) bg.remove(); } });
    const pre = h("pre", { class: "log" }, "loading…");
    bg.append(h("div", { class: "modal-box" }, h("div", { class: "tools" }, h("b", {}, title), h("span", { class: "sp" }),
      btn("Close", () => bg.remove())), pre));
    document.body.append(bg);
    try {
      const q = new URLSearchParams({ kind, owner, id });
      const r = await api("/api/dashboard/log?" + q);
      pre.textContent = r.ok ? ((await r.json()).text || "(no output yet)") : "(nothing to show)";
      pre.scrollTop = pre.scrollHeight;
    } catch (e) { pre.textContent = "could not load"; }
  }

  // ---------------------------------------------------------------- sections
  const VIEWS = {
    async overview(d) {
      let hist = { samples: [] };
      try { hist = await (await api("/api/dashboard/history?minutes=" + minutes)).json(); } catch (e) { /* keep empty */ }
      const rows = hist.samples, c = d.current, k = d.counts;
      const out = [];
      const alerts = d.alerts.length ? d.alerts : [{ level: "ok", text: "Everything looks healthy." }];
      out.push(h("div", { class: "alerts" }, alerts.map((a) => h("div", { class: "alert " + a.level }, h("span", { class: "ai", "aria-hidden": "true" }, { ok: "✓", info: "ℹ" }[a.level] || "⚠"), h("span", {}, a.text)))));
      out.push(h("div", { class: "dgrid" },
        meter("CPU", pct(c.cpu), c.load != null ? `load ${c.load}` : "of what this instance may use", c.cpu, spark(rows, "cpu", C.green)),
        meter("Memory", pct(c.mem_pct), c.mem != null ? `${bytes(c.mem)} of ${bytes(c.mem_total)}` : "", c.mem_pct, spark(rows, "mem_pct", C.blue)),
        meter("Disk", pct(c.disk_pct), c.disk != null ? `${bytes(c.disk)} of ${bytes(c.disk_total)}` : "", c.disk_pct, spark(rows, "disk_pct", C.amber)),
        meter("Network", c.rx != null ? "↓ " + bytes(c.rx) + "/s" : NA, c.tx != null ? "↑ " + bytes(c.tx) + "/s" : "", null, spark(rows, "rx", C.green))));
      out.push(h("div", { class: "dgrid small" },
        meter("Users", `${k.users}`, `${d.users_online} online now`, null),
        meter("Terminals", `${k.terminals}`, "open shells", null),
        meter("Jobs", `${k.jobs_running}`, "running", null),
        meter("Services", `${k.services_running}/${k.services_total}`, "running / defined", null),
        meter("Exposed apps", `${k.exposures}`, "published ports", null),
        meter("Uptime", dur(d.server.uptime), `v${d.server.version} · ${d.server.host}`, null)));
      const range = h("span", {}, [5, 15, 60].map((m) => btn(m + " min", () => { minutes = m; load(); }, m === minutes ? "solid" : "")));
      out.push(h("div", { class: "tools" }, h("b", {}, "History"), h("span", { class: "sp" }), range));
      out.push(h("div", { class: "dgrid" },
        h("div", { class: "card" }, h("h3", {}, "CPU %"), chart(rows, [{ key: "cpu", color: C.green, name: "CPU" }], { max: 100, fmt: (v) => Math.round(v) + "%" })),
        h("div", { class: "card" }, h("h3", {}, "Memory %"), chart(rows, [{ key: "mem_pct", color: C.blue, name: "Memory" }], { max: 100, fmt: (v) => Math.round(v) + "%" })),
        h("div", { class: "card" }, h("h3", {}, "Network"), chart(rows, [{ key: "rx", color: C.green, name: "down" }, { key: "tx", color: C.amber, name: "up" }], { fmt: (v) => bytes(v) }),
          legend([[C.green, "download"], [C.amber, "upload"]])),
        h("div", { class: "card" }, h("h3", {}, "Activity"), chart(rows, [{ key: "terms", color: C.blue, name: "terminals" }, { key: "jobs", color: C.amber, name: "jobs" }, { key: "svc", color: C.green, name: "services" }], { fmt: (v) => String(Math.round(v)) }),
          legend([[C.blue, "terminals"], [C.amber, "jobs"], [C.green, "services"]])),
        h("div", { class: "card" }, h("h3", {}, "Load average"), chart(rows, [{ key: "load", color: C.red, name: "load" }], { fmt: (v) => v.toFixed(1) })),
        h("div", { class: "card" }, h("h3", {}, "Processes"), chart(rows, [{ key: "procs", color: C.blue, name: "processes" }], { fmt: (v) => String(Math.round(v)) }))));
      return out;
    },

    async processes(d) {
      const out = [];
      out.push(h("h2", { class: "sec" }, "Services ", h("small", {}, "supervised bots and workers")));
      out.push(table(["Name", "Owner", "State", "Uptime", "#Restarts", "Where", "Command", ""], d.services.map((s) => h("tr", {},
        td(s.name), td(s.owner), td([statePill(s.state), s.last_error ? h("div", { class: "sub" }, s.last_error) : null]),
        td(s.state === "running" ? dur(s.uptime) : "-"), td(s.restarts, "num"), td(s.target), td(s.cmd, "cmd"),
        td(h("div", { class: "acts" },
          s.enabled ? btn("Restart", () => act({ action: "service.restart", name: s.name })) : null,
          s.enabled ? btn("Stop", () => act({ action: "service.stop", name: s.name }), "danger")
                    : btn("Start", () => act({ action: "service.start", name: s.name })),
          btn("Logs", () => showLog("service", "", s.name, `service ${s.name}`)),
          btn("Remove", () => act({ action: "service.remove", name: s.name }, { confirm: `Remove service '${s.name}' and stop it?` }), "danger"))))),
        "No services. Users create them with the `service` command."));

      out.push(h("h2", { class: "sec" }, "Jobs ", h("small", {}, "background commands")));
      out.push(table(["Owner", "Job", "State", "Runtime", "PID", "Where", "Command", ""], d.jobs.map((j) => h("tr", {},
        td(j.owner), td("%" + j.jid), td(statePill(j.alive ? "running" : "stopped")), td(dur(j.runtime)), td(j.pid || "-", "num"),
        td(j.vm || "host"), td(j.cmd, "cmd"),
        td(h("div", { class: "acts" }, btn("Output", () => showLog("job", j.owner, String(j.jid), `job %${j.jid} of ${j.owner}`)),
          j.alive ? btn("Kill", () => act({ action: "job.kill", owner: j.owner, jid: j.jid }, { confirm: `Kill job %${j.jid} of ${j.owner}?` }), "danger") : null)))),
        "No jobs."));

      out.push(h("h2", { class: "sec" }, "Terminals"));
      out.push(table(["Owner", "Tab", "Where", "Command", "Clients", "PID", ""], d.terminals.map((t) => h("tr", {},
        td(t.owner), td(t.tab), td(t.vm || "host"), td(t.cmd || "shell", "cmd"), td(t.clients, "num"), td(t.pid || "-", "num"),
        td(btn("Close", () => act({ action: "term.kill", owner: t.owner, tab: t.tab }, { confirm: `Close ${t.owner}'s terminal ${t.tab}?` }), "danger")))),
        "No open terminals."));

      out.push(h("h2", { class: "sec" }, "Host processes ", h("small", {}, d.host.supported ? `top ${d.host.rows.length} of ${d.host.total}, by CPU` : "")));
      out.push(d.host.supported ? table(["PID", "Name", "Owner", "State", "#CPU", "#Memory", "#CPU time", "Command"], d.host.rows.map((p) => h("tr", {},
        td(p.pid, "num"), td(p.name), td(p.owner || "-"), td(p.state), td(p.cpu == null ? "…" : p.cpu + "%", "num"), td(bytes(p.rss), "num"),
        td(dur(p.cpu_s), "num"), td(p.cmd, "cmd"))))
        : h("div", { class: "card muted" }, "Process listing needs Linux (/proc)."));
      return out;
    },

    async users(d) {
      const out = [];
      const form = h("form", { class: "inline", onsubmit: async (e) => {
        e.preventDefault();
        const f = e.target.elements;
        const ok = await act({ action: "user.create", username: f.u.value.trim(), password: f.p.value, admin: f.a.checked });
        if (ok) e.target.reset();
      } },
        h("input", { type: "text", name: "u", placeholder: "username", autocomplete: "off", required: true }),
        h("input", { type: "password", name: "p", placeholder: `password (min ${d.min_password})`, autocomplete: "new-password", required: true }),
        h("label", {}, h("input", { type: "checkbox", name: "a" }), "admin"),
        h("button", { class: "b solid", type: "submit" }, "Create user"));
      out.push(h("div", { class: "card" }, h("h3", {}, "New user ", h("span", { class: "sp" }), h("span", { class: "muted" }, `${d.users.length} of ${d.max_users}`)), form));
      out.push(h("h2", { class: "sec" }, "Accounts"));
      out.push(table(["User", "Role", "Seen", "#Terms", "#Jobs", "#Services", "#Apps", "#Systems", ""], d.users.map((u) => h("tr", {},
        td([h("span", { class: "dot" + (u.online ? " on" : "") }), u.name, u.you ? h("span", { class: "muted" }, " (you)") : null]),
        td(h("span", { class: "pill " + (u.admin ? "admin" : "") }, u.admin ? "admin" : "user")),
        td(u.online ? "online" : u.last_seen ? ago(u.last_seen) : "since restart: never", ""),
        td(u.terminals, "num"), td(u.jobs, "num"), td(`${u.services_running}/${u.services}`, "num"), td(u.exposures, "num"), td(u.systems, "num"),
        td(u.you ? h("span", { class: "muted" }, "—") : h("div", { class: "acts" },
          u.admin ? btn("Demote", () => act({ action: "user.demote", username: u.name }, { confirm: `Remove admin rights from '${u.name}'?` }))
                  : btn("Promote", () => act({ action: "user.promote", username: u.name }, { confirm: `Make '${u.name}' an admin? Admins can reach the host shell and this dashboard.` })),
          btn("Password", async () => { const p = await askText({ title: `New password for ${u.name}`, body: `At least ${d.min_password} characters.`, type: "password", ok: "Change password" }); if (p) act({ action: "user.password", username: u.name, password: p }); }),
          btn("Stop all", () => act({ action: "user.stop", username: u.name }, { confirm: `Kill every job, terminal and hosted app of '${u.name}'?` })),
          btn("Delete", () => act({ action: "user.delete", username: u.name }, { confirm: `Delete '${u.name}' and ALL their data, systems and services? This cannot be undone.` }), "danger"))))),
        "No users."));
      out.push(h("div", { class: "sub" }, "You cannot demote or delete yourself here; use sudo nauser / sudo ruser. The last admin can never be removed."));
      return out;
    },

    async systems(d) {
      const out = [];
      const pn = d.pid_namespaces || {};
      out.push(h("div", { class: "sub" }, pn.active ? "Each system has its own process list (PID namespaces on)." : `PID namespaces are off${pn.reason ? ": " + pn.reason : ""}; processes are found through their tracer.`));
      out.push(h("h2", { class: "sec" }, "Linux systems"));
      out.push(table(["Owner", "System", "State", "#Processes", ""], d.systems.map((v) => h("tr", {},
        td(v.owner), td([v.distro, v.version ? " " + v.version : ""]), td([statePill(v.state), v.message ? h("div", { class: "sub" }, v.message) : null]),
        td(v.proc_count, "num"), td(v.proc_count ? btn("Processes", () => showVmProcs(v)) : ""))), "No systems installed."));
      const st = d.storage;
      out.push(h("h2", { class: "sec" }, "Disk usage ", h("small", {}, st.age != null ? `measured ${dur(st.age)} ago${st.partial ? " (partial)" : ""}` : st.busy ? "measuring…" : ""),
        h("span", { class: "sp" }), btn("Measure again", () => act({ action: "storage.rescan" }))));
      const total = st.items.reduce((a, b) => a + b.bytes, 0) || 1;
      out.push(table(["Path", "#Size", "Share"], st.items.map((i) => h("tr", {}, td(i.path), td(bytes(i.bytes), "num"),
        td(h("div", { class: "bar", style: "min-width:120px" }, h("i", { style: `width:${(i.bytes / total * 100).toFixed(1)}%` }))))),
        st.busy ? "Measuring… this can take a while on a big disk." : "No data yet."));
      return out;
    },

    async apps(d) {
      const out = [];
      out.push(h("h2", { class: "sec" }, "Exposed ports ", h("small", {}, "expose <port> <name>")));
      out.push(table(["Name", "Port", "Owner", "Access", "State", ""], d.exposures.map((e) => h("tr", {},
        td(h("a", { href: `/${e.name}/`, target: "_blank", rel: "noopener", style: "color:var(--blue)" }, `/${e.name}/`)), td(e.port, "num"), td(e.owner),
        td(h("span", { class: "pill " + (e.public ? "warn" : "") }, e.public ? "public" : "login required")),
        td(h("span", { class: "pill " + (e.listening ? "ok" : "bad") }, e.listening ? "listening" : "nothing listening")),
        td(btn("Remove", () => act({ action: "expose.remove", name: e.name }, { confirm: `Stop publishing /${e.name}/?` }), "danger")))), "No exposed ports."));
      out.push(h("h2", { class: "sec" }, "Hosted apps"));
      out.push(table(["Folder", "Port", "Owner", "State", "PID"], d.hosted.map((a) => h("tr", {}, td(a.folder), td(a.port, "num"), td(a.owner),
        td(statePill(a.alive ? "running" : "stopped")), td(a.pid || "-", "num"))), "No hosted apps."));
      return out;
    },

    async activity(d) {
      return [h("div", { class: "sub" }, "Logins, logouts and every account or process change made from here or with sudo commands."),
        table(["When", "Who", "Event", "Target", "Details", "IP"], d.events.map((e) => h("tr", {},
          td(clock(e.t)), td(e.actor), td(h("span", { class: "pill " + (e.ok ? "" : "bad") }, e.action)), td(e.target), td(e.detail, "cmd"), td(e.ip || "")))
          , "No events yet.")];
    },

    // ------------------------------------------------------------ Repo storage (forms are built once so the
    // 5-second refresh never wipes what you are typing; only the status block is redrawn)
    async repo(d) {
      const st = d.status, rp = d.repo, S = d.settings;
      const busyNow = st.state && st.state !== "idle";
      const failed = !!st.last_error && !busyNow;
      let tone = "ok", ico = "cloud", title, text;
      if (busyNow) { tone = "ok"; title = ({ scanning: "Checking for changes…", uploading: "Saving your files to the repo…", restoring: "Restoring your files…", hydrating: "Bringing files back to this disk…", "re-encoding": "Re-encrypting your files…" })[st.state] || "Working…"; text = st.message || ""; }
      else if (st.blocked) { tone = "bad"; ico = "alert"; title = "Syncing is paused"; text = st.blocked; }
      else if (st.wanted && st.problems.length) { tone = "warn"; ico = "alert"; title = "Almost there: finish the setup"; text = st.problems.join(". ") + ". Until then your files stay on this host."; }
      else if (st.active) { title = "Your files live in your GitHub repo"; text = `${st.files} file${st.files === 1 ? "" : "s"} · ${bytes(st.bytes)} saved · last synced ${st.last_time ? ago(st.last_time) : "never"}`; }
      else { tone = "off"; ico = "server"; title = "Your files live on this host"; text = "They are lost if the host wipes its disk. Connect a repository below to keep them safe in GitHub."; }
      const hero = h("div", { class: "hero " + tone }, h("div", { class: "h-ico" }, icon(ico, "lg")),
        h("div", { class: "h-txt" }, h("h2", {}, title), text ? h("p", {}, text) : null,
          failed ? h("p", { class: "hint err" }, "Last sync failed: " + st.last_error) : null,
          busyNow ? h("div", { class: "meter busy" }, h("i", {})) : null,
          busyNow && st.total ? h("p", {}, `${st.done} of ${st.total}`) : null));
      const cachePct = st.cache_limit ? Math.min(100, Math.round(st.cache_bytes / st.cache_limit * 100)) : 0;
      const tiles = h("div", { class: "tiles" },
        h("div", { class: "tile" }, h("div", { class: "k" }, "Files"), h("div", { class: "v" }, String(st.files)), h("div", { class: "s" }, `${st.pieces} piece${st.pieces === 1 ? "" : "s"} in the repo`)),
        h("div", { class: "tile" }, h("div", { class: "k" }, "Saved size"), h("div", { class: "v" }, bytes(st.bytes)), h("div", { class: "s" }, st.encrypted ? "encrypted" : "plain text")),
        h("div", { class: "tile" }, h("div", { class: "k" }, "On this disk"), h("div", { class: "v" }, bytes(st.cache_bytes)),
          h("div", { class: "s" }, `of ${bytes(st.cache_limit)} cache`), h("div", { class: "meter " + level(cachePct) }, h("i", { style: `width:${cachePct}%` }))),
        h("div", { class: "tile" }, h("div", { class: "k" }, "Not on disk"), h("div", { class: "v" }, String(st.placeholders)), h("div", { class: "s" }, st.placeholders ? bytes(st.placeholder_bytes) + " fetched when opened" : "big files appear here")));
      const statusBlock = h("div", { style: "display:flex;flex-direction:column;gap:12px" },
        h("div", { class: "page-head" }, h("h2", {}, "Repo storage"), h("p", {}, "Keep your files in GitHub instead of on this host's disk.")), hero, tiles);

      if (!cacheV.repo) {
        const mk = (type, val, ph) => h("input", { type, value: val || "", placeholder: ph || "", autocomplete: type === "password" ? "new-password" : "off", spellcheck: "false", autocapitalize: "none" });
        const url = mk("text", rp.url, "https://github.com/you/rconsole-data.git");
        const tok = mk("password", "", rp.token_set ? "Saved. Type to replace" : "Paste a token with Contents: read and write");
        const pw = mk("password", "", rp.passphrase_set ? "Saved. Type to replace" : "Choose a passphrase and keep a copy");
        const br = mk("text", rp.branch, "rconsole-storage");
        const secretField = (label, inputEl, isSet, hint, what) => h("div", { class: "field" },
          h("div", { class: "field-top" }, h("label", {}, label), h("span", { class: "chip " + (isSet ? "ok" : "") }, isSet ? [icon("check"), "Saved"] : "Not set")),
          inputEl, h("p", { class: "hint" }, hint),
          isSet ? h("div", { class: "field-foot" }, h("button", { type: "button", class: "link danger", onclick: async () => {
            if (!(await ask({ title: `Remove the saved ${what}?`, body: what === "passphrase" ? "Without it nothing already stored in the repo can be read. Make sure you have a copy." : "Syncing stops until you add a token again.", ok: "Remove" }))) return;
            delete cacheV.repo; act({ action: "repo.configure", [what]: "", ["clear_" + what]: true }); } }, `Remove saved ${what}`)) : null);
        const textField = (label, inputEl, hint) => h("div", { class: "field" }, h("div", { class: "field-top" }, h("label", {}, label)), inputEl, h("p", { class: "hint" }, hint));
        const save = async () => { const b = { action: "repo.configure", url: url.value.trim(), branch: br.value.trim() };
          if (tok.value) b.token = tok.value; if (pw.value) b.passphrase = pw.value;
          delete cacheV.repo; const ok = await act(b); if (!ok) cacheV.repo = null; };
        const done = [[!!rp.url, "Repository address", rp.url ? rp.url : "Add the address of a private repository"],
                      [rp.token_set, "Access token", rp.token_set ? "Saved" : "Needs Contents: read and write"],
                      [rp.passphrase_set || !st.encrypted, "Passphrase", st.encrypted ? (rp.passphrase_set ? "Saved" : "Needed because encryption is on") : "Not needed: encryption is off"]];
        const step1 = h("div", { class: "card" }, h("div", { class: "step" }, h("span", { class: "n" }, "1"), h("div", {}, h("h3", {}, "Connect your repository"),
            h("p", { class: "sub" }, "Use a private repository. Backups share it on a different branch."))),
          h("ul", { class: "checklist", style: "margin-bottom:16px" }, done.map(([ok, t, sub]) => h("li", { class: ok ? "done" : "bad" }, h("span", { class: "dotk" }, ok ? icon("check", "sm") : null), h("div", {}, t, h("small", {}, sub))))),
          textField("Repository address", url, "The HTTPS address of your private repository."),
          secretField("Access token", tok, rp.token_set, "Create a fine-grained token limited to this repository with Contents: read and write.", "token"),
          secretField("Passphrase", pw, rp.passphrase_set, "Encrypts your files in the repo. If you lose it they cannot be read.", "passphrase"),
          textField("Storage branch", br, "Where files are kept. It must differ from the backup branch (" + rp.backup_branch + ")."),
          h("div", { style: "display:flex;gap:10px;flex-wrap:wrap;margin-top:16px" }, h("button", { type: "button", class: "b solid lg", onclick: save }, "Save connection"),
            h("button", { type: "button", class: "b lg", onclick: () => act({ action: "repo.test" }) }, "Test connection")));

        // where files live
        let mode = S.RCONSOLE_STORAGE ? S.RCONSOLE_STORAGE.value : st.mode;
        const modeNote = h("p", { class: "hint" });
        const apply = h("button", { type: "button", class: "b solid", disabled: true }, "Apply");
        const descs = { host: "Files stay on this machine's disk. If the host wipes its disk they are gone unless you restore a backup.",
                        github: "Files are saved in your repo and this disk only caches them, so a restart or a new deploy brings everything back." };
        const seg = h("div", { class: "seg", role: "radiogroup", "aria-label": "Where files live" });
        const refreshSeg = () => { for (const b of seg.children) b.setAttribute("aria-checked", String(b.dataset.v === mode)); modeNote.textContent = descs[mode]; apply.disabled = mode === st.mode; };
        for (const [v, label] of [["host", "This host"], ["github", "GitHub repo"]]) seg.append(h("button", { type: "button", role: "radio", "data-v": v, onclick: () => { mode = v; refreshSeg(); } }, label));
        apply.onclick = async () => {
          if (mode === "host" && st.placeholders && !(await ask({ title: "Move files back to this disk?", body: `${st.placeholders} large file(s) (${bytes(st.placeholder_bytes)}) will be downloaded to this host. Make sure there is enough free disk space.`, ok: "Move them back", danger: false }))) return;
          delete cacheV.repo; act({ action: "repo.mode", mode });
        };
        refreshSeg();
        const step2 = h("div", { class: "card" }, h("div", { class: "step" }, h("span", { class: "n" }, "2"), h("div", {}, h("h3", {}, "Where your files live"),
            h("p", { class: "sub" }, "Your workspace and each system's /home, /srv and /data. System folders such as /root, /etc and /app always stay on the host."))),
          seg, modeNote, h("div", { style: "margin-top:14px" }, apply));

        // encryption
        let encOn = S.RCONSOLE_STORAGE_ENCRYPT ? S.RCONSOLE_STORAGE_ENCRYPT.value === "1" : true;
        const rewrite = h("input", { type: "checkbox", id: "rw" });
        const encSw = h("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(encOn), "aria-label": "Encrypt files in the repo" });
        const encApply = h("button", { type: "button", class: "b solid", disabled: true }, "Apply");
        const encNote = h("p", { class: "hint" });
        const syncEnc = () => { encSw.setAttribute("aria-checked", String(encOn)); encApply.disabled = encOn === st.encrypted;
          encNote.textContent = encOn ? "File names and contents are encrypted with AES-256 before they leave this server." : "File names and contents are stored as plain text. Anyone with access to the repo can read them."; };
        encSw.onclick = () => { encOn = !encOn; syncEnc(); };
        encApply.onclick = () => { delete cacheV.repo; act({ action: "repo.encrypt", on: encOn, rewrite: rewrite.checked }); };
        syncEnc();
        const step3 = h("div", { class: "card" }, h("div", { class: "step" }, h("span", { class: "n" }, "3"), h("div", {}, h("h3", {}, "Encryption"))),
          h("div", { class: "field switchrow", style: "padding-top:0;border-top:0", onclick: (e) => { if (e.target.closest("button")) return; encSw.click(); } }, h("div", { class: "grow" }, h("span", { class: "field-label" }, "Encrypt files in the repo"), encNote), encSw),
          h("label", { class: "check", for: "rw" }, rewrite, h("span", {}, "Also convert the files already stored (slower)")),
          h("div", { style: "margin-top:12px" }, encApply));

        const maint = h("div", { class: "card" }, h("div", { class: "step" }, h("span", { class: "n" }, "4"), h("div", {}, h("h3", {}, "Maintenance"))),
          h("div", { class: "rows" },
            h("div", { class: "row-act" }, h("div", { class: "grow" }, h("div", { class: "t" }, "Save changes now"), h("div", { class: "d" }, "Normally files are saved every few seconds on their own.")),
              h("button", { type: "button", class: "b", onclick: () => act({ action: "repo.sync" }) }, "Sync now")),
            h("div", { class: "row-act" }, h("div", { class: "grow" }, h("div", { class: "t" }, "Bring every file back to this disk"), h("div", { class: "d" }, "Downloads all big files that are only in the repo. Needs enough free disk space.")),
              h("button", { type: "button", class: "b", onclick: () => act({ action: "repo.hydrate" }, { confirm: { title: "Download every file?", body: "All files that are only in the repo are copied to this host's disk. Make sure there is enough space.", ok: "Download all", danger: false } }) }, "Download all"))),
          h("p", { class: "hint", style: "margin-top:14px" }, "Piece size, cache size and sync timing are under Settings, in Files in your GitHub repo."));
        cacheV.repo = h("div", { style: "display:flex;flex-direction:column;gap:12px" }, step1, step2, step3, maint);
      }
      return [statusBlock, cacheV.repo];
    },

    // ------------------------------------------------------------ Settings
    async settings(d) {
      const sig = JSON.stringify(d.restart_needed);
      if (cacheV.settings && cacheV.settingsVer === sig) return cacheV.settings;
      cacheV.settingsVer = sig;
      const fields = [];
      const fmtVal = (s, v) => (s.kind === "bool" ? (v === "1" ? "On" : "Off") : (v === "" || v == null ? "the built-in default" : v + (s.unit ? " " + s.unit : "")));
      const out = [h("div", { class: "page-head" }, h("h2", {}, "Settings"),
        h("p", {}, "Every option of this console, in one place. Changes are saved on this server and apply right away unless marked “Restart to apply”."))];
      if (d.restart_needed.length) out.push(h("div", { class: "alert" }, h("span", { class: "ai" }, icon("refresh")),
        h("div", { style: "flex:1" }, h("b", {}, "Restart needed"), h("div", { class: "hint" }, "These saved changes apply after a restart: " + d.restart_needed.join(", ") + ".")),
        h("button", { type: "button", class: "b danger", onclick: () => act({ action: "server.restart" }, { confirm: { title: "Restart the server?", body: "The console and your services restart. The page reconnects in a few seconds.", ok: "Restart now" } }) }, "Restart now")));

      const dirtyInfo = () => {
        const n = fields.filter((f) => f.dirty()).length;
        for (const f of fields) f.node.classList.toggle("changed", f.dirty());
        saveBar.update(n);
      };
      const buildField = (s) => {
        let get, set, ctl, dirty;
        const orig = s.kind === "secret" ? "" : s.value;
        const node = h("div", { class: "field" + (s.kind === "bool" ? " switchrow" : "") });
        const chips = [];
        if (!s.live) chips.push(h("span", { class: "chip warn", title: "Takes effect after a restart" }, icon("refresh"), "Restart to apply"));
        if (s.source === "dashboard") chips.push(h("span", { class: "chip info" }, "Set here"));
        else if (s.source === "environment") chips.push(h("span", { class: "chip" }, "From environment"));
        const help = s.help ? h("p", { class: "hint" }, s.help) : null;
        if (s.kind === "bool") {
          const sw = h("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(orig === "1"), "aria-label": s.label });
          const flip = () => { sw.setAttribute("aria-checked", String(sw.getAttribute("aria-checked") !== "true")); dirtyInfo(); };
          sw.onclick = (e) => { e.stopPropagation(); flip(); };
          node.addEventListener("click", (e) => { if (!e.target.closest("button, a")) flip(); });
          get = () => (sw.getAttribute("aria-checked") === "true" ? "1" : "0");
          set = (v) => sw.setAttribute("aria-checked", String(v === "1"));
          node.append(h("div", { class: "grow" }, h("div", { class: "field-top" }, h("span", { class: "field-label" }, s.label), chips), help), sw);
        } else {
          let el;
          if (s.kind === "choice" && s.choices.length <= 3) {
            el = h("div", { class: "seg", role: "radiogroup", "aria-label": s.label });
            let cur = orig;
            for (const c of s.choices) el.append(h("button", { type: "button", role: "radio", "data-v": c, onclick: () => { cur = c; sync(); dirtyInfo(); } }, c));
            const sync = () => { for (const b of el.children) b.setAttribute("aria-checked", String(b.dataset.v === cur)); };
            sync(); get = () => cur; set = (v) => { cur = v; sync(); };
          } else if (s.kind === "choice") {
            el = h("select", { "aria-label": s.label }, s.choices.map((c) => h("option", { value: c }, c))); el.value = orig;
            el.addEventListener("change", dirtyInfo); get = () => el.value; set = (v) => { el.value = v; };
          } else {
            const input = h("input", { type: s.kind === "secret" ? "password" : s.kind === "int" ? "number" : "text", "aria-label": s.label, autocomplete: s.kind === "secret" ? "new-password" : "off",
              spellcheck: "false", autocapitalize: "none", inputmode: s.kind === "int" ? "numeric" : null, min: s.min, max: s.max, value: orig,
              placeholder: s.kind === "secret" ? (s.set ? "Saved. Type to replace" : "Not set") : (s.placeholder || "") });
            input.addEventListener("input", dirtyInfo);
            el = (s.kind === "int" && s.unit) ? h("div", { class: "input-wrap" }, input, h("span", { class: "unit" }, s.unit)) : input;
            if (s.kind === "int" && s.unit) input.classList.add("has-unit");
            get = () => input.value; set = (v) => { input.value = v; };
          }
          const top = h("div", { class: "field-top" }, h("label", {}, s.label), s.kind === "secret" ? h("span", { class: "chip " + (s.set ? "ok" : "") }, s.set ? [icon("check"), "Saved"] : "Not set") : null, chips);
          node.append(top, el, help);
          if (s.kind === "int" && (s.min != null || s.max != null)) node.append(h("p", { class: "hint" }, `Between ${s.min} and ${s.max}${s.unit ? " " + s.unit : ""}.`));
        }
        const foot = [];
        if (s.source === "dashboard") foot.push(h("button", { type: "button", class: "link", onclick: () => { cacheV.settings = null; act({ action: "settings.reset", names: [s.env] }); } }, `Reset to ${fmtVal(s, s.fallback)}`));
        if (s.kind === "secret" && s.set) foot.push(h("button", { type: "button", class: "link danger", onclick: async () => {
          if (await ask({ title: `Remove ${s.label.toLowerCase()}?`, body: "It is deleted from this server's settings.", ok: "Remove" })) { cacheV.settings = null; act({ action: "settings.save", changes: { [s.env]: "" } }); } } }, "Remove saved value"));
        if (foot.length) (s.kind === "bool" ? node.querySelector(".grow") : node).append(h("div", { class: "field-foot" }, foot));
        dirty = () => (s.kind === "secret" ? get() !== "" : get() !== orig);
        const f = { s, node, get, set, orig, dirty, text: (s.label + " " + (s.help || "") + " " + s.env).toLowerCase() };
        fields.push(f);
        return f;
      };

      // the sticky save bar, shared by every group
      const saveBar = (() => {
        const bar = document.getElementById("dsave") || document.body.appendChild(h("div", { id: "dsave", role: "region", "aria-label": "Unsaved changes" }));
        const msg = h("div", { class: "msg" }), n = { v: 0 };
        const discard = h("button", { type: "button", class: "b ghost", onclick: () => { for (const f of fields) { if (f.dirty()) f.set(f.s.kind === "secret" ? "" : f.orig); } dirtyInfo(); } }, "Discard");
        const save = h("button", { type: "button", class: "b solid", onclick: async () => {
          const changes = {}, bad = [];
          for (const f of fields) {
            if (!f.dirty()) continue;
            const v = f.get();
            if (f.s.kind === "int") { const x = Number(v); if (v === "" || !Number.isInteger(x) || (f.s.min != null && x < f.s.min) || (f.s.max != null && x > f.s.max)) { bad.push(f.s.label); continue; } }
            changes[f.s.env] = v;
          }
          if (bad.length) { toast(`Check: ${bad.slice(0, 3).join(", ")}`, true); return; }
          cacheV.settings = null;
          const ok = await act({ action: "settings.save", changes });
          if (!ok) { cacheV.settings = null; }
        } }, "Save changes");
        bar.replaceChildren(h("div", { class: "bar-in" }, msg, discard, save));
        const api_ = { update(count) { n.v = count; msg.replaceChildren(`${count} unsaved change${count === 1 ? "" : "s"}`, h("small", {}, "Saved on this server")); api_.sync(); },
          sync() { bar.classList.toggle("show", tab === "settings" && n.v > 0); } };
        window.__saveBarSync = api_.sync;
        return api_;
      })();

      // search
      const groupsEl = [];
      const search = h("input", { type: "search", placeholder: "Search settings", "aria-label": "Search settings", autocomplete: "off", spellcheck: "false" });
      const empty = h("div", { class: "card empty", hidden: true }, "No setting matches your search.");
      search.addEventListener("input", () => {
        const q = search.value.trim().toLowerCase(); let any = false;
        for (const g of groupsEl) {
          let vis = 0;
          for (const f of g.fields) { const show = !q || f.text.includes(q); f.node.classList.toggle("hidden", !show); if (show) vis++; }
          g.el.hidden = vis === 0; if (q && vis) g.el.open = true; any = any || vis > 0;
        }
        empty.hidden = any || !q;
      });
      out.push(h("div", { class: "searchbox" }, icon("search"), search));

      const gIcons = { storage: "cloud", backup: "lock", server: "server", login: "users", systems: "box", services: "refresh", files: "folder", keepalive: "pulse", update: "download" };
      d.groups.forEach((g, i) => {
        const fs = g.settings.map(buildField);
        const body = h("div", { class: "g-body cols" }, fs.map((f) => f.node));
        const el = h("details", { class: "group" }, h("summary", {}, h("span", { class: "g-ico" }, icon(gIcons[g.key] || "sliders")),
          h("span", { class: "g-txt" }, h("div", { class: "g-title" }, g.title), h("div", { class: "g-blurb" }, g.blurb)), h("span", { class: "g-chev" }, icon("chevron"))), body);
        if (i === 0) el.open = true;
        groupsEl.push({ el, fields: fs });
        out.push(el);
      });
      out.push(empty);
      out.push(h("details", { class: "group" }, h("summary", {}, h("span", { class: "g-ico" }, icon("lock")),
        h("span", { class: "g-txt" }, h("div", { class: "g-title" }, "Fixed settings"), h("div", { class: "g-blurb" }, "Protected on purpose; set them as environment variables.")), h("span", { class: "g-chev" }, icon("chevron"))),
        h("div", { class: "g-body" }, d.read_only.map((r) => h("div", { class: "field" }, h("div", { class: "field-top" }, h("label", {}, r.env)), h("p", { class: "hint" }, r.why))))));
      cacheV.settings = out;
      return out;
    },

    async health(d) {
      const kv = (o) => h("dl", { class: "kv" }, Object.entries(o).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v == null || v === "" ? "-" : String(v))]));
      const s = d.server, b = d.backup || {}, k = d.keepalive || {}, u = d.update || {};
      const when = (t) => (t ? ago(t) : "never");
      return [h("div", { class: "dgrid" },
        h("div", { class: "card" }, h("h3", {}, "Server"), kv({ Version: s.version, Host: s.host, Python: s.python, System: s.platform, CPUs: s.cpus, PID: s.pid,
          Threads: s.threads, Uptime: dur(s.uptime), "Data dir": s.data_dir, "Account limit": s.max_users, "Sample every": s.metrics_interval + " s" })),
        h("div", { class: "card" }, h("h3", {}, "Backups ", h("span", { class: "sp" }),
          b.configured ? btn("Back up now", () => act({ action: "backup.now" })) : null),
          kv({ Configured: b.configured ? "yes" : "no", Target: b.target, Encrypted: b.encrypted ? "yes" : "no", Interval: b.interval ? dur(b.interval) : "",
            "Last backup": when(b.last_time), "Last result": b.last_error ? "failed: " + b.last_error : b.last_time ? "ok" : "-",
            Problems: (b.problems || []).join("; ") || "none" })),
        h("div", { class: "card" }, h("h3", {}, "Keep-alive"), kv({ Enabled: k.enabled ? "yes" : "no", Interval: k.interval ? dur(k.interval) : "",
          Targets: (k.targets || []).join(", "), "Last ping": when(k.last && k.last.last_time), "Pings": k.last && k.last.pings, Failures: k.last && k.last.failures })),
        h("div", { class: "card" }, h("h3", {}, "Updates"), kv({ Installed: u.version, "Update running": u.updating ? "yes: " + u.stage : "no",
          "Last update": u.last ? `${u.last.from || "?"} → ${u.last.to || "?"} (${u.last.ok ? "ok" : "failed"}) ${when(u.last.at)}` : "none", Error: u.error })))];
    },
  };

  function showVmProcs(v) {
    const bg = h("div", { class: "modal-bg", onclick: (e) => { if (e.target === bg) bg.remove(); } });
    bg.append(h("div", { class: "modal-box" }, h("div", { class: "tools" }, h("b", {}, `${v.owner} / ${v.distro}`), h("span", { class: "sp" }), btn("Close", () => bg.remove())),
      table(["PID", "State", "#Memory", "#CPU time", "Command", ""], v.procs.filter((p) => p.pid !== 1).map((p) => h("tr", {}, td(p.pid, "num"), td(p.state), td(bytes(p.rss_kb * 1024), "num"),
        td(dur(p.cpu_s), "num"), td(p.cmd, "cmd"),
        td(btn("Stop", async () => { if (await act({ action: "vmproc.kill", owner: v.owner, vm: v.vm, pid: p.pid }, { confirm: `Send SIGTERM to ${p.pid}?` })) bg.remove(); }, "danger")))), "No processes.")));
    document.body.append(bg);
  }

  // ---------------------------------------------------------------- loop
  async function load() {
    if (busy || paused) return;
    busy = true;
    try {
      const r = await api("/api/dashboard/data?section=" + tab);
      if (!r.ok) throw new Error("bad response");
      const data = await r.json();
      const nodes = await VIEWS[tab](data);
      if (document.querySelector(".modal-bg")) { liveEl.textContent = "live"; return; }   // never redraw under an open dialog
      const y = window.scrollY, active = document.activeElement;
      const keep = active && active.closest && active.closest("form.inline") ? null : undefined;
      const same = main.children.length === nodes.length && nodes.slice(1).every((n, i) => n === main.children[i + 1]);
      if (same && nodes[0] === main.children[0]) { /* built once (settings): leave the form alone */ }
      else if (same && (tab === "repo")) main.replaceChild(nodes[0], main.children[0]);   // only the status card refreshes
      else if (keep !== null) { main.replaceChildren(...nodes); window.scrollTo(0, y); }
      liveEl.textContent = "live"; liveEl.classList.remove("off");
    } catch (e) {
      liveEl.textContent = "offline"; liveEl.classList.add("off");
    } finally { busy = false; }
  }
  function select(name) {
    tab = TABS.some((t) => t[0] === name) ? name : "overview";
    if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
    for (const b of tabsEl.children) b.classList.toggle("on", b.dataset.tab === tab);
    tabTitle.textContent = TABS.find((t) => t[0] === tab)[1];
    if (window.__saveBarSync) window.__saveBarSync();
    main.replaceChildren(h("div", { class: "muted" }, "loading…"));
    load();
  }
  for (const [id, label, ico] of TABS) tabsEl.append(h("button", { type: "button", role: "tab", "data-tab": id, onclick: () => select(id) },
    h("span", { class: "ti", "aria-hidden": "true" }, icon(ico)), h("span", { class: "tl" }, label)));

  // Phones: a menu button in the header opens a drawer with every page (the tab strip is for wide screens).
  const header = document.getElementById("dh");
  const tabTitle = h("span", { class: "dh-tab" }, "");
  const menuBtn = h("button", { type: "button", class: "dh-menu", "aria-label": "Open menu", "aria-haspopup": "dialog", "aria-expanded": "false" }, icon("menu"));
  header.prepend(menuBtn);
  header.insertBefore(tabTitle, header.querySelector(".dh-title").nextSibling);
  function openDrawer() {
    const prev = document.activeElement;
    const bg = h("div", { class: "drawer-bg", role: "presentation" });
    const close = () => { bg.classList.add("leaving"); menuBtn.setAttribute("aria-expanded", "false"); document.body.classList.remove("noscroll");
      setTimeout(() => bg.remove(), 160); if (prev && prev.focus) prev.focus(); };
    const go = (id) => { close(); select(id); };
    const nav = h("nav", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": "Dashboard menu" },
      h("div", { class: "drawer-head" }, h("span", { class: "drawer-title" }, "Dashboard"), h("button", { type: "button", class: "b ghost", "aria-label": "Close menu", onclick: close }, icon("x"))),
      h("div", { class: "drawer-list" }, TABS.map((t) => h("button", { type: "button", class: t[0] === tab ? "on" : "", "aria-current": t[0] === tab ? "page" : null, onclick: () => go(t[0]) }, icon(t[2]), t[1]))),
      h("div", { class: "drawer-foot" }, h("a", { class: "b block", href: "/console" }, icon("back", "sm"), "Back to the console"),
        h("p", { class: "hint" }, `${window.RCONSOLE_USER || ""}`)));
    bg.append(nav);
    bg.addEventListener("mousedown", (e) => { if (e.target === bg) close(); });
    bg.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); close(); return; }
      if (e.key !== "Tab") return;
      const f = [...nav.querySelectorAll("button, a")]; const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    document.body.append(bg);
    document.body.classList.add("noscroll");
    menuBtn.setAttribute("aria-expanded", "true");
    (nav.querySelector(".on") || nav.querySelector("button")).focus();
  }
  menuBtn.onclick = openDrawer;
  window.addEventListener("hashchange", () => select(location.hash.slice(1)));
  document.addEventListener("visibilitychange", () => { paused = document.hidden; if (!paused) load(); });
  timer = setInterval(load, 5000);
  select(location.hash.slice(1));
})();
