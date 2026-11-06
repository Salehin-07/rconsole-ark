"use strict";
// Admin dashboard: plain DOM, no libraries. All text is inserted with textContent (never as markup),
// so usernames, commands and log output can never inject markup.
(() => {
  const ME = window.RCONSOLE_USER;
  const TABS = [["overview", "Overview"], ["processes", "Processes"], ["users", "Users"], ["systems", "Systems"],
                ["apps", "Apps"], ["activity", "Activity"], ["health", "Health"]];
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

  let toastTimer;
  function toast(msg, bad) {
    const el = document.getElementById("dtoast");
    el.textContent = msg; el.className = "show" + (bad ? " bad" : "");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.className = ""; }, 3500);
  }

  async function api(url, opts) {
    const r = await fetch(url, opts);
    if (r.status === 401) { location = "/login"; throw new Error("unauthorized"); }
    if (r.status === 403 || r.status === 404) { location = "/console"; throw new Error("not allowed"); }
    return r;
  }
  async function act(body, { confirm: ask, refresh = true } = {}) {
    if (ask && !window.confirm(ask)) return false;
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
    const W = 600, H = 110, L = 34, B = 14;
    const s = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img" });
    const vals = rows.flatMap((r) => series.map((x) => r[x.key]).filter((v) => v != null));
    if (rows.length < 2 || !vals.length) {
      const t = svg("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }); t.textContent = "collecting data…";
      s.append(t); return s;
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
    return s;
  }
  const legend = (items) => h("div", { class: "legend" }, items.map(([c, n]) => h("span", {}, h("b", { style: `background:${c}` }), n)));

  const C = { green: "#5ee08a", blue: "#6db3ff", amber: "#ffcb6b", red: "#ff6b6b" };

  function meter(label, value, sub, p) {
    return h("div", { class: "card" }, h("h3", {}, label), h("div", { class: "big" }, value),
             sub ? h("div", { class: "sub" }, sub) : null,
             p != null ? h("div", { class: "bar " + level(p) }, h("i", { style: `width:${Math.min(100, p)}%` })) : null);
  }

  function table(head, rows, empty) {
    if (!rows.length) return h("div", { class: "card muted" }, empty || "Nothing here.");
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
      out.push(h("div", { class: "alerts" }, alerts.map((a) => h("div", { class: "alert " + a.level }, a.text))));
      out.push(h("div", { class: "dgrid" },
        meter("CPU", pct(c.cpu), c.load != null ? `load ${c.load}` : "of what this instance may use", c.cpu),
        meter("Memory", pct(c.mem_pct), c.mem != null ? `${bytes(c.mem)} of ${bytes(c.mem_total)}` : "", c.mem_pct),
        meter("Disk", pct(c.disk_pct), c.disk != null ? `${bytes(c.disk)} of ${bytes(c.disk_total)}` : "", c.disk_pct),
        meter("Network", c.rx != null ? "↓ " + bytes(c.rx) + "/s" : NA, c.tx != null ? "↑ " + bytes(c.tx) + "/s" : "", null)));
      out.push(h("div", { class: "dgrid" },
        meter("Users", `${k.users}`, `${d.users_online} online now`, null),
        meter("Terminals", `${k.terminals}`, "open shells", null),
        meter("Jobs", `${k.jobs_running}`, "running", null),
        meter("Services", `${k.services_running}/${k.services_total}`, "running / defined", null),
        meter("Exposed apps", `${k.exposures}`, "published ports", null),
        meter("Uptime", dur(d.server.uptime), `v${d.server.version} · ${d.server.host}`, null)));
      const range = h("span", {}, [5, 15, 60].map((m) => btn(m + " min", () => { minutes = m; load(); }, m === minutes ? "solid" : "")));
      out.push(h("div", { class: "tools" }, h("b", {}, "History"), h("span", { class: "sp" }), range));
      out.push(h("div", { class: "dgrid" },
        h("div", { class: "card" }, h("h3", {}, "CPU %"), chart(rows, [{ key: "cpu", color: C.green }], { max: 100, fmt: (v) => Math.round(v) + "%" })),
        h("div", { class: "card" }, h("h3", {}, "Memory %"), chart(rows, [{ key: "mem_pct", color: C.blue }], { max: 100, fmt: (v) => Math.round(v) + "%" })),
        h("div", { class: "card" }, h("h3", {}, "Network"), chart(rows, [{ key: "rx", color: C.green }, { key: "tx", color: C.amber }], { fmt: (v) => bytes(v) }),
          legend([[C.green, "download"], [C.amber, "upload"]])),
        h("div", { class: "card" }, h("h3", {}, "Activity"), chart(rows, [{ key: "terms", color: C.blue }, { key: "jobs", color: C.amber }, { key: "svc", color: C.green }], { fmt: (v) => String(Math.round(v)) }),
          legend([[C.blue, "terminals"], [C.amber, "jobs"], [C.green, "services"]])),
        h("div", { class: "card" }, h("h3", {}, "Load average"), chart(rows, [{ key: "load", color: C.red }], { fmt: (v) => v.toFixed(1) })),
        h("div", { class: "card" }, h("h3", {}, "Processes"), chart(rows, [{ key: "procs", color: C.blue }], { fmt: (v) => String(Math.round(v)) }))));
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
          btn("Password", async () => { const p = window.prompt(`New password for '${u.name}' (min ${d.min_password} characters):`); if (p) act({ action: "user.password", username: u.name, password: p }); }),
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
      if (keep !== null) { main.replaceChildren(...nodes); window.scrollTo(0, y); }
      liveEl.textContent = "live"; liveEl.classList.remove("off");
    } catch (e) {
      liveEl.textContent = "offline"; liveEl.classList.add("off");
    } finally { busy = false; }
  }
  function select(name) {
    tab = TABS.some((t) => t[0] === name) ? name : "overview";
    if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
    for (const b of tabsEl.children) b.classList.toggle("on", b.dataset.tab === tab);
    main.replaceChildren(h("div", { class: "muted" }, "loading…"));
    load();
  }
  for (const [id, label] of TABS) tabsEl.append(h("button", { type: "button", role: "tab", "data-tab": id, onclick: () => select(id) }, label));
  window.addEventListener("hashchange", () => select(location.hash.slice(1)));
  document.addEventListener("visibilitychange", () => { paused = document.hidden; if (!paused) load(); });
  timer = setInterval(load, 5000);
  select(location.hash.slice(1));
})();
