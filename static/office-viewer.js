/* Office preview for the file browser: .docx (Word), .xlsx/.xlsm/.xls (Excel), .pptx (PowerPoint text).
 * Everything is parsed in the browser (bundled mammoth / SheetJS / JSZip in static/vendor/office, loaded on first use)
 * and shown inside a sandboxed iframe with no scripts and no network, so a hostile document cannot touch the console.
 * window.RCOffice.open(stage, url, {kind, name, version, onFallback}) -> {destroy}. */
(function () {
  "use strict";
  const MAX_BYTES = 40 * 1024 * 1024;          // bigger files are offered as a download instead
  const MAX_ROWS = 2000, MAX_COLS = 60;        // one sheet is cut here so a huge workbook cannot freeze the tab
  const LIBS = {
    docx: ["/static/vendor/office/mammoth.browser.min.js"],
    xlsx: ["/static/vendor/office/xlsx.full.min.js"],
    pptx: ["/static/vendor/office/jszip.min.js"],
  };
  const loaded = {};
  function loadScript(src, version) {
    if (!loaded[src]) {
      loaded[src] = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = src + (version ? "?v=" + encodeURIComponent(version) : "");
        s.onload = () => resolve();
        s.onerror = () => { delete loaded[src]; reject(new Error("could not load " + src)); };
        document.head.appendChild(s);
      });
    }
    return loaded[src];
  }
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const CSS = "html{background:#fff}body{font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2328;margin:0;padding:20px 22px 40px;max-width:860px;margin:auto;overflow-wrap:anywhere}" +
    "img{max-width:100%;height:auto}table{border-collapse:collapse;margin:10px 0}td,th{border:1px solid #c9ced6;padding:4px 8px;vertical-align:top}" +
    "h1,h2,h3,h4{line-height:1.25}a{color:#0969da}.sheet{max-width:none;padding:0}.sheet table{margin:0;font-size:13px;white-space:pre-wrap}" +
    ".sheet td,.sheet th{min-width:48px}.note{color:#656d76;font-size:12px;padding:8px 10px}" +
    ".slide{border:1px solid #c9ced6;border-radius:10px;padding:16px 20px;margin:0 0 16px;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.08)}" +
    ".slide h2{margin:0 0 8px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#656d76}.slide p{margin:4px 0}.slide .t{font-size:18px;font-weight:700}" +
    ".empty{color:#656d76;font-style:italic}";
  function doc(body, cls) {
    return "<!doctype html><html><head><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; style-src 'unsafe-inline'\">" +
      "<style>" + CSS + "</style></head><body class=\"" + (cls || "") + "\">" + body + "</body></html>";
  }
  function frame(title) {
    const f = document.createElement("iframe");
    f.className = "viewer-office";
    f.title = title || "document";
    f.setAttribute("sandbox", "");               // no scripts, no same-origin, no forms, no popups
    f.referrerPolicy = "no-referrer";
    return f;
  }
  const ext = (n) => ((n || "").split(".").pop() || "").toLowerCase();

  async function fetchBuffer(url, signal) {
    const r = await fetch(url, { credentials: "same-origin", signal });
    if (!r.ok) throw new Error(r.status === 404 ? "This file no longer exists." : "The server could not send this file (" + r.status + ").");
    const len = Number(r.headers.get("Content-Length") || 0);
    if (len > MAX_BYTES) throw new Error("This file is too large to preview (" + Math.round(len / 1048576) + " MB).");
    const buf = await r.arrayBuffer();
    if (buf.byteLength > MAX_BYTES) throw new Error("This file is too large to preview.");
    return buf;
  }

  async function renderDocx(stage, buf, name) {
    const res = await window.mammoth.convertToHtml({ arrayBuffer: buf });
    const f = frame(name);
    const html = res.value && res.value.trim() ? res.value : "<p class=\"empty\">This document has no text to show.</p>";
    f.srcdoc = doc(html);
    stage.appendChild(f);
  }

  function sheetHtml(ws) {
    const XLSX = window.XLSX;
    if (!ws || !ws["!ref"]) return "<p class=\"empty\">This sheet is empty.</p>";
    const range = XLSX.utils.decode_range(ws["!ref"]);
    let cut = "";
    if (range.e.r - range.s.r + 1 > MAX_ROWS) { range.e.r = range.s.r + MAX_ROWS - 1; cut += " first " + MAX_ROWS + " rows"; }
    if (range.e.c - range.s.c + 1 > MAX_COLS) { range.e.c = range.s.c + MAX_COLS - 1; cut += (cut ? " and" : "") + " first " + MAX_COLS + " columns"; }
    const copy = Object.assign({}, ws, { "!ref": XLSX.utils.encode_range(range) });
    return (cut ? "<div class=\"note\">Large sheet: showing the" + cut + ". Download the file to see everything.</div>" : "") +
      XLSX.utils.sheet_to_html(copy, { header: "", footer: "" }).replace(/^[\s\S]*?<body>|<\/body>[\s\S]*$/gi, "");
  }
  async function renderXlsx(stage, buf, name) {
    const wb = window.XLSX.read(buf, { type: "array", cellDates: true, sheetRows: MAX_ROWS + 1 });
    const names = wb.SheetNames || [];
    if (!names.length) throw new Error("This workbook has no sheets.");
    const tabs = document.createElement("div");
    tabs.className = "ofv-tabs";
    const f = frame(name);
    const show = (i) => {
      for (const b of tabs.children) b.classList.toggle("on", Number(b.dataset.i) === i);
      f.srcdoc = doc(sheetHtml(wb.Sheets[names[i]]), "sheet");
    };
    names.forEach((n, i) => {
      const b = document.createElement("button");
      b.type = "button"; b.dataset.i = String(i); b.textContent = n; b.title = n;
      b.onclick = () => show(i);
      tabs.appendChild(b);
    });
    if (names.length > 1) stage.appendChild(tabs);
    stage.appendChild(f);
    show(0);
  }

  async function renderPptx(stage, buf, name) {
    const zip = await window.JSZip.loadAsync(buf);
    const slides = Object.keys(zip.files).map((p) => /^ppt\/slides\/slide(\d+)\.xml$/.exec(p)).filter(Boolean)
      .map((m) => ({ n: Number(m[1]), path: m[0] })).sort((a, b) => a.n - b.n);
    if (!slides.length) throw new Error("No slides found in this file.");
    const parts = [];
    for (const s of slides) {
      const xml = new DOMParser().parseFromString(await zip.file(s.path).async("string"), "application/xml");
      const paras = [];
      for (const p of xml.getElementsByTagNameNS("*", "p")) {
        const t = [...p.getElementsByTagNameNS("*", "t")].map((x) => x.textContent).join("");
        if (t.trim()) paras.push(t);
      }
      parts.push("<div class=\"slide\"><h2>Slide " + s.n + "</h2>" +
        (paras.length ? paras.map((t, i) => "<p" + (i === 0 ? " class=\"t\"" : "") + ">" + esc(t) + "</p>").join("") : "<p class=\"empty\">(no text on this slide)</p>") + "</div>");
    }
    const f = frame(name);
    f.srcdoc = doc("<div class=\"note\">Text of the slides only. Pictures, shapes and layout are not shown; download the file for the full deck.</div>" + parts.join(""));
    stage.appendChild(f);
  }

  function open(stage, url, o) {
    o = o || {};
    const kind = ext(o.name), ctl = new AbortController();
    let dead = false;
    const boot = document.createElement("div");
    boot.className = "pdfv-boot";
    const spin = document.createElement("span"); spin.className = "pdfv-spin";
    const label = document.createElement("span"); label.textContent = "Opening document…";
    boot.append(spin, label);
    const holder = document.createElement("div");
    holder.className = "ofv";
    stage.appendChild(boot);
    const group = kind === "docx" ? "docx" : kind === "pptx" ? "pptx" : "xlsx";
    (async () => {
      try {
        const [buf] = await Promise.all([fetchBuffer(url, ctl.signal), Promise.all(LIBS[group].map((s) => loadScript(s, o.version)))]);
        if (dead) return;
        boot.remove();
        stage.appendChild(holder);
        if (group === "docx") await renderDocx(holder, buf, o.name);
        else if (group === "pptx") await renderPptx(holder, buf, o.name);
        else await renderXlsx(holder, buf, o.name);
      } catch (err) {
        if (dead || (err && err.name === "AbortError")) return;
        if (o.onFallback) o.onFallback(String((err && err.message) || err).replace(/^Error: /, "") ||
          "This file can't be previewed. It may be damaged or password protected.");
      }
    })();
    return { destroy() { dead = true; ctl.abort(); } };
  }
  window.RCOffice = { open };
})();
