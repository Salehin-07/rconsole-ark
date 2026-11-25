/* Rconsole database viewer: a small, read-only SQLite browser inside the file manager's viewer.
 *   window.RCDb.open(stage, { path, name, version, onFallback(msg) }) -> { destroy() }
 * The server does the reading (api/db/*, in a limited child process); this file only draws. Everything the database
 * contains is put on the page with textContent, never as HTML. */
(function () {
  "use strict";
  const h = (tag, attrs, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(String(k)));
    return el;
  };
  const fmtBytes = (n) => (n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : n < 1073741824 ? (n / 1048576).toFixed(1) + " MB" : (n / 1073741824).toFixed(2) + " GB");

  async function get(url, opts) {
    const r = await fetch(url, opts);
    let data = null;
    try { data = await r.json(); } catch (_) { /* not JSON */ }
    if (!r.ok) { const e = new Error((data && data.error) || "request failed (" + r.status + ")"); e.status = r.status; throw e; }
    return data;
  }

  // One cell: plain values as text; NULL / blobs / long text / huge numbers get a quiet marker.
  function cell(v) {
    const td = h("td", {});
    if (v === null) { td.append(h("span", { class: "dbv-null" }, "NULL")); }
    else if (typeof v === "object") {
      if ("blob" in v) { td.append(h("span", { class: "dbv-blob" }, "BLOB " + fmtBytes(v.blob)), " ", h("code", {}, v.hex + (v.blob > 24 ? "…" : ""))); }
      else if ("text" in v) { td.append(v.text, h("span", { class: "dbv-more" }, "… (" + v.len + " chars)")); td.title = v.len + " characters; the first " + v.text.length + " are shown"; }
      else if ("big" in v) { td.append(v.big); }
    } else { td.textContent = String(v); if (typeof v === "number") td.className = "num"; }
    return td;
  }
  function grid(columns, rows, opts) {
    opts = opts || {};
    const head = h("tr", {}, columns.map((c) => {
      const th = h("th", { scope: "col" }, c);
      if (opts.sort) {
        th.classList.add("dbv-sortable");
        th.tabIndex = 0;
        th.setAttribute("role", "button");
        if (opts.sort.col === c) th.append(h("span", { class: "dbv-arrow" }, opts.sort.desc ? " ▼" : " ▲"));
        const go = () => opts.sort.on(c);
        th.addEventListener("click", go);
        th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
      }
      return th;
    }));
    const body = rows.map((r) => h("tr", {}, r.map(cell)));
    return h("div", { class: "dbv-gridwrap" }, h("table", { class: "dbv-grid" }, h("thead", {}, head), h("tbody", {}, body)));
  }

  function open(stage, o) {
    const base = "/api/db";
    const q = (path) => encodeURIComponent(path);
    let info = null, current = null, tab = "browse", dead = false;
    const st = { offset: 0, limit: 100, order: null, desc: false, q: "" };
    let reqId = 0;

    const root = h("div", { class: "dbv" });
    const tabs = h("div", { class: "dbv-tabs", role: "tablist" });
    const select = h("select", { class: "dbv-select", "aria-label": "Table" });
    const meta = h("span", { class: "dbv-meta" });
    const side = h("nav", { class: "dbv-side", "aria-label": "Tables" });
    const main = h("div", { class: "dbv-main" });
    const bar = h("div", { class: "dbv-bar" }, tabs, select, h("span", { class: "dbv-sp" }), meta);
    root.append(bar, h("div", { class: "dbv-body" }, side, main));
    stage.append(root);

    const tabDefs = [["browse", "Browse"], ["structure", "Structure"], ["query", "Query"]];
    const tabBtns = {};
    for (const [id, label] of tabDefs) {
      tabBtns[id] = h("button", { type: "button", role: "tab", onclick: () => setTab(id) }, label);
      tabs.append(tabBtns[id]);
    }
    function setTab(id) {
      tab = id;
      for (const k of Object.keys(tabBtns)) tabBtns[k].setAttribute("aria-selected", String(k === id));
      select.hidden = id === "query";
      render();
    }
    function status(text, bad) { return h("div", { class: "dbv-status" + (bad ? " bad" : "") }, text); }
    function busy(text) { main.replaceChildren(h("div", { class: "dbv-status" }, h("span", { class: "pdfv-spin" }), " " + (text || "Loading…"))); }

    function pickTable(name) {
      current = info.tables.find((t) => t.name === name) || null;
      st.offset = 0; st.order = null; st.desc = false; st.q = "";
      select.value = name || "";
      for (const b of side.querySelectorAll("button")) b.classList.toggle("on", b.dataset.t === name);
      render();
    }
    select.addEventListener("change", () => pickTable(select.value));

    function render() {
      if (dead) return;
      if (tab === "query") return renderQuery();
      if (!current) { main.replaceChildren(status(info.tables.length ? "Pick a table." : "This database has no tables.")); return; }
      return tab === "structure" ? renderStructure() : renderBrowse();
    }

    async function renderBrowse() {
      const id = ++reqId;
      const t = current;
      const search = h("input", { class: "dbv-search", type: "search", placeholder: "Search this table…", value: st.q, "aria-label": "Search rows", maxlength: "100" });
      let timer = null;
      search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { st.q = search.value; st.offset = 0; loadRows(); }, 350); });
      const pager = h("div", { class: "dbv-pager" });
      const holder = h("div", { class: "dbv-holder" });
      main.replaceChildren(h("div", { class: "dbv-tools" }, h("b", {}, t.name), search, pager), holder);
      async function loadRows() {
        const my = ++reqId;
        holder.replaceChildren(status("Loading…"));
        try {
          const url = `${base}/table?path=${q(o.path)}&table=${q(t.name)}&offset=${st.offset}&limit=${st.limit}` +
            (st.order ? `&order=${q(st.order)}&dir=${st.desc ? "desc" : "asc"}` : "") + (st.q ? `&q=${q(st.q)}` : "");
          const d = await get(url);
          if (dead || my !== reqId) return;
          holder.replaceChildren(d.rows.length ? grid(d.columns, d.rows, { sort: { col: st.order, desc: st.desc, on: (c) => {
            if (st.order === c) st.desc = !st.desc; else { st.order = c; st.desc = false; }
            st.offset = 0; loadRows();
          } } }) : status(st.q ? "No row matches your search." : "This table is empty."));
          const from = d.rows.length ? d.offset + 1 : 0, to = d.offset + d.rows.length;
          const total = d.total != null ? (d.capped ? "more than " + d.total.toLocaleString() : d.total.toLocaleString()) : null;
          const prev = h("button", { type: "button", disabled: d.offset === 0, onclick: () => { st.offset = Math.max(0, st.offset - st.limit); loadRows(); } }, "‹ Prev");
          const next = h("button", { type: "button", disabled: !d.more, onclick: () => { st.offset += st.limit; loadRows(); } }, "Next ›");
          pager.replaceChildren(h("span", { class: "dbv-count" }, d.rows.length ? `${from.toLocaleString()}–${to.toLocaleString()}${total ? " of " + total : ""}` : ""), prev, next);
          holder.scrollTop = 0;
        } catch (e) { if (!dead && my === reqId) holder.replaceChildren(status(e.message, true)); }
      }
      if (id === reqId) loadRows();
    }

    function renderStructure() {
      const t = current;
      const cols = h("table", { class: "dbv-grid" }, h("thead", {}, h("tr", {}, ["Column", "Type", "Key", "Not null", "Default"].map((x) => h("th", {}, x)))),
        h("tbody", {}, t.columns.map((c) => h("tr", {}, h("td", {}, c.name), h("td", {}, c.type || "-"), h("td", {}, c.pk ? "primary" : ""), h("td", {}, c.notnull ? "yes" : ""), h("td", {}, c.default == null ? "" : String(c.default))))));
      const extra = info.other.filter((x) => x.table === t.name);
      main.replaceChildren(h("div", { class: "dbv-tools" }, h("b", {}, t.name), h("span", { class: "dbv-count" }, t.type + (t.rows != null ? " · " + (t.rows > info.count_cap ? "more than " + info.count_cap.toLocaleString() : t.rows.toLocaleString()) + " rows" : ""))),
        h("div", { class: "dbv-holder" }, h("div", { class: "dbv-gridwrap" }, cols), h("h4", {}, "CREATE statement"), h("pre", { class: "dbv-sql" }, t.sql || "(none)"),
          extra.length ? h("h4", {}, "Indexes and triggers") : null, extra.map((x) => h("pre", { class: "dbv-sql" }, "-- " + x.type + " " + x.name + "\n" + (x.sql || "(automatic index)")))));
    }

    function renderQuery() {
      const box = h("textarea", { class: "dbv-sqlbox", spellcheck: "false", autocomplete: "off", autocapitalize: "off", rows: "4", "aria-label": "SQL query",
        placeholder: "SELECT * FROM " + ((current && current.name) || "table") + " LIMIT 50" });
      box.value = renderQuery.last || "";
      const out = h("div", { class: "dbv-holder" }, status("Read-only: SELECT, WITH, EXPLAIN and read-only PRAGMA. Ctrl/⌘ + Enter runs it."));
      const run = h("button", { type: "button", class: "dbv-run" }, "▶ Run");
      async function go() {
        renderQuery.last = box.value;
        const my = ++reqId;
        run.disabled = true;
        out.replaceChildren(status("Running…"));
        try {
          const d = await get(base + "/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: o.path, sql: box.value }) });
          if (dead || my !== reqId) return;
          out.replaceChildren(d.columns.length ? grid(d.columns, d.rows) : status(d.note || "Done."),
            h("div", { class: "dbv-count dbv-foot" }, `${d.rows.length.toLocaleString()} row${d.rows.length === 1 ? "" : "s"}${d.more ? " (more exist: add LIMIT or WHERE)" : ""} · ${d.ms} ms`));
        } catch (e) { if (!dead && my === reqId) out.replaceChildren(status(e.message, true)); }
        finally { run.disabled = false; }
      }
      run.onclick = go;
      box.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); go(); } e.stopPropagation(); });
      main.replaceChildren(h("div", { class: "dbv-querybar" }, box, run), out);
    }

    (async function boot() {
      busy("Opening database…");
      try {
        info = await get(`${base}/info?path=${q(o.path)}`);
        if (dead) return;
        const shown = info.tables.filter((t) => !t.internal);
        meta.textContent = `${fmtBytes(info.size)} · SQLite ${info.sqlite_version} · read-only` + (info.stale ? " · write-ahead log not applied" : "");
        for (const t of shown) {
          side.append(h("button", { type: "button", "data-t": t.name, onclick: () => { if (tab === "query") setTab("browse"); pickTable(t.name); } },
            h("span", { class: "dbv-tn" }, t.name), h("small", {}, t.type === "view" ? "view" : t.rows == null ? "" : t.rows > info.count_cap ? info.count_cap.toLocaleString() + "+" : t.rows.toLocaleString())));
          select.append(h("option", { value: t.name }, t.name));
        }
        info.tables = shown;
        setTab("browse");
        if (shown.length) pickTable(shown[0].name); else render();
      } catch (e) {
        if (dead) return;
        if (o.onFallback) o.onFallback(e.message); else main.replaceChildren(status(e.message, true));
      }
    })();
    return { destroy() { dead = true; reqId++; root.remove(); } };
  }
  window.RCDb = { open };
})();
