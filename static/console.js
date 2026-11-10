"use strict";

const USER = window.RCONSOLE_USER || "user";
let tabs = [];
let activeId = null;
// Which tab id is currently materialized in the shared #output DOM. Used by
// renderOutput to detect a tab switch (the DOM is shared across all tabs) and
// force a full rebuild — without this, switching back to a tab whose DOM was
// clobbered by another tab shows the wrong (or empty) content.
let renderedTabId = null;

// ---------------------------------------------------------------------------
// Update lock. While `rconsole update` runs the server answers every request with 503 (header
// X-Rconsole-Updating) so nothing else can be done. Any such answer — or the command's own reply —
// puts this full-screen progress view over the console; it follows /api/update/status, waits through
// the restart, then reloads the page by itself.
// ---------------------------------------------------------------------------
const rcUpdate = { el: null, timer: null, seenActive: false, seenDown: false, since: 0 };
const rcRawFetch = typeof window.fetch === "function" ? window.fetch.bind(window) : null;
if (rcRawFetch) {
  window.fetch = async function (...args) {
    const r = await rcRawFetch(...args);
    try {
      if (r && r.status === 503 && r.headers && r.headers.get("X-Rconsole-Updating")) showUpdateOverlay();
    } catch (e) { /* never break a request over the overlay */ }
    return r;
  };
}
function showUpdateOverlay() {
  if (rcUpdate.el || !rcRawFetch) return;
  rcUpdate.since = Date.now() / 1000;
  const el = document.createElement("div");
  el.id = "updateOverlay";
  el.className = "update-overlay";
  el.innerHTML = '<div class="update-box"><div class="update-title">rconsole is updating</div>' +
    '<div class="update-sub">Nothing else can be done until it is finished. This page reloads by itself.</div>' +
    '<div class="update-bar"><div class="update-fill"></div></div>' +
    '<div class="update-msg">starting…</div><pre class="update-log"></pre></div>';
  document.body.appendChild(el);
  rcUpdate.el = el;
  rcUpdate.timer = setInterval(pollUpdate, 1500);
  pollUpdate();
}
function hideUpdateOverlay() {
  clearInterval(rcUpdate.timer);
  if (rcUpdate.el) rcUpdate.el.remove();
  rcUpdate.el = null;
  rcUpdate.seenActive = rcUpdate.seenDown = false;
}
async function pollUpdate() {
  const el = rcUpdate.el;
  if (!el) return;
  const fill = el.querySelector(".update-fill");
  const msg = el.querySelector(".update-msg");
  let d = null;
  try {
    const r = await rcRawFetch("/api/update/status", { cache: "no-store", credentials: "same-origin" });
    d = r.ok ? await r.json() : null;
  } catch (e) { d = null; }
  if (!d) {                                    // the server is restarting
    rcUpdate.seenDown = true;
    msg.textContent = "restarting rconsole…";
    return;
  }
  if (d.updating) {
    rcUpdate.seenActive = true;
    fill.style.width = Math.max(3, d.pct || 0) + "%";
    const what = d.mode === "rollback" ? "rolling back to " : "updating to ";
    msg.textContent = what + (d.to || "") + " — " + (d.stage || "working") + "…";
    if (d.log) el.querySelector(".update-log").textContent = d.log.slice(-8).join("\n");
    return;
  }
  const last = d.last || {};
  const fresh = last.at && last.at >= rcUpdate.since - 10;       // a result from this very update
  if (!rcUpdate.seenActive && !rcUpdate.seenDown && !fresh) { hideUpdateOverlay(); return; }   // stale 503
  clearInterval(rcUpdate.timer);
  fill.style.width = "100%";
  const ok = last.ok !== false;
  el.classList.toggle("update-failed", !ok);
  if (ok) {
    msg.textContent = "Done: " + (last.message || ("now running " + d.version)) + " — reloading…";
    setTimeout(() => window.location.reload(), 1800);
    return;
  }
  msg.textContent = "Update failed: " + (last.message || "see the log") + " — the previous version keeps running and your data was not touched.";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn";
  btn.textContent = "Back to the console";
  btn.onclick = () => window.location.reload();
  el.querySelector(".update-box").appendChild(btn);
}

// ---------------------------------------------------------------------------
// xterm.js is heavy and only needed for live terminal tabs (nano, vim, REPLs).
// Load it on demand (once) so line-mode users don't pay the download/parse or
// memory cost on first paint. Consoles then cache it for subsequent visits.
// ---------------------------------------------------------------------------
let xtermLoad = null;
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error("failed to load " + src));
    document.head.appendChild(s);
  });
}
function ensureXterm() {
  if (window.Terminal && window.FitAddon) return Promise.resolve();
  if (!xtermLoad) {
    xtermLoad = Promise.all([
      loadScript("/static/xterm.js"),
      loadScript("/static/addon-fit.js"),
    ]);
  }
  return xtermLoad;
}

// ---------------------------------------------------------------------------
// ANSI escape code -> HTML so colored host output and the fun commands
// (cowsay/matrix/lolcat/neofetch/...) render in full color in the line console.
// ---------------------------------------------------------------------------
const ANSI_COLORS = [
  "#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
  "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff",
];
function ansi256(n) {
  if (n < 16) return ANSI_COLORS[n];
  if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
  const c = n - 16, r = Math.floor(c / 36) % 6, g = Math.floor(c / 6) % 6, b = c % 6;
  const v = (x) => (x === 0 ? 0 : x === 5 ? 255 : 55 + x * 40);
  return `rgb(${v(r)},${v(g)},${v(b)})`;
}
function ansiToHtml(s) {
  const re = /\x1b\[([0-9;]*)m/g;
  let out = "", last = 0, m;
  const st = { fg: null, bg: null, bold: false, dim: false, italic: false,
               under: false, strike: false, inv: false };
  function span(text) {
    const e = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    if (!text) return "";
    let fgc = st.fg, bgc = st.bg;
    if (st.inv) { const t = fgc; fgc = bgc || "#000000"; bgc = t || "#cccccc"; }
    let css = "";
    if (fgc) css += "color:" + fgc + ";";
    if (bgc) css += "background:" + bgc + ";";
    if (st.bold) css += "font-weight:bold;";
    if (st.dim) css += "opacity:.7;";
    if (st.italic) css += "font-style:italic;";
    if (st.under || st.strike)
      css += "text-decoration:" + (st.under ? "underline" : "") +
             (st.strike ? " line-through" : "") + ";";
    return css ? `<span style="${css}">${e}</span>` : e;
  }
  function apply(codes) {
    const parts = (codes === "" ? "0" : codes).split(";")
      .map((x) => (x === "" ? 0 : parseInt(x, 10)));
    let i = 0;
    while (i < parts.length) {
      const c = parts[i];
      if (c === 0) Object.assign(st, { fg: null, bg: null, bold: false, dim: false,
        italic: false, under: false, strike: false, inv: false });
      else if (c === 1) st.bold = true;
      else if (c === 2) st.dim = true;
      else if (c === 3) st.italic = true;
      else if (c === 4) st.under = true;
      else if (c === 7) st.inv = true;
      else if (c === 9) st.strike = true;
      else if (c === 22) { st.bold = false; st.dim = false; }
      else if (c === 23) st.italic = false;
      else if (c === 24) st.under = false;
      else if (c === 27) st.inv = false;
      else if (c === 29) st.strike = false;
      else if (c >= 30 && c <= 37) st.fg = ANSI_COLORS[c - 30];
      else if (c === 38) {
        if (parts[i + 1] === 2) { st.fg = `rgb(${parts[i + 2]},${parts[i + 3]},${parts[i + 4]})`; i += 4; }
        else if (parts[i + 1] === 5) { st.fg = ansi256(parts[i + 2]); i += 2; }
      }
      else if (c === 39) st.fg = null;
      else if (c >= 90 && c <= 97) st.fg = ANSI_COLORS[c - 90 + 8];
      else if (c >= 40 && c <= 47) st.bg = ANSI_COLORS[c - 40];
      else if (c === 48) {
        if (parts[i + 1] === 2) { st.bg = `rgb(${parts[i + 2]},${parts[i + 3]},${parts[i + 4]})`; i += 4; }
        else if (parts[i + 1] === 5) { st.bg = ansi256(parts[i + 2]); i += 2; }
      }
      else if (c === 49) st.bg = null;
      else if (c >= 100 && c <= 107) st.bg = ANSI_COLORS[c - 100 + 8];
      i++;
    }
  }
  while ((m = re.exec(s))) {
    if (m.index > last) out += span(s.slice(last, m.index));
    apply(m[1]);
    last = re.lastIndex;
  }
  if (last < s.length) out += span(s.slice(last));
  return out;
}

// ---------------------------------------------------------------------------
// Themes: applied via <html data-theme="...">; xterm gets a matching palette.
// ---------------------------------------------------------------------------
const THEME_XTERM = {
  default:  { background: "#000000", foreground: "#d6e2ef", cursor: "#d6e2ef" },
  light:    { background: "#ffffff", foreground: "#1a1a1a", cursor: "#1a1a1a" },
  amber:    { background: "#1a1000", foreground: "#ffb000", cursor: "#ffb000" },
  solarized:{ background: "#002b36", foreground: "#839496", cursor: "#839496" },
  dracula:  { background: "#282a36", foreground: "#f8f8f2", cursor: "#f8f8f2" },
  github:   { background: "#0d1117", foreground: "#c9d1d9", cursor: "#c9d1d9" },
  ubuntu:   { background: "#300a24", foreground: "#eeeeec", cursor: "#eeeeec" },
  retro:    { background: "#000000", foreground: "#33ff33", cursor: "#33ff33" },
  nord:     { background: "#2e3440", foreground: "#d8dee9", cursor: "#d8dee9" },
  gruvbox:  { background: "#282828", foreground: "#ebdbb2", cursor: "#ebdbb2" },
  matrix:   { background: "#000800", foreground: "#33ff33", cursor: "#33ff33" },
  tokyonight:{ background: "#1a1b26", foreground: "#c0caf5", cursor: "#c0caf5" },
  catppuccin:{ background: "#1e1e2e", foreground: "#cdd6f4", cursor: "#cdd6f4" },
  monokai:  { background: "#272822", foreground: "#f8f8f2", cursor: "#f8f8f2" },
  everforest:{ background: "#2d353b", foreground: "#d3c6aa", cursor: "#d3c6aa" },
  ayu:      { background: "#0a0e14", foreground: "#b3b1ad", cursor: "#b3b1ad" },
  papercolor:{ background: "#1c1c1c", foreground: "#eeeeee", cursor: "#eeeeee" },
};
let currentTheme = window.RCONSOLE_THEME || "github";
function applyTheme(name) {
  currentTheme = name || "github";
  document.documentElement.dataset.theme = currentTheme;
  const tb = document.getElementById("themebtn");
  if (tb) tb.textContent = currentTheme;
  const t = activeTab();
  if (t && t.term) {
    try { t.term.options.theme = THEME_XTERM[currentTheme] || THEME_XTERM.default; } catch (e) { /* ignore */ }
  }
}

// Theme order for the toggle button (keep in sync with commands.THEMES).
const THEME_LIST = ["default", "light", "amber", "solarized", "dracula", "github",
  "ubuntu", "retro", "nord", "gruvbox", "matrix", "tokyonight", "catppuccin",
  "monokai", "everforest", "ayu", "papercolor"];

function cycleTheme() {
  const i = THEME_LIST.indexOf(currentTheme);
  const next = THEME_LIST[(i + 1) % THEME_LIST.length];
  applyTheme(next);
  // Persist the choice to the server (keyed to the account).
  fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd: "theme " + next, tab: activeId || "main" }),
  }).catch(() => {});
}

const elTabs = document.getElementById("tabs");
const elNew = document.getElementById("newtab");
const elOutput = document.getElementById("output");
const elPrompt = document.getElementById("prompt");
const elCmd = document.getElementById("cmd");

let jobTail = null;
function onTailKey(e) {
  if (jobTail && jobTail.active) {
    e.preventDefault();
    stopJobTail();
  }
}
function stopJobTail() {
  if (!jobTail) return;
  const tab = jobTail.tab;
  jobTail.active = false;
  if (jobTail.timer) clearInterval(jobTail.timer);
  document.removeEventListener("keydown", onTailKey, true);
  jobTail = null;
  if (!tab || isActiveTab(tab)) {
    elCmd.disabled = false;
    elCmd.focus();
  }
  if (!tab || isActiveTab(tab)) renderOutput();
}

function startJobTail(tab, jid, isFg) {
  stopJobTail();
  jobTail = { jid, off: 0, active: true, timer: null, fg: !!isFg, tab };
  appendLines(
    tab,
    "[tailing job " + jid + (isFg ? " (foreground)" : "") + " — press any key to stop]",
    "term-dim"
  );
  if (isActiveTab(tab)) renderOutput();
  if (isActiveTab(tab)) elCmd.disabled = true;
  document.addEventListener("keydown", onTailKey, true);
  const tick = () => {
    if (!jobTail || !jobTail.active) return;
    fetch("/api/jobtail?job=" + encodeURIComponent(jid) + "&off=" + encodeURIComponent(jobTail.off))
      .then((r) => r.json())
      .then((j) => {
        if (!jobTail || !jobTail.active) return;
        if (j.text) {
          String(j.text).split("\n").forEach((l) => {
            const line = l === "" ? " " : l;
            tab.lines.push({ text: line, html: ansiToHtml(line), cls: "" });
          });
          if (isActiveTab(tab)) renderOutput();
        }
        if (typeof j.off === "number") jobTail.off = j.off;
        if (!j.alive) {
          appendLines(tab, "[job " + jid + " finished]", "term-dim");
          if (isActiveTab(tab)) renderOutput();
          stopJobTail();
        }
      })
      .catch(() => {});
  };
  jobTail.timer = setInterval(tick, 800);
  tick();
}
// Name shown in the prompt: "rconsole" on the host, otherwise the VM you are in
// (e.g. ark@debian:/root$). Kept up to date from every server response.
let TARGET_LABEL = "rconsole";
let lastSentCmd = "";
function promptText(cwd) {
  return `${USER}@${TARGET_LABEL}:${cwd}$ `;
}

function usedTabNumbers() {
  const nums = new Set();
  tabs.forEach((t) => {
    const m = /^t(\d+)$/.exec(t.id);
    if (m) nums.add(parseInt(m[1], 10));
  });
  return nums;
}

// Reuse freed numbers: the lowest positive integer not already taken.
function nextTabNumber() {
  const nums = usedTabNumbers();
  let n = 1;
  while (nums.has(n)) n += 1;
  return n;
}

function makeTab(opts) {
  opts = opts || {};
  const id = opts.id || ("t" + nextTabNumber());
  const m = /^t(\d+)$/.exec(id);
  const num = m ? parseInt(m[1], 10) : 1;
  const tab = {
    id,
    name: opts.name || ("tab " + num),
    lines: [],
    cwd: opts.cwd || "/home/user",
    hist: [],
    histIdx: 0,
    interactive: false,
    interactivePrompt: "",
    draft: "",
    ptyActive: false,
    ptyWs: null,
    term: null,
    fitAddon: null,
    termRO: null,
    termHost: null,
    isTerminal: !!opts.isTerminal,
    cmd: opts.cmd || "",
    _exited: false,
    _killed: false,
    _reconnectTries: 0,
  };
  if (opts.first) {
    tab.lines.push({ text: "Rconsole — Made by MD ABU SALEHIN", cls: "term-green" });
    tab.lines.push({ text: "type 'help' to get started. Good Day.", cls: "term-dim" });
    tab.lines.push({ text: "" });
  }
  tabs.push(tab);
  renderTabs();
  return tab;
}

function newTab(focus = true) {
  const tab = makeTab({ first: tabs.length === 0 });
  if (focus) selectTab(tab.id);
  return tab;
}

function activeTab() {
  return tabs.find((t) => t.id === activeId);
}

function renderTabs() {
  elTabs.innerHTML = "";
  tabs.forEach((t) => {
    const d = document.createElement("div");
    d.className = "tab" + (t.id === activeId ? " active" : "");
    const label = document.createElement("span");
    label.textContent = t.name;
    d.appendChild(label);
    if (tabs.length > 1) {
      const close = document.createElement("span");
      close.className = "close";
      close.textContent = "×";
      close.onclick = (e) => {
        e.stopPropagation();
        closeTab(t.id);
      };
      d.appendChild(close);
    }
    d.onclick = () => selectTab(t.id);
    elTabs.appendChild(d);
  });
}

function closeTab(id) {
  stopJobTail();
  const idx = tabs.findIndex((t) => t.id === id);
  if (idx === -1) return;
  const t = tabs[idx];
  t._killed = true;
  // A terminal tab's processes must be stopped when the user closes it. Tell
  // the server to kill the session (it pops the server-side tab too); a silent
  // disconnect would instead preserve the process so the user can return.
  if (t.isTerminal && t.ptyWs && t.ptyWs.readyState === 1) {
    try { t.ptyWs.send(JSON.stringify({ t: "kill" })); } catch (e) { /* ignore */ }
  }
  closePty(id);
  fetch("/api/close_tab", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab: id }),
  }).catch(() => {});
  tabs.splice(idx, 1);
  if (activeId === id) {
    activeId = tabs.length ? tabs[Math.max(0, idx - 1)].id : null;
  }
  if (!tabs.length) {
    newTab();
    return;
  }
  renderTabs();
  selectTab(activeId);
}

function openAgentPicker(p) {
  const modal = document.getElementById("agentModal");
  const sel = document.getElementById("agentModalSelect");
  const title = document.getElementById("agentModalTitle");
  if (title) title.textContent = p.action === "model" ? "Choose a model" : "Choose";
  sel.innerHTML = "";
  (p.models || []).forEach((m) => {
    const o = document.createElement("option");
    o.value = m;
    o.textContent = m;
    if (m === p.current) o.selected = true;
    sel.appendChild(o);
  });
  modal.classList.remove("hidden");
  elCmd.disabled = true;
  const selectBtn = document.getElementById("agentModalSelectBtn");
  const cancelBtn = document.getElementById("agentModalCancelBtn");
  selectBtn.onclick = () => {
    const v = sel.value;
    modal.classList.add("hidden");
    send("/model " + v);
  };
  cancelBtn.onclick = () => {
    modal.classList.add("hidden");
    // Resume the agent session to a normal prompt without changing the model.
    send("");
  };
}

function selectTab(id) {
  stopJobTail();
  const prev = activeTab();
  if (prev && elCmd) prev.draft = elCmd.value;
  activeId = id;
  renderTabs();
  renderOutput();
  const t = activeTab();
  // Re-attach a persistent terminal that survived a refresh / tab switch.
  if (t && t.isTerminal && !t.ptyActive && t.cmd) {
    openPty(t.cmd, t.id);
    return;
  }
  if (elCmd && t) elCmd.value = t.draft || "";
  updateView();
  if (!isPtyActive() && elCmd && !pickerHasFocus()) elCmd.focus();
}

// True while a dropdown/button in the output (e.g. the system picker) is in use.
function pickerHasFocus() {
  const a = document.activeElement;
  return !!(a && a.closest && a.closest(".choice-box"));
}

function appendLineEl(ln) {
  const div = document.createElement("div");
  if (ln.el) {
    div.className = "line codebox-line";
    div.appendChild(ln.el);
    elOutput.appendChild(div);
    return;
  }
  div.className = "line" + (ln.cls ? " " + ln.cls : "");
  if (ln.html !== undefined) div.innerHTML = ln.html;
  else div.textContent = ln.text;
  elOutput.appendChild(div);
}

// Render agent output with support for fenced code blocks (```lang ... ```):
// normal text is appended line-by-line (ANSI aware); a fenced block becomes a
// styled, syntax-highlighted "code box" element, so file edits/read shown by
// the agent look like a little editor panel rather than raw text.
function appendCodeBox(tab, lang, code) {
  if (!tab) return;
  // Shell/command output can carry ANSI color codes; strip them so they don't
  // render as literal escape sequences inside the box.
  code = stripAnsi(code);
  const box = document.createElement("div");
  box.className = "agent-code";
  const head = document.createElement("div");
  head.className = "agent-code-head";
  const title = document.createElement("span");
  title.className = "agent-code-title";
  title.textContent = lang === "log" ? "bash" : (lang ? lang : "code");
  head.appendChild(title);
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "agent-code-copy";
  copy.textContent = "copy";
  copy.onclick = () => {
    try {
      navigator.clipboard.writeText(code);
      copy.textContent = "copied";
      setTimeout(() => (copy.textContent = "copy"), 1200);
    } catch (e) { /* clipboard blocked */ }
  };
  head.appendChild(copy);
  const pre = document.createElement("pre");
  pre.className = "agent-code-pre";
  const hl = getHighlighter(lang || "text");
  if (hl && code.length <= 300000) pre.innerHTML = hl(code) + "\n";
  else pre.textContent = code;
  box.appendChild(head);
  box.appendChild(pre);
  tab.lines.push({ el: box });
}

function appendRich(tab, text, cls) {
  if (!tab) return;
  if (text === undefined || text === null) return;
  const lines = String(text).split("\n");
  let buf = [];
  let inFence = false;
  let fenceLang = "";
  let fenceBuf = [];
  const flush = () => {
    if (buf.length) {
      appendLines(tab, buf.join("\n"), cls || "");
      buf = [];
    }
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const open = /^```([a-zA-Z0-9_+-]*)\s*$/.exec(line);
    if (!inFence) {
      if (open) {
        flush();
        inFence = true;
        fenceLang = (open[1] || "").toLowerCase();
        fenceBuf = [];
      } else {
        buf.push(line);
      }
    } else {
      if (/^```\s*$/.test(line)) {
        appendCodeBox(tab, fenceLang, fenceBuf.join("\n"));
        inFence = false;
        fenceBuf = [];
      } else {
        fenceBuf.push(line);
      }
    }
  }
   if (inFence) {
    // Unterminated fence: emit as plain text rather than swallow content.
    buf.push("```" + fenceLang);
    buf = buf.concat(fenceBuf);
  }
  flush();
}

function isActiveTab(tab) {
  return !!tab && activeId === tab.id;
}

function renderOutput() {
  const t = activeTab();
  // Full rebuild when (a) the active tab differs from what's in the DOM, or
  // (b) this tab's line array was replaced (e.g. a `clear`). Otherwise just
  // append the lines added since the last render — keeps long sessions smooth.
  const needFull =
    renderedTabId !== (t && t.id) ||
    (t && t._renderedLines !== t.lines);
  if (needFull) {
    elOutput.innerHTML = "";
    if (t) t.lines.forEach(appendLineEl);
    renderedTabId = t ? t.id : null;
  } else if (t) {
    for (let i = t._renderedCount || 0; i < t.lines.length; i++) {
      appendLineEl(t.lines[i]);
    }
  }
  if (t) {
    t._renderedLines = t.lines;
    t._renderedCount = t.lines.length;
  }
  // prompt
  if (t && t.interactive) {
    elPrompt.textContent = t.interactivePrompt;
    elPrompt.className = "interactive";
  } else {
    elPrompt.textContent = t ? promptText(t.cwd) : "";
    elPrompt.className = "";
  }
  const sc = elOutput.parentElement;
  sc.scrollTop = sc.scrollHeight;
  // Return focus to the input only when the user is already there, so we don't
  // yank focus (and re-open the soft keyboard) on every render on mobile.
  if (!isPtyActive() &&
      (document.activeElement === elCmd || document.activeElement === document.body)) {
    elCmd.focus({ preventScroll: true });
  }
}

function appendLines(tab, text, cls) {
  if (!tab) return;
  if (text === undefined || text === null) return;
  String(text).split("\n").forEach((l) => {
    const line = l === "" ? " " : l;
    tab.lines.push({ text: line, html: ansiToHtml(line), cls: cls || "" });
  });
  // Bound the scrollback so very long sessions don't grow the DOM/memory
  // without limit (which would eventually make every render janky). Trim
  // occasionally and force a single rebuild rather than per-line.
  const CAP = 4000;
  if (tab.lines.length > CAP) {
    tab.lines.splice(0, tab.lines.length - CAP);
    tab._renderedLines = null;
  }
}

// How many monospace characters fit across the output area. Sent with every
// command so the server can fit things like `neofetch` to this screen — a phone
// holds ~40 columns, a desktop 100+.
let _chW = 0;
function outputCols() {
  try {
    if (!_chW) {
      const probe = document.createElement("span");
      probe.style.cssText = "position:absolute;visibility:hidden;white-space:pre;font:inherit";
      probe.textContent = "M".repeat(50);
      elOutput.appendChild(probe);
      _chW = probe.getBoundingClientRect().width / 50;
      probe.remove();
    }
    const cs = getComputedStyle(elOutput);
    const inner = elOutput.clientWidth - parseFloat(cs.paddingLeft || 0) - parseFloat(cs.paddingRight || 0);
    return _chW > 0 ? Math.max(20, Math.floor(inner / _chW)) : 0;
  } catch (e) {
    return 0;
  }
}
window.addEventListener("resize", () => { _chW = 0; });

async function send(cmd, tabId) {
  const t = (tabId && tabs.find((x) => x.id === tabId)) || activeTab();
  if (!t) return;
  lastSentCmd = cmd;
  const body = JSON.stringify({ cmd, tab: t.id, cols: outputCols() });
  let resp;
  try {
    resp = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  } catch (e) {
    return;
  }
  if (resp.status === 401) {
    window.location = "/login";
    return;
  }
  const data = await resp.json();
  handleResponse(t.id, data);
}

// Apply a server response to the specific tab that issued the command. Routing
// by tab id (not the currently-active tab) keeps output in the right tab even
// if the user switches tabs while a request is in flight.
function handleResponse(tabId, data) {
  const t = tabs.find((x) => x.id === tabId);
  if (!t) return;
  if (data.redirect) {
    window.location = data.redirect;
    return;
  }
  if (data.updating) showUpdateOverlay();

  // Intermediate "tick" from a streaming generator (e.g. the AI agent's
  // "thinking…" / tool activity). Render it and keep advancing the generator
  // automatically — do NOT show an input box, so the turn feels live instead of
  // going silent until the final answer.
  if (data._tick) {
    if (data.output) appendRich(t, data.output, "");
    if (isActiveTab(t)) renderOutput();
    if (isActiveTab(t)) elCmd.disabled = true;
    send("", tabId);
    return;
  }
  if (isActiveTab(t)) elCmd.disabled = false;

  // The agent asked the client to show a picker (e.g. /model with no argument).
  if (data.agent_picker) {
    if (data.output) appendRich(t, data.output, "");
    if (isActiveTab(t)) {
      renderOutput();
      openAgentPicker(data.agent_picker);
    }
    return;
  }

  if (data.editor) {
    openCodeEditor(data.editor);
    return;
  }

  if (data.jobtail !== undefined && data.jobtail !== null) {
    startJobTail(t, data.jobtail, !!data.fg);
    return;
  }

  if (data.theme) applyTheme(data.theme);

  if (data.clear) t.lines = [];

  if (data.pty) {
    t.isTerminal = true;
    t.cmd = data.cmd;
    openPty(data.cmd, t.id);
    return;
  }

  if (data.target) TARGET_LABEL = data.target;
  if (data.output !== undefined && data.output !== "") {
    appendRich(t, data.output, "");
  }
  // The command needs to know which system to use: show a dropdown right here.
  if (data.choice && data.choice.options && data.choice.options.length) addChoice(t, data.choice);
  if (/^\s*(sudo\s+)?vm(\s|$)/.test(lastSentCmd)) refreshTargets();
  if (data.prompt !== undefined) {
    t.interactive = true;
    t.interactivePrompt = data.prompt;
  } else {
    t.interactive = false;
    t.interactivePrompt = "";
  }
  if (data.cwd) t.cwd = data.cwd;
  if (isActiveTab(t)) renderOutput();
}

// ---------------------------------------------------------------------------
// Interactive PTY terminal (xterm.js + websocket). Each tab owns its own
// terminal instance + websocket, so a session (nano, vim, REPL, ...) keeps
// running in the background and reappears when you return to that tab.
// ---------------------------------------------------------------------------
function isPtyActive() {
  const t = activeTab();
  return !!(t && t.ptyActive);
}

function sendPtyTo(t, d) {
  if (t && t.ptyWs && t.ptyWs.readyState === 1) {
    t.ptyWs.send(JSON.stringify({ t: "data", d }));
  }
}

async function openPty(cmd, tabId) {
  const t = tabs.find((x) => x.id === tabId);
  if (!t || t.ptyActive) return;
  // Tear down any prior terminal instance (e.g. when re-attaching after a
  // transient websocket drop) so we don't leak hosts / observers / sockets.
  if (t.term) { try { t.term.dispose(); } catch (e) { /* ignore */ } t.term = null; t.fitAddon = null; }
  if (t.termRO) { try { t.termRO.disconnect(); } catch (e) { /* ignore */ } t.termRO = null; }
  if (t.termHost && t.termHost.parentNode) t.termHost.parentNode.removeChild(t.termHost);
  t.termHost = null;
  t.ptyActive = true;
  t.isTerminal = true;
  t.cmd = cmd;
  t.ptyText = "";
  t._exited = false;
  t._killed = false;

  const host = document.createElement("div");
  host.className = "term-host";
  host.id = "term-" + tabId;
  document.getElementById("terms").appendChild(host);
  t.termHost = host;

  // Defer terminal setup until xterm.js (lazy-loaded) is available.
  try {
    await ensureXterm();
  } catch (e) {
    t.ptyActive = false;
    activeTab().lines.push({ text: "Failed to load terminal components.", cls: "term-red" });
    renderOutput();
    elCmd.focus();
    return;
  }

  const Terminal = window.Terminal;
  const FitAddon = window.FitAddon;
  t.term = new Terminal({
    cursorBlink: true,
    // Canvas renderer is dramatically faster than the DOM renderer for
    // full-screen TUIs like nano/vim — it's what keeps typing responsive.
    rendererType: "canvas",
    fontSize: 14,
    fontFamily: "Consolas, 'Courier New', monospace",
    theme: THEME_XTERM[currentTheme] || THEME_XTERM.default,
  });
  t.fitAddon = new FitAddon.FitAddon();
  t.term.loadAddon(t.fitAddon);
  t.term.open(host);

  // A visible way out (Esc is not on a phone keyboard): closes this window and
  // ends the session, exactly like pressing Esc.
  const xbtn = document.createElement("button");
  xbtn.type = "button";
  xbtn.className = "term-close";
  xbtn.textContent = "✕";
  xbtn.title = "Close this window (Esc)";
  xbtn.setAttribute("aria-label", "Close window");
  xbtn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    userExitPty(tabId);
  });
  host.appendChild(xbtn);

  // xterm captures keystrokes through its own hidden textarea. On mobile that
  // textarea inherits the browser's autocorrect/autocapitalize and (on iOS)
  // triggers zoom-on-focus — both add latency and mangle editor input. Harden
  // it the same way we did for the line-mode input.
  const ta = t.term.textarea;
  if (ta) {
    ta.setAttribute("autocomplete", "off");
    ta.setAttribute("autocorrect", "off");
    ta.setAttribute("autocapitalize", "off");
    ta.setAttribute("spellcheck", "false");
    ta.style.fontSize = "16px";
  }

  // Fit whenever the terminal box changes size (initial show, window resize,
  // or the mobile soft-keyboard opening/closing) so it always fills the width.
  let fitScheduled = false;
  let lastFitW = 0, lastFitH = 0, lastFitTime = 0;
  const doFit = () => {
    if (fitScheduled) return;
    fitScheduled = true;
    requestAnimationFrame(() => {
      fitScheduled = false;
      const w = host.clientWidth, h = host.clientHeight;
      // Skip when the host is hidden (display:none) so we never tell the backend
      // to resize a terminal to 0x0, which can break running programs.
      if (w === 0 || h === 0) return;
      // Break any resize feedback loop: fit() can change the rendered host size,
      // which re-fires ResizeObserver, which calls fit() again — pegging the CPU
      // and flooding the backend with resize messages (and freezing the terminal
      // on mobile). Only refit when the box actually changed and not too often.
      const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
      if ((w === lastFitW && h === lastFitH) || now - lastFitTime < 120) return;
      lastFitW = w; lastFitH = h; lastFitTime = now;
      try { t.fitAddon.fit(); } catch (e) { /* ignore */ }
      if (t.ptyWs && t.ptyWs.readyState === 1) {
        t.ptyWs.send(JSON.stringify({ t: "resize", cols: t.term.cols, rows: t.term.rows }));
      }
    });
  };
  const ro = new ResizeObserver(doFit);
  ro.observe(host);
  t.termRO = ro;
  requestAnimationFrame(doFit);
  setTimeout(doFit, 60);
  setTimeout(doFit, 250);

  // On touch devices the terminal must hold focus to receive keystrokes.
  host.addEventListener("pointerdown", () => t.term.focus());
  t.term.focus();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url =
    `${proto}://${location.host}/ws?tab=${encodeURIComponent(tabId)}` +
    `&cmd=${encodeURIComponent(cmd)}`;
  const ws = new WebSocket(url);
  t.ptyWs = ws;

  ws.onopen = () => {
    t._reconnectTries = 0;
    ws.send(JSON.stringify({ t: "resize", cols: t.term.cols, rows: t.term.rows }));
  };
  ws.onmessage = (e) => {
    // Control frames are prefixed with SOH (\x01) so raw terminal output can
    // never be misinterpreted as one (e.g. a program printing '{"t":"exit"}').
    if (typeof e.data === "string" && e.data.charCodeAt(0) === 1) {
      try {
        const msg = JSON.parse(e.data.slice(1));
        if (msg && msg.t === "exit") {
          onPtyExit(tabId, msg.cwd);
        }
      } catch (err) { /* ignore malformed control frame */ }
      return;
    }
    if (t.ptyText !== undefined) t.ptyText += e.data;
    t.term.write(e.data);
  };
  ws.onclose = () => {
    // The process really ended (server told us) or the user killed it: drop to
    // the line console normally.
    if (t._exited || t._killed) { closePty(tabId); return; }
    // Transient disconnect (network blip, mobile backgrounding, keyboard
    // animation). Re-attach the still-living server session instead of stranding
    // the user in the line console mid-edit.
    t.ptyActive = false;
    t.ptyWs = null;
    t._reconnectTries = (t._reconnectTries || 0) + 1;
    if (t._reconnectTries <= 5) {
      setTimeout(() => {
        const cur = tabs.find((x) => x.id === tabId);
        if (cur && !cur._killed && activeTab() === cur) openPty(cur.cmd, tabId);
      }, 700 * t._reconnectTries);
    } else {
      closePty(tabId);
    }
  };
  ws.onerror = ws.onclose;

  t.term.onData((d) => {
    sendPtyTo(t, d);
  });

  // Esc while a PTY is live detaches the session and returns to the line-mode
  // console. Captured on the host in the capture phase so it fires before
  // xterm's own textarea listener and the ESC byte never reaches the child.
  host.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      userExitPty(tabId);
    }
  }, true);

  updateView();
}

// The user pressed Ctrl+C (or the keybar ^C) inside a live PTY: terminate the
// session and drop back to the line console so they aren't stranded in the
// dead/running terminal. Output is not captured — this is an explicit detach.
function userExitPty(tabId) {
  const t = tabs.find((x) => x.id === tabId);
  if (!t || !t.ptyActive) return;
  t._killed = true;
  if (t.ptyWs && t.ptyWs.readyState === 1) {
    try { t.ptyWs.send(JSON.stringify({ t: "kill" })); } catch (e) { /* ignore */ }
  }
  t.isTerminal = false;
  t.cmd = "";
  t.ptyText = "";
  closePty(tabId);
  renderOutput();
  updateView();
  elCmd.focus();
}

let resizeRaf = null;
function onPtyResize() {
  if (resizeRaf) return;
  resizeRaf = requestAnimationFrame(() => {
    resizeRaf = null;
    const t = activeTab();
    if (!t || !t.ptyActive || !t.term || !t.fitAddon) return;
    // Never push a degenerate size to the backend (can kill the child).
    if (t.term.cols <= 0 || t.term.rows <= 0) return;
    try {
      t.fitAddon.fit();
      if (t.ptyWs && t.ptyWs.readyState === 1) {
        t.ptyWs.send(
          JSON.stringify({ t: "resize", cols: t.term.cols, rows: t.term.rows })
        );
      }
    } catch (e) { /* ignore */ }
  });
}

function closePty(tabId) {
  const t = tabs.find((x) => x.id === tabId);
  if (!t || !t.ptyActive) return;
  t.ptyActive = false;
  setCtrl(false);
  if (t.ptyWs) {
    try { t.ptyWs.close(); } catch (e) { /* ignore */ }
    t.ptyWs = null;
  }
  if (t.term) {
    try { t.term.dispose(); } catch (e) { /* ignore */ }
    t.term = null;
    t.fitAddon = null;
  }
  if (t.termRO) {
    try { t.termRO.disconnect(); } catch (e) { /* ignore */ }
    t.termRO = null;
  }
  if (t.termHost && t.termHost.parentNode) {
    t.termHost.parentNode.removeChild(t.termHost);
  }
  t.termHost = null;

  if (activeId === tabId) {
    renderOutput();
    updateView();
    elCmd.focus();
  }
}

// Show the active tab's view: its live terminal (if it has one) or the line
// console. Background terminals keep running and are merely hidden.
function stripAnsi(s) {
  return String(s)
    .replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "")
    .replace(/\x1b[()][AB0]/g, "")
    .replace(/\x1b[=>]/g, "")
    .replace(/\x1b[78]/g, "");
}

// The PTY process ended on its own (one-shot command, or the user quit a
// full-screen program). Capture the output into the console's scrollback and
// drop back to the line console so the user isn't stuck in the dead terminal.
function onPtyExit(tabId, cwd) {
  const t = tabs.find((x) => x.id === tabId);
  if (!t) return;
  t._exited = true;
  if (cwd) t.cwd = cwd;               // stay where the shell ended up
  if (t.ptyText) {
    const lines = stripAnsi(t.ptyText).replace(/\r/g, "").split("\n");
    for (const l of lines) {
      if (l.trim() !== "") t.lines.push({ text: l });
    }
  }
  // Tell the server to tear down the finished session so it doesn't linger.
  if (t.ptyWs && t.ptyWs.readyState === 1) {
    try { t.ptyWs.send(JSON.stringify({ t: "kill" })); } catch (e) { /* ignore */ }
  }
  t.isTerminal = false;
  t.cmd = "";
  t.ptyText = "";
  closePty(tabId);
  renderOutput();
  updateView();
  elCmd.focus();
}

function updateView() {
  const t = activeTab();
  const app = document.getElementById("app");
  document.querySelectorAll(".term-host").forEach((h) => { h.style.display = "none"; });
  if (t && t.ptyActive && t.termHost) {
    app.classList.add("pty");
    t.termHost.style.display = "block";
    if (t.termHost.clientWidth > 0 && t.termHost.clientHeight > 0) {
      try { t.fitAddon.fit(); } catch (e) { /* ignore */ }
    }
    t.term.focus();
  } else {
    app.classList.remove("pty");
  }
}

function submitCmd() {
  const t = activeTab();
  const val = elCmd.value;
  elCmd.value = "";
  // echo the command (or the interactive answer) to output
  if (t.interactive) {
    t.lines.push({ text: t.interactivePrompt + val, cls: "term-amber" });
  } else {
    if (val.trim() !== "") {
      t.hist.push(val);
      t.histIdx = t.hist.length;
    }
    t.lines.push({ text: promptText(t.cwd) + val });
  }
  renderOutput();
  send(val);
}

// A <form> submit reliably catches the Enter key from mobile soft keyboards,
// which don't always emit a clean keydown "Enter" event.
document.getElementById("promptform").addEventListener("submit", (e) => {
  e.preventDefault();
  submitCmd();
});

elCmd.addEventListener("keydown", (e) => {
  const t = activeTab();
  if (e.key === "ArrowUp" && !t.interactive) {
    if (t.histIdx > 0) {
      t.histIdx -= 1;
      elCmd.value = t.hist[t.histIdx] || "";
      e.preventDefault();
    }
  } else if (e.key === "ArrowDown" && !t.interactive) {
    if (t.histIdx < t.hist.length - 1) {
      t.histIdx += 1;
      elCmd.value = t.hist[t.histIdx] || "";
    } else {
      t.histIdx = t.hist.length;
      elCmd.value = "";
    }
    e.preventDefault();
  }
});

elNew.onclick = () => newTab();
const elTheme = document.getElementById("themebtn");
if (elTheme) elTheme.onclick = cycleTheme;
// Clicking empty screen space focuses the prompt (opens the keyboard on phones).
// It must NOT do that for real controls: stealing focus from a <select> closes
// its dropdown the instant it opens, which made the inline "choose a system"
// picker impossible to use.
const INTERACTIVE_SELECTOR = "select, option, button, a, input, textarea, label, .choice-box";
document.getElementById("screen").addEventListener("click", (e) => {
  if (e.target.closest && e.target.closest(INTERACTIVE_SELECTOR)) return;
  const sel = window.getSelection && window.getSelection();
  if (sel && String(sel).length) return;          // let people select/copy output text
  elCmd.focus();
});

// ---------------------------------------------------------------------------
// Mobile key bar: Tab / Esc / Ctrl / arrow buttons for touch keyboards.
// ---------------------------------------------------------------------------
if (window.matchMedia && (window.matchMedia("(max-width: 768px)").matches ||
    "ontouchstart" in window || navigator.maxTouchPoints > 0)) {
  document.body.classList.add("touch");
}

// Keep the footer (input + key bar) visible above the soft keyboard by sizing
// #app to the visible viewport rather than the full window height.
let vpRaf = null;
let lastVpHeight = 0;
function fitToViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  // The soft keyboard animating or the caret scrolling fires resize/scroll
  // events constantly while you type. Only react when the *height* actually
  // changed (keyboard open/close, rotation) — otherwise we'd flood the backend
  // with resize messages and stall the terminal on mobile.
  if (vv.height === lastVpHeight) return;
  lastVpHeight = vv.height;
  if (vpRaf) cancelAnimationFrame(vpRaf);
  vpRaf = requestAnimationFrame(() => {
    document.getElementById("app").style.height = vv.height + "px";
    onPtyResize();
  });
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", fitToViewport);
  window.visualViewport.addEventListener("scroll", fitToViewport);
  fitToViewport();
}

function insertAtCursor(text) {
  const s = elCmd.selectionStart ?? elCmd.value.length;
  const e = elCmd.selectionEnd ?? elCmd.value.length;
  elCmd.value = elCmd.value.slice(0, s) + text + elCmd.value.slice(e);
  elCmd.selectionStart = elCmd.selectionEnd = s + text.length;
}

let ctrlActive = false;
let ctrlResetTimer = null;
const elKeybar = document.getElementById("keybar");

function setCtrl(active) {
  ctrlActive = active;
  const btn = elKeybar.querySelector('[data-mod="ctrl"]');
  if (btn) btn.classList.toggle("active", active);
  if (ctrlResetTimer) { clearTimeout(ctrlResetTimer); ctrlResetTimer = null; }
  // Auto-release the sticky modifier so it can never get "stuck" and turn the
  // next normal keystroke into a control char (e.g. Ctrl+X quitting nano).
  if (active) ctrlResetTimer = setTimeout(() => setCtrl(false), 3000);
}

function sendPty(d) {
  const t = activeTab();
  sendPtyTo(t, d);
}

function handlePtyKey(b) {
  const t = activeTab();
  if (!t || !t.ptyActive) return;
  setCtrl(false);
  if (b.dataset.key === "Esc") {
    // Esc detaches the PTY and returns to the line console (consistent with the
    // keyboard Esc shortcut).
    userExitPty(t.id);
    return;
  }
  if (b.dataset.ctrl) {
    // Explicit control combos (e.g. ^X to quit nano). Sending the control char
    // directly means normal typing is NEVER turned into a control sequence, so
    // a stray modifier can't accidentally quit the editor mid-edit.
    const ch = String.fromCharCode(b.dataset.ctrl.toUpperCase().charCodeAt(0) - 64);
    sendPtyTo(t, ch);
    if (t.term) t.term.focus();
    return;
  }
  const key = b.dataset.key;
  if (key === "Tab") {
    sendPtyTo(t, "\t");
  } else if (key === "Esc") {
    sendPtyTo(t, "\x1b");
  } else if (key && key.startsWith("Arrow")) {
    const seq = {
      ArrowUp: "\x1b[A", ArrowDown: "\x1b[B",
      ArrowLeft: "\x1b[D", ArrowRight: "\x1b[C",
    }[key];
    sendPtyTo(t, seq);
  }
  if (t.term) t.term.focus();
}

function moveCursor(delta) {
  const p = elCmd.selectionStart ?? elCmd.value.length;
  const np = Math.max(0, Math.min(elCmd.value.length, p + delta));
  elCmd.selectionStart = elCmd.selectionEnd = np;
}

function historyNav(dir) {
  const t = activeTab();
  if (t.interactive) return;
  if (dir < 0) {
    if (t.histIdx > 0) {
      t.histIdx -= 1;
      elCmd.value = t.hist[t.histIdx] || "";
    }
  } else {
    if (t.histIdx < t.hist.length - 1) {
      t.histIdx += 1;
      elCmd.value = t.hist[t.histIdx] || "";
    } else {
      t.histIdx = t.hist.length;
      elCmd.value = "";
    }
  }
  const end = elCmd.value.length;
  elCmd.selectionStart = elCmd.selectionEnd = end;
}

function isEditorPrompt() {
  const t = activeTab();
  if (!t) return false;
  return /^(edit|L\d+|ins)> /.test(t.interactivePrompt || "");
}

function doKeybarAction(b) {
  const t = activeTab();
  if (isPtyActive()) {
    handlePtyKey(b);
    return;
  }
  const key = b.dataset.key;
  if (t.interactive) {
    // Inside the in-console editor, arrow keys move the line cursor (up/down)
    // so you can navigate like nano on a phone; left/right still move the caret
    // within the command you're typing, and Tab/Esc behave normally.
    if (key === "ArrowUp" && isEditorPrompt()) { send("up"); return; }
    if (key === "ArrowDown" && isEditorPrompt()) { send("down"); return; }
    if (key === "Tab") { insertAtCursor("\t"); elCmd.focus({ preventScroll: true }); return; }
    if (key === "Esc") { elCmd.value = ""; elCmd.focus({ preventScroll: true }); return; }
    if (key === "ArrowLeft") { moveCursor(-1); elCmd.focus({ preventScroll: true }); return; }
    if (key === "ArrowRight") { moveCursor(1); elCmd.focus({ preventScroll: true }); return; }
    return;
  }
  if (key) {
    if (key === "Tab") {
      insertAtCursor("\t");
    } else if (key === "Esc") {
      elCmd.value = "";
    } else if (key === "ArrowLeft") {
      moveCursor(-1);
    } else if (key === "ArrowRight") {
      moveCursor(1);
    } else if (key === "ArrowUp") {
      historyNav(-1);
    } else if (key === "ArrowDown") {
      historyNav(1);
    }
    elCmd.focus({ preventScroll: true });
  }
}

// pointerdown + preventDefault keeps the text input focused (and the soft
// keyboard open) when a key-bar button is tapped, and removes the tap delay.
// Use click (not pointerdown+preventDefault): on mobile a tap is a valid user
// gesture that lets the soft keyboard open/remain, whereas preventDefault can
// swallow the gesture and leave the keyboard shut so taps appear to do nothing.
elKeybar.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  doKeybarAction(b);
});

// Keep the active terminal fitted on window (non-viewport) resizes.
window.addEventListener("resize", onPtyResize);

// ---------------------------------------------------------------------------
// Built-in code editor (client-side). All editing + syntax highlighting happen
// in the browser, so it stays smooth on phones and laptops alike; only Save
// touches the server.
// ---------------------------------------------------------------------------
const elEditor = document.getElementById("editor");
const elEditorFile = document.getElementById("editor-file");
const elEditorLang = document.getElementById("editor-lang");
const elEditorStatus = document.getElementById("editor-status");
const elEditorInput = document.getElementById("editor-input");
const elEditorPre = document.getElementById("editor-highlight");
const elEditorCode = elEditorPre.querySelector("code");
const elEditorGutter = document.getElementById("editor-gutter");
let editorState = null;
let hlRaf = null;

function escHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// --- language detection: extension -> rule id, plus special basenames ------
const LANG_BY_EXT = {
  py: "python", pyw: "python", pyi: "python", bzl: "python", sky: "python",
  gd: "python", mojo: "python", gyp: "python", gypi: "python",
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  flow: "javascript", es6: "javascript", jsm: "javascript",
  ts: "typescript", mts: "typescript", cts: "typescript", tsx: "typescript",
  json: "json", jsonc: "json", json5: "json", webmanifest: "json",
  geojson: "json", har: "json", jsonl: "json", map: "json", tsbuildinfo: "json",
  ipynb: "json",
  css: "css", scss: "css", sass: "css", less: "css", styl: "css",
  html: "html", htm: "html", xhtml: "html", ejs: "html", hbs: "html",
  mustache: "html", erb: "html", twig: "html", liquid: "html",
  j2: "html", jinja: "html", jinja2: "html", vue: "html", svelte: "html",
  astro: "html", cshtml: "html", razor: "html", jsp: "html", jspx: "html",
  heex: "html", eex: "html",
  xml: "xml", xsd: "xml", xsl: "xml", xslt: "xml", svg: "xml", plist: "xml",
  config: "xml", props: "xml", targets: "xml", resx: "xml", wsdl: "xml",
  rss: "xml", atom: "xml", opml: "xml", dtd: "xml", rng: "xml",
  xlf: "xml", xliff: "xml", kml: "xml", gpx: "xml", tcx: "xml",
  dita: "xml", ditamap: "xml", xaml: "xml", axml: "xml", nuspec: "xml",
  vcxproj: "xml", csproj: "xml", vbproj: "xml", fsproj: "xml", iml: "xml",
  wsf: "xml", wxs: "xml", wxi: "xml", jelly: "xml", ps1xml: "xml",
  yaml: "yaml", yml: "yaml",
  toml: "toml",
  ini: "ini", cfg: "ini", conf: "ini", properties: "ini", inf: "ini",
  reg: "ini", desktop: "ini", service: "ini", socket: "ini", timer: "ini",
  mount: "ini", automount: "ini", swap: "ini", path: "ini", slice: "ini",
  scope: "ini", mf: "ini", gitconfig: "ini", gitmodules: "ini",
  editorconfig: "ini", gitignore: "ini", gitattributes: "ini",
  dockerignore: "ini", npmrc: "ini", yarnrc: "ini", hgrc: "ini",
  md: "markdown", markdown: "markdown", mdown: "markdown", mkdn: "markdown",
  rmd: "markdown",
  rst: "rst", rest: "rst",
  tex: "latex", sty: "latex", cls: "latex", bib: "latex", dtx: "latex",
  ins: "latex", ltx: "latex",
  diff: "diff", patch: "diff", rej: "diff",
  sql: "sql", ddl: "sql", dml: "sql", mysql: "sql", pgsql: "sql",
  psql: "sql", sqlite: "sql", cql: "sql", hql: "sql", plsql: "sql",
  tsql: "sql", sparksql: "sql", presto: "sql", trino: "sql",
  gql: "graphql", graphql: "graphql",
  sh: "bash", bash: "bash", zsh: "bash", fish: "bash", ksh: "bash",
  csh: "bash", tcsh: "bash", ebuild: "bash", eclass: "bash",
  pl: "perl", pm: "perl", t: "perl",
  rb: "ruby", rbw: "ruby", rake: "ruby", gemspec: "ruby", ru: "ruby",
  php: "php", phtml: "php", php3: "php", php4: "php", php5: "php",
  phps: "php", module: "php",
  lua: "lua",
  r: "r",
  jl: "julia",
  ps1: "powershell", psm1: "powershell", psd1: "powershell",
  bat: "powershell", cmd: "powershell", btm: "powershell",
  awk: "awk",
  tcl: "tcl",
  vim: "vim", vimrc: "vim", gvimrc: "vim", nvimrc: "vim",
  c: "c", h: "c", i: "c", odin: "c",
  cpp: "cpp", hpp: "cpp", hh: "cpp", cc: "cpp", cxx: "cpp", hxx: "cpp",
  tpp: "cpp", ipp: "cpp", ino: "cpp", pde: "cpp", cu: "cpp", cuh: "cpp",
  hip: "cpp", mm: "cpp", cp: "cpp", cxx_: "cpp", d: "cpp",
  "c++": "cpp", C: "cpp",
  cs: "csharp", csx: "csharp", hx: "csharp", vala: "csharp", vapi: "csharp",
  java: "java", jj: "java", jjt: "java",
  kt: "kotlin", kts: "kotlin",
  scala: "scala", sc: "scala", sbt: "scala",
  groovy: "groovy", gradle: "groovy", gvy: "groovy",
  go: "go",
  rs: "rust",
  zig: "zig", zon: "zig",
  swift: "swift",
  dart: "dart",
  hs: "haskell", lhs: "haskell", elm: "haskell", purs: "haskell",
  ml: "ocaml", mli: "ocaml", mll: "ocaml", mly: "ocaml",
  ex: "elixir", exs: "elixir",
  erl: "elixir", hrl: "elixir", app: "elixir", escript: "elixir",
  clj: "clojure", cljs: "clojure", cljc: "clojure", edn: "clojure",
  bb: "clojure",
  lisp: "lisp", lsp: "lisp", cl: "lisp", el: "lisp",
  scm: "lisp", ss: "lisp", rkt: "lisp",
  fs: "fsharp", fsi: "fsharp", fsx: "fsharp",
  vb: "vb", vbs: "vb", vba: "vb",
  pas: "pascal", dpr: "pascal", dpk: "pascal",
  ada: "ada", adb: "ada", ads: "ada",
  nim: "nim", nims: "nim",
  f: "fortran", f90: "fortran", f95: "fortran", f03: "fortran",
  for: "fortran", fpp: "fortran",
  v: "verilog", sv: "verilog", svh: "verilog", vh: "verilog",
  vhd: "vhdl", vhdl: "vhdl",
  asm: "asm", s: "asm", nasm: "asm",
  dot: "dot", gv: "dot",
  po: "po", pot: "po",
  proto: "proto",
  sol: "solidity",
  pp: "puppet", epp: "puppet",
  m: "matlab", sci: "matlab",
  cmake: "cmake", dockerfile: "docker",
  mk: "make", mak: "make", ninja: "make",
  htaccess: "apache",
  // Plain text: docs, data dumps and binaries (honest label, no highlight).
  txt: "text", text: "text", log: "text", out: "text", asc: "text",
  nfo: "text", diz: "text", csv: "text", tsv: "text", psv: "text",
  dat: "text", bin: "text", pid: "text", lock: "text", sum: "text",
  pem: "text", key: "text", crt: "text", cer: "text", csr: "text",
  der: "text", p12: "text", pfx: "text", jks: "text", keystore: "text",
  png: "text", jpg: "text", jpeg: "text", gif: "text", webp: "text",
  ico: "text", bmp: "text", tif: "text", tiff: "text", heic: "text",
  avif: "text", mp3: "text", wav: "text", ogg: "text", oga: "text",
  flac: "text", aac: "text", m4a: "text", opus: "text", mid: "text",
  midi: "text", mp4: "text", mkv: "text", avi: "text", mov: "text",
  webm: "text", m4v: "text", ogv: "text", flv: "text", wmv: "text",
  mpg: "text", mpeg: "text", zip: "text", tar: "text", gz: "text",
  tgz: "text", bz2: "text", tbz2: "text", xz: "text", txz: "text",
  "7z": "text", rar: "text", zst: "text", lz4: "text", cab: "text",
  msi: "text", deb: "text", rpm: "text", apk: "text", ipa: "text",
  jar: "text", war: "text", ear: "text", pdf: "text",
  exe: "text", dll: "text", sys: "text", com: "text", scr: "text",
  cpl: "text", so: "text", dylib: "text", o: "text", a: "text",
  lib: "text", obj: "text", ko: "text", elf: "text", class: "text",
  pyc: "text", pyo: "text", mo: "text",
  ttf: "text", otf: "text", woff: "text", woff2: "text", eot: "text",
  fnt: "text", fon: "text", sqlite3: "text", db: "text", db3: "text",
  sdb: "text", mdb: "text", accdb: "text", fdb: "text", odb: "text",
  h5: "text", hdf5: "text", mat: "text", rdata: "text", rda: "text",
  pickle: "text", pkl: "text", npy: "text", npz: "text", pt: "text",
  pth: "text", ckpt: "text", onnx: "text", parquet: "text", orc: "text",
  avro: "text", arrow: "text", feather: "text",
  doc: "text", docx: "text", xls: "text", xlsx: "text", ppt: "text",
  pptx: "text", odt: "text", ods: "text", odp: "text", pages: "text",
  numbers: "text", key: "text", epub: "text", swf: "text", psd: "text",
  ai: "text", sketch: "text", fig: "text",
  dmg: "text", iso: "text", img: "text", vhd: "text", vmdk: "text",
  qcow2: "text", torrent: "text", chm: "text", wasm: "text",
  srt: "text", vtt: "text", ssa: "text", ass: "text", sub: "text",
  ics: "text", vcs: "text", vcf: "text", sln: "text", suo: "text",
};
// Exact basenames (lowercased) that imply a language without an extension.
const LANG_BY_NAME = {
  dockerfile: "docker", containerfile: "docker", earthfile: "docker",
  makefile: "make", gnumakefile: "make", "makefile.am": "make",
  justfile: "make", cmakelists: "cmake", "cmakelists.txt": "cmake",
  jenkinsfile: "groovy", vagrantfile: "ruby", gemfile: "ruby",
  rakefile: "ruby", podfile: "ruby", fastfile: "ruby", appfile: "ruby",
  deliverfile: "ruby", snapfile: "ruby", scanfile: "ruby", gymfile: "ruby",
  matchfile: "ruby", brewfile: "ruby", capfile: "ruby", guardfile: "ruby",
  thorfile: "ruby", berksfile: "ruby", cheffile: "ruby",
  build: "python", "build.bazel": "python", workspace: "python",
  tiltfile: "python", sconstruct: "python", sconscript: "python",
  buck: "python", targets: "python", snakefile: "python", wscript: "python",
  "meson.build": "python", "conanfile.txt": "ini", pipfile: "toml",
  "cargo.lock": "toml", "poetry.lock": "toml", "uv.lock": "toml",
  "pipfile.lock": "toml", procfile: "yaml", pkgbuild: "bash",
  apkbuild: "bash", "manifest.in": "ini", "citation.cff": "yaml",
  ".babelrc": "json", ".bowerrc": "json", ".eslintrc": "json",
  ".jshintrc": "json", ".prettierrc": "json", ".stylelintrc": "json",
  ".firebaserc": "json", ".nycrc": "json",
  ".npmrc": "ini", ".yarnrc": "ini", ".editorconfig": "ini",
  ".gitconfig": "ini", ".gitmodules": "ini", ".hgrc": "ini",
  ".inputrc": "ini", ".coveragerc": "ini", ".flake8": "ini",
  ".pylintrc": "ini", ".wgetrc": "ini", ".nanorc": "ini",
  ".gitignore": "ini", ".gitattributes": "ini", ".dockerignore": "ini",
  ".gcloudignore": "ini", ".eslintignore": "ini", ".prettierignore": "ini",
  ".npmignore": "ini", ".htaccess": "apache", "nginx.conf": "nginx",
  ".vimrc": "vim", _vimrc: "vim", ".gvimrc": "vim",
  ".bashrc": "bash", ".bash_profile": "bash", ".profile": "bash",
  ".zshrc": "bash", ".zprofile": "bash", ".zshenv": "bash",
  ".zlogin": "bash", ".zlogout": "bash", ".kshrc": "bash",
  ".cshrc": "bash", ".tcshrc": "bash", ".login": "bash", ".logout": "bash",
  ".tmux.conf": "bash", ".screenrc": "bash",
  "prefs.js": "javascript", "user.js": "javascript",
  readme: "markdown", contributing: "markdown", changelog: "markdown",
  history: "markdown", news: "markdown",
};
function langFromPath(p) {
  const raw = ((p || "").split("/").pop() || "").split("\\").pop() || "";
  const base = raw.trim();
  const lower = base.toLowerCase();
  if (Object.prototype.hasOwnProperty.call(LANG_BY_NAME, lower)) return LANG_BY_NAME[lower];
  if (lower === ".env" || lower.startsWith(".env.")) return "ini";
  // Original-case extension first, so case-significant extensions (e.g. .C
  // for C++ on case-sensitive filesystems) resolve before lowercasing.
  const rawDot = base.lastIndexOf(".");
  const rawExt = rawDot >= 0 ? base.slice(rawDot + 1) : "";
  if (rawExt && Object.prototype.hasOwnProperty.call(LANG_BY_EXT, rawExt)) return LANG_BY_EXT[rawExt];
  const dot = lower.lastIndexOf(".");
  const ext = dot >= 0 ? lower.slice(dot + 1) : "";
  if (ext && Object.prototype.hasOwnProperty.call(LANG_BY_EXT, ext)) return LANG_BY_EXT[ext];
  return "text";
}

// Rule builders: keyword lists are space-separated words.
function _k(words) { return "\\b(" + words.trim().split(/\s+/).join("|") + ")\\b"; }
const _CSTR = ["string", "\"[^\"\\\\\\n]*\""];
const _SSTR = ["string", "'[^'\\\\\\n]*'"];
const _NUM = ["number", "\\b\\d[\\d_]*(\\.\\d+)?([eE][+-]?\\d+)?\\b|\\b0[xX][0-9a-fA-F_]+\\b|\\b0[oO][0-7_]+\\b|\\b0[bB][01_]+\\b"];
const _FUNC = ["function", "([A-Za-z_$]\\w*)\\s*(?=\\()"];
const _MOP = ["operator", "===|!==|==|!=|<=|>=|=>|->|::|&&|\\|\\||\\+\\+|--|<<|>>|\\.\\.\\."];
// C-family: // + /* */ comments, quoted strings, numbers, keywords...
function clike(o) {
  const r = [["comment", "//[^\\n]*"], ["comment", "/\\*[\\s\\S]*?\\*/"], _CSTR, _SSTR];
  if (o.template) r.push(["string", "`[^`\\\\]*`"]);
  if (o.vars) r.push(["variable", o.vars]);
  r.push(_NUM);
  // bool/type before keyword: words like "string"/"true" that appear in both
  // lists get their more specific color instead of generic keyword purple.
  if (o.bools) r.push(["bool", _k(o.bools)]);
  if (o.types) r.push(["type", _k(o.types)]);
  r.push(["keyword", _k(o.kw)]);
  if (o.decorator) r.push(["decorator", "@[A-Za-z_]\\w*"]);
  r.push(_FUNC);
  if (o.builtins) r.push(["builtin", _k(o.builtins)]);
  r.push(_MOP);
  if (o.extra) o.extra.forEach((x) => r.push(x));
  return r;
}
// Hash-comment family: # comments, quoted strings, numbers, keywords...
function hashlike(o) {
  const r = [["comment", "#[^\\n]*"]];
  if (o.triple) r.push(["string", "'''[\\s\\S]*?'''"], ["string", '"""[\\s\\S]*?"""']);
  r.push(_CSTR, _SSTR);
  if (o.vars) r.push(["variable", o.vars]);
  r.push(_NUM);
  if (o.bools) r.push(["bool", _k(o.bools)]);
  if (o.types) r.push(["type", _k(o.types)]);
  r.push(["keyword", _k(o.kw)]);
  if (o.decorator) r.push(["decorator", "@[A-Za-z_]\\w*"]);
  r.push(_FUNC);
  if (o.builtins) r.push(["builtin", _k(o.builtins)]);
  if (o.extra) o.extra.forEach((x) => r.push(x));
  return r;
}

// Per-language token rules: [className, regexSource]. Comments/strings first.
// Case-insensitive languages get an "i" flag via HL_FLAGS below.
const HL_FLAGS = {
  batch: "gi", sql: "gi", vb: "gi", pascal: "gi", ada: "gi",
  fortran: "gi", vhdl: "gi", asm: "gi", powershell: "gi", ini: "gi",
  cmake: "gi", docker: "gi",
};
const HL_RULES = {
  python: [
    ["comment", "#[^\\n]*"],
    ["string", "'''[\\s\\S]*?'''"],
    ["string", '"""[\\s\\S]*?"""'],
    ["string", "'[^'\\\\\\n]*'"],
    ["string", '"[^"\\\\\\n]*"'],
    ["number", "\\b\\d+(\\.\\d+)?\\b"],
    ["keyword", "\\b(def|class|import|from|as|return|if|elif|else|for|while|break|continue|pass|with|try|except|finally|raise|lambda|yield|global|nonlocal|assert|del|in|is|not|and|or|async|await|print|match|case)\\b"],
    ["bool", "\\b(True|False|None)\\b"],
    ["decorator", "@[A-Za-z_]\\w*"],
    ["function", "([A-Za-z_]\\w*)\\s*(?=\\()"],
    ["builtin", "\\b(self|cls|super|len|range|str|int|float|list|dict|set|tuple|open|enumerate|zip|map|filter|sorted|reversed|sum|min|max|abs|type|isinstance|object|Exception)\\b"],
  ],
  javascript: [
    ["comment", "//[^\\n]*"],
    ["comment", "/\\*[\\s\\S]*?\\*/"],
    ["string", "`[^`]*`"],
    ["string", "'[^'\\\\\\n]*'"],
    ["string", '"[^"\\\\\\n]*"'],
    ["number", "\\b\\d+(\\.\\d+)?\\b"],
    ["keyword", "\\b(function|return|if|else|for|while|var|let|const|class|extends|new|import|export|from|default|try|catch|finally|throw|typeof|instanceof|in|of|await|async|yield|switch|case|break|continue|do|void|delete|this|super)\\b"],
    ["bool", "\\b(true|false|null|undefined|NaN)\\b"],
    ["function", "([A-Za-z_$]\\w*)\\s*(?=\\()"],
    ["builtin", "\\b(console|document|window|require|module|exports|process|Math|JSON|Object|Array|String|Number|Boolean|Promise|Map|Set|Symbol)\\b"],
  ],
  typescript: clike({
    template: true, decorator: true,
    kw: "abstract any as asserts async await break case catch class const constructor continue debugger declare default delete do else enum export extends false finally for from function get if implements import in infer instanceof interface is keyof let module namespace never new null number object of private protected public readonly require return satisfies set static string super switch symbol this throw true try type typeof undefined unknown var void while with yield",
    types: "string number boolean bigint symbol void null undefined any unknown never object Array Record Map Set Promise Partial Required",
    bools: "true false null undefined NaN",
    builtins: "console document window require module exports process Math JSON Object Array String Number Boolean Promise Map Set Symbol parseInt parseFloat isNaN setTimeout setInterval clearTimeout clearInterval fetch",
  }),
  json: [
    ["string", '"[^"\\\\\\n]*"(?=\\s*:)'],
    ["string", '"[^"\\\\\\n]*"'],
    ["number", "-?\\b\\d+(\\.\\d+)?([eE][+-]?\\d+)?\\b"],
    ["bool", "\\b(true|false|null)\\b"],
    ["punct", "[{}\\[\\]:,]"],
  ],
  html: [
    ["comment", "<!--[\\s\\S]*?-->"],
    ["string", '"[^"\\\\\\n]*"|\'[^\']*\''],
    ["tag", "</?[a-zA-Z][a-zA-Z0-9]*"],
    ["attr", "\\b[a-zA-Z-]+(?==)"],
  ],
  css: [
    ["comment", "/\\*[\\s\\S]*?\\*/"],
    ["string", '"[^"\\\\\\n]*"|\'[^\']*\''],
    ["keyword", "@media|@import|@keyframes|@font-face|@supports"],
    ["attr", "[a-zA-Z-]+(?=\\s*:)"],
    ["number", "#[0-9a-fA-F]{3,8}\\b|\\b\\d+(\\.\\d+)?(px|em|rem|%|s|ms|vh|vw|fr|pt|deg)?\\b"],
  ],
  yaml: [
    ["comment", "#[^\\n]*"],
    ["string", '"[^"\\\\\\n]*"|\'[^\']*\''],
    ["attr", "^[\\s-]*[A-Za-z0-9_.-]+(?=:)"],
    ["bool", "\\b(true|false|null|yes|no|~)\\b"],
    ["number", "\\b\\d+(\\.\\d+)?\\b"],
  ],
  toml: [
    ["comment", "#[^\\n]*"],
    ["string", "'''[\\s\\S]*?'''"],
    ["string", '"""[\\s\\S]*?"""'],
    ["string", "'[^'\\n]*'"],
    _CSTR,
    ["keyword", "^\\[{1,2}[^\\]\\n]*\\]{1,2}"],
    ["attr", "^[A-Za-z0-9_.-]+(?=\\s*=)"],
    ["number", "\\b\\d{4}-\\d{2}-\\d{2}([T ]\\d{2}:\\d{2}(:\\d{2}(\\.\\d+)?(Z|[+-]\\d{2}:?\\d{2})?)?)?\\b"],
    ["number", "[+-]?\\b\\d[\\d_]*(\\.\\d+)?([eE][+-]?\\d+)?\\b|\\b0[xob][0-9a-fA-F_]+\\b"],
    ["bool", "\\b(true|false)\\b"],
  ],
  ini: [
    ["comment", "[#;][^\\n]*"],
    _CSTR, _SSTR,
    ["keyword", "^\\[[^\\]\\n]*\\]"],
    ["attr", "^[A-Za-z0-9_.\\- ]+(?=\\s*=)"],
    ["bool", "\\b(true|false|yes|no|on|off)\\b"],
    _NUM,
  ],
  bash: [
    ["comment", "#[^\\n]*"],
    ["string", '"[^"\\\\\\n]*"|\'[^\']*\''],
    ["keyword", "\\b(if|then|else|elif|fi|for|in|do|done|while|case|esac|function|return|export|local|echo|cd|exit|source|set)\\b"],
    ["builtin", "\\b(ls|cd|pwd|cat|echo|grep|sed|awk|cp|mv|rm|mkdir|touch|sudo|apt|pip|python|python3|git|chmod|chown)\\b"],
  ],
  markdown: [
    ["comment", "^#{1,6} .*"],
    ["string", "`[^`]*`"],
    ["attr", "\\[[^\\]]*\\]\\([^\\)]*\\)"],
    ["keyword", "^\\s*[-*+] "],
    ["bool", "^\\s*\\d+\\. "],
  ],
  rst: [
    ["comment", "^\\.\\. .*"],
    ["decorator", "^\\.\\. [A-Za-z_-]+::"],
    ["string", "``[^`]*``"],
    ["string", "`[^`\\n]*`_?"],
    ["attr", "\\[[^\\]]*\\]_|^[A-Za-z0-9_.~-]+(?=::?\\s*$)"],
    ["keyword", "^[=\\-~^\"#*+]{3,}$"],
  ],
  latex: [
    ["comment", "%[^\\n]*"],
    ["keyword", "\\\\[A-Za-z]+|\\\\[^A-Za-z\\s]"],
    ["string", "\\$[^$\\n]*\\$"],
    ["punct", "[{}]"],
    _NUM,
  ],
  diff: [
    ["keyword", "^(\\+\\+\\+|---|diff |index | Cram ).*"],
    ["string", "^\\+.*"],
    ["comment", "^-.*"],
    ["attr", "^@.*"],
    ["number", "^\\d+(,\\d+)?[acd]\\d+.*"],
  ],
  sql: [
    ["comment", "--[^\\n]*"],
    ["comment", "/\\*[\\s\\S]*?\\*/"],
    _SSTR, _CSTR,
    ["string", "`[^`\\n]*`"],
    ["string", "\\[[^\\]\\n]*\\]"],
    _NUM,
    ["keyword", _k("select from where group by order having limit offset join inner outer left right full cross on using union all distinct as with recursive values into insert update delete set create table view index alter drop truncate database schema column constraint primary key foreign references unique check default not null and or in is like between exists case when then else end asc all any some cast convert over partition rows range unbounded preceding following current row only with ties fetch first next percent except intersect minus merge using returning join lateral pivot unpivot")],
    ["bool", _k("true false null unknown")],
    ["type", _k("int integer bigint smallint tinyint decimal numeric float real double precision money char varchar nchar nvarchar text ntext binary varbinary image bit date time datetime datetime2 timestamp interval year boolean bool json jsonb xml uuid serial bigserial")],
    ["builtin", _k("count sum avg min max coalesce nullif greatest least upper lower trim substring length cast convert row_number rank dense_rank lag lead first_value last_value now current_date current_timestamp")],
  ],
  graphql: [
    ["comment", "#[^\\n]*"],
    ["string", '"""[\\s\\S]*?"""'],
    _CSTR,
    ["variable", "\\$[A-Za-z_]\\w*"],
    ["decorator", "@[A-Za-z_]\\w*"],
    ["keyword", _k("query mutation subscription fragment on type interface union enum input scalar extend schema directive repeatable")],
    ["type", "\\b[A-Z][A-Za-z0-9_]*\\b"],
    ["bool", _k("true false null")],
    _FUNC,
  ],
  docker: [
    ["comment", "#[^\\n]*"],
    _CSTR, _SSTR,
    ["variable", "\\$[A-Za-z_][\\w]*|\\$\\{[^}]*\\}"],
    ["keyword", "^(FROM|RUN|CMD|LABEL|MAINTAINER|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|VOLUME|USER|WORKDIR|ARG|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL)\\b"],
    ["bool", "\\b(true|false)\\b"],
  ],
  make: [
    ["comment", "#[^\\n]*"],
    _CSTR, _SSTR,
    ["variable", "\\$[@<^?*%|]|\\$[({][^})]*[})]|\\$[A-Za-z_]"],
    ["function", "^[\\w%.][^:#=\\n]*::?"],
    ["keyword", "^(include|-include|sinclude|ifeq|ifneq|ifdef|ifndef|else|endif|define|endef|undefine|override|export|unexport|private|vpath)\\b"],
  ],
  cmake: [
    ["comment", "#[^\\n]*"],
    _CSTR,
    ["variable", "\\$\\{[^}]*\\}|\\$[A-Za-z_]+\\{[^}]*\\}"],
    _NUM,
    ["keyword", _k("if elseif else endif foreach endforeach while endwhile function endfunction macro endmacro return break continue")],
    ["bool", _k("TRUE FALSE ON OFF YES NO")],
    _FUNC,
  ],
  apache: [
    ["comment", "#[^\\n]*"],
    _CSTR, _SSTR,
    ["variable", "\\$[A-Za-z_]+|%\\{[^}]*\\}[a-z]*"],
    ["tag", "</?[A-Za-z][^\\s>]*"],
    ["keyword", "^[A-Za-z][A-Za-z0-9]*"],
  ],
  nginx: [
    ["comment", "#[^\\n]*"],
    _CSTR, _SSTR,
    ["variable", "\\$[A-Za-z_]+"],
    ["keyword", "^[a-z_]+"],
    ["bool", "\\b(on|off)\\b"],
    _NUM,
  ],
  c: clike({
    kw: "auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while _Bool _Complex _Imaginary alignas alignof static_assert thread_local",
    types: "int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t size_t ssize_t ptrdiff_t intptr_t uintptr_t wchar_t char16_t char32_t FILE DIR pid_t uid_t gid_t off_t time_t clock_t va_list jmp_buf fpos_t int char float double void bool short long signed unsigned struct union enum",
    bools: "true false NULL",
    builtins: "printf scanf main malloc free calloc realloc memcpy memmove memset strlen strcmp strncmp strcpy strncpy strcat fopen fclose fread fwrite fprintf fscanf sprintf snprintf sscanf exit abort atexit assert qsort bsearch abs labs rand srand time toupper tolower stdin stdout stderr errno",
  }),
  cpp: clike({
    kw: "alignas alignof assert auto break case catch char class concept const consteval constinit constexpr continue co_await co_return co_yield decltype default delete do double dynamic_cast else enum explicit export extern false final float for friend goto if import inline int long module mutable namespace new noexcept nullptr operator private protected public register reinterpret_cast requires return short signed sizeof static static_assert static_cast struct switch template this thread_local throw true try typedef typeid typename union unsigned using virtual void volatile while",
    types: "int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t size_t string wstring vector map set unordered_map unordered_set array tuple pair optional variant string_view span int char float double void bool short long signed unsigned auto",
    bools: "true false nullptr NULL",
    builtins: "std cout cin cerr clog endl string vector map set move forward make_shared make_unique make_pair printf scanf malloc free new delete",
  }),
  csharp: clike({
    template: true,
    kw: "abstract as base break byte case catch char checked class const continue decimal default delegate do double else enum event explicit extern false finally fixed float for foreach goto if implicit in int interface internal is lock long namespace new null object operator out override params private protected public readonly ref return sbyte sealed short sizeof stackalloc static string struct switch this throw true try typeof uint ulong unchecked unsafe ushort using var virtual void volatile while add alias ascending async await by descending dynamic equals from get global group into join let nameof on orderby partial remove select set value when where with yield",
    types: "int string bool object var dynamic void char byte sbyte short ushort uint long ulong float double decimal DateTime TimeSpan Guid List Dictionary IEnumerable Task Action Func",
    bools: "true false null",
    builtins: "Console WriteLine ReadLine Convert Math String List Dictionary Enumerable Task",
    extra: [["string", '@"[^"]*"'], ["string", '\\$"[^"\\\\\\n]*"']],
  }),
  java: clike({
    decorator: true,
    kw: "abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for goto if implements import instanceof int interface long native new package private protected public return short static strictfp super switch synchronized this throw throws transient try var void volatile while",
    types: "byte short int long float double boolean char void String Object List Map Set ArrayList HashMap Integer Long Double Boolean Character Exception var",
    bools: "true false null",
    builtins: "System out println print String Math Object Integer List Map Arrays Collections Objects",
    extra: [["string", '"""[\\s\\S]*?"""']],
  }),
  kotlin: clike({
    template: true,
    kw: "as break class continue do else false for fun if in interface is null object package return super this throw true try typealias typeof val var when while by catch constructor delegate dynamic field finally get import inner internal lateinit open operator out param property receiver set setparam value where actual abstract annotation companion const crossinline data enum expect external final infix inline internal noinline open operator out override private protected public reified sealed suspend tailrec vararg",
    types: "Int Long Short Byte Float Double Boolean Char String Unit Nothing Any List Map Set Array",
    bools: "true false null",
    builtins: "println print listOf mapOf setOf arrayOf",
  }),
  scala: clike({
    template: true,
    kw: "abstract case catch class def do else extends false final finally for forSome if implicit import lazy match new null object override package private protected return sealed super this throw trait try true type val var while with yield",
    types: "Int Long Short Byte Float Double Boolean Char String Unit Null Nothing Any AnyRef List Map Set Option Some None Array",
    bools: "true false null",
    builtins: "println print List Map Set Option Some None",
  }),
  groovy: clike({
    template: true,
    kw: "as assert break case catch class const continue def do else false finally for goto if implements import in instanceof interface new null package return super switch this throw throws trait true try while enum extends",
    types: "def var String Object List Map Integer Boolean void",
    bools: "true false null",
    builtins: "println print assert",
  }),
  go: clike({
    kw: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var",
    types: "bool string int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 uintptr float32 float64 complex64 complex128 byte rune error any",
    bools: "true false nil iota",
    builtins: "make new len cap append copy delete panic recover print println close min max clear",
  }),
  rust: clike({
    kw: "as break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while async await dyn abstract become box do final macro override priv typeof unsized virtual yield try",
    types: "bool char str String i8 i16 i32 i64 i128 u8 u16 u32 u64 u128 isize usize f32 f64 Option Result Vec Box Rc Arc Err Ok Some None",
    bools: "true false",
    builtins: "println print vec String Some None Ok Err panic assert format",
    extra: [["decorator", "#\\[[^\\]\\n]*\\]"]],
  }),
  zig: clike({
    kw: "addrspace align allowzero and anyerror anyframe anytype asm async await break catch comptime const continue defer else enum errdefer error export extern false fn for if inline linksection nakedcc noinline null or orelse packed pub resume return struct suspend switch test threadlocal true try undefined union unreachable usingnamespace var volatile while",
    types: "void bool noreturn type i8 i16 i32 i64 i128 u8 u16 u32 u64 u128 isize usize f16 f32 f64 f80 f128",
    bools: "true false null undefined",
    builtins: "unreachable",
  }),
  swift: clike({
    decorator: true,
    kw: "associatedtype break case catch class continue default defer deinit do else enum extension fallthrough false fileprivate for func guard if import in init inout internal is lazy let nil open operator override precedence private protocol public repeat required return self Self static struct subscript super switch throw throws true try typealias var where while as convenience didSet dynamic final get indirect infix lazy mutating optional postfix prefix required set unowned weak willSet",
    types: "Int Int8 Int16 Int32 Int64 UInt Float Double Bool String Character Array Dictionary Set Optional Any AnyObject Void Never",
    bools: "true false nil",
    builtins: "print debugPrint String Int assertionFailure fatalError",
  }),
  dart: clike({
    template: true, decorator: true,
    kw: "abstract as assert async await break case catch class const continue covariant default deferred do dynamic else enum export extends extension external factory false final finally for get hide if implements import in late library mixin new null on operator part required rethrow return sealed set show static super switch sync this throw true try typedef var void while with yield",
    types: "int double num bool String List Map Set dynamic void var late",
    bools: "true false null",
    builtins: "print assert",
  }),
  php: [
    ["comment", "//[^\\n]*"],
    ["comment", "#[^\\n]*"],
    ["comment", "/\\*[\\s\\S]*?\\*/"],
    _CSTR, _SSTR,
    ["variable", "\\$[A-Za-z_]\\w*"],
    _NUM,
    ["keyword", _k("abstract and array as break callable case catch class clone const continue declare default do echo else elseif empty enddeclare endfor endforeach endif endswitch endwhile exit extends final finally fn for foreach function global goto if implements include include_once instanceof insteadof interface isset list namespace new or print private protected public readonly require require_once return static switch throw trait try unset use var while xor yield from")],
    ["bool", _k("true false null")],
    _FUNC,
    ["builtin", _k("echo print count strlen strpos array_merge in_array isset empty unset die exit var_dump print_r")],
    _MOP,
  ],
  perl: hashlike({
    vars: "[$@%][A-Za-z_]\\w*(?:::\\w+)*",
    kw: "BEGIN END CHECK INIT else elsif eq for foreach given gt ge lt le ne no not or and xor unless until while when default continue do goto if last next redo return sub package use require our my local state say print printf push pop shift unshift split join map grep sort reverse keys values each defined undef bless tie tied ref wantarray die warn eval",
    bools: "undef",
    builtins: "print say printf sprintf push pop shift unshift split join map grep sort reverse keys values each defined ref die warn eval open close chomp chop length substr index",
  }),
  ruby: hashlike({
    vars: "[$@][A-Za-z_]\\w*|:[A-Za-z_][\\w?!]*",
    kw: "alias and begin BEGIN break case class def defined do else elsif end END ensure false for if in module next nil not or redo rescue retry return self super then true undef unless until when while yield",
    bools: "true false nil",
    builtins: "puts print p require require_relative include extend attr_reader attr_writer attr_accessor new lambda proc raise fail loop",
  }),
  lua: [
    ["comment", "--[^\\n]*"],
    ["string", "\\[\\[[\\s\\S]*?\\]\\]"],
    _CSTR, _SSTR,
    _NUM,
    ["keyword", _k("and break do else elseif end false for function goto if in local nil not or repeat return then true until while")],
    ["bool", _k("true false nil")],
    _FUNC,
    ["builtin", _k("print require pairs ipairs tostring tonumber type setmetatable getmetatable rawget rawset table string io os math")],
  ],
  r: hashlike({
    kw: "if else repeat while function for in next break return switch library require source stop warning message cat print paste data.frame list c invisible",
    bools: "TRUE FALSE NULL NA Inf NaN",
    builtins: "c list data.frame library require source print paste cat length nrow ncol dim names",
    extra: [["operator", "<-|->|%>%|==|!=|<=|>="]],
  }),
  julia: hashlike({
    kw: "baremodule begin break catch const continue do else elseif end export false finally for function global if import in isa let local macro module quote return struct true try using where while abstract type primitive type mutable struct",
    types: "Int8 Int16 Int32 Int64 Int128 UInt8 UInt16 UInt32 UInt64 UInt128 Float16 Float32 Float64 Bool Char String Symbol Any Nothing Missing Union Tuple Array Dict",
    bools: "true false nothing missing",
    builtins: "println print push! pop! length size eltype typeof include",
  }),
  powershell: [
    ["comment", "<#[\\s\\S]*?#>"],
    ["comment", "#[^\\n]*"],
    _CSTR, _SSTR,
    ["variable", "\\$[A-Za-z_][\\w:]*|\\${[^}]*\\}|\\$\\?"],
    _NUM,
    ["keyword", _k("function filter foreach for if elseif else switch while do until break continue return exit throw trap try catch finally in param begin process end dynamicparam class enum using namespace hidden static")],
    ["function", "\\b[A-Z]\\w+-[A-Z]\\w+\\b"],
    ["bool", "\\$(true|false|null)"],
    _FUNC,
  ],
  batch: [
    ["comment", "rem\\b[^\\n]*|::[^\\n]*"],
    ["string", '"[^"\\n]*"'],
    ["variable", "%[A-Za-z_][\\w]*%|%\\d|%%[A-Za-z]|![A-Za-z_][\\w]*!"],
    ["function", "^:[^\\n]*"],
    ["keyword", _k("echo off on setlocal endlocal enabledelayedexpansion if else for in do goto call exit pause cls cd dir del copy move ren type rem set shift title color prompt path defined exist errorlevel")],
  ],
  awk: [
    ["comment", "#[^\\n]*"],
    _CSTR, _SSTR,
    ["variable", "\\$\\d+"],
    _NUM,
    ["keyword", _k("BEGIN END function return if else for in while do break continue next exit print printf getline delete split substr length")],
    _FUNC,
    ["builtin", _k("print printf getline length substr split")],
  ],
  tcl: [
    ["comment", "#[^\\n]*"],
    _CSTR, _SSTR,
    ["string", "\\{[^{}]*\\}"],
    ["variable", "\\$[A-Za-z_][\\w:()]*|\\${[^}]*}"],
    _NUM,
    ["keyword", _k("proc return if then else elseif for foreach while break continue switch default case set append lappend unset global upvar variable namespace package require source eval exec exit error info puts gets open close read write after update vwait")],
    _FUNC,
    ["builtin", _k("puts gets set proc return")],
  ],
  vim: [
    ["comment", "^\\s*\".*"],
    _CSTR, _SSTR,
    ["variable", "\\$[A-Za-z_]\\w*"],
    _NUM,
    ["keyword", _k("set setlocal let unlet function endfunction return if else elseif endif for endfor while endwhile break continue try catch finally endtry map nmap imap vmap nnoremap inoremap vnoremap autocmd augroup highlight syntax runtime source edit call execute normal echo echomsg finish abort delcommand command filetype plugin indent on off only bdelete buffer buffers quit q w qa wq")],
    _FUNC,
    ["builtin", _k("expand executable filereadable has exists")],
  ],
  haskell: [
    ["comment", "\\{-[\\s\\S]*?-\\}"],
    ["comment", "--[^\\n]*"],
    _CSTR,
    ["string", "'[^'\\\\\\n]'"],
    _NUM,
    ["keyword", _k("case class data deriving do else if import in infix infixl infixr instance let module of then type where as hiding qualified mdo proc rec forall family role stock any via newtype")],
    ["type", "\\b[A-Z][A-Za-z0-9_']*\\b"],
    ["bool", _k("True False")],
    ["builtin", _k("putStrLn print map filter foldr foldl show read return")],
    _FUNC,
  ],
  ocaml: [
    ["comment", "\\(\\*[\\s\\S]*?\\*\\)"],
    _CSTR,
    ["string", "'[^'\\\\\\n]'"],
    _NUM,
    ["keyword", _k("and as assert begin class constraint do done downto else end exception external false for fun function functor if in include inherit initializer land lazy let lor lsl lsr lxor match method mod module mutable new object of open or private rec sig struct then to true try type val virtual when while with")],
    ["type", "\\b[A-Z][A-Za-z0-9_']*\\b"],
    ["bool", _k("true false")],
    ["builtin", _k("print_string print_endline failwith invalid_arg raise exit")],
    _FUNC,
  ],
  elixir: hashlike({
    triple: true, decorator: true,
    kw: "alias and case catch cond def defimpl defmodule defp defprotocol defstruct do else end false fn for if import in not or quote raise receive require rescue return super throw true try unquote unless use when with",
    bools: "true false nil",
    builtins: "IO Enum Map List String Kernel puts inspect",
  }),
  erlang: [
    ["comment", "%[^\\n]*"],
    _CSTR,
    _NUM,
    ["keyword", _k("after and andalso band begin bnot bor bsl bsr bxor case catch cond div end fun if let of or orelse query receive rem try when xor")],
    ["bool", _k("true false")],
    _FUNC,
    ["builtin", _k("io lists maps")],
  ],
  clojure: [
    ["comment", ";[^\\n]*"],
    _CSTR,
    ["variable", ":[\\w?!*+\\-/]+"],
    _NUM,
    ["keyword", _k("def defn defmacro defmethod defmulti defonce defprotocol defrecord defstruct deftype fn let letfn if when when-not when-let cond condp case doseq dotimes for loop recur do comment declare ns in-ns refer use require import quote var deref atom swap! reset! future promise delay lazy-seq cons first rest apply map filter reduce")],
    ["bool", _k("true false nil")],
    _FUNC,
    ["builtin", _k("println print str")],
  ],
  lisp: [
    ["comment", ";[^\\n]*"],
    _CSTR,
    _NUM,
    ["keyword", _k("defun defmacro defvar defparameter defconstant let flet labels lambda if when unless cond case progn prog1 prog2 quote quasiquote unquote eval apply funcall loop do dolist dotimes return return-from block tagbody go setf setq incf decf push pop defstruct defclass defmethod defgeneric")],
    ["bool", _k("t nil")],
    _FUNC,
    ["builtin", _k("car cdr cons list append length reverse print format")],
  ],
  fsharp: [
    ["comment", "//[^\\n]*"],
    ["comment", "\\(\\*[\\s\\S]*?\\*\\)"],
    ["string", '"""[\\s\\S]*?"""'],
    _CSTR,
    _NUM,
    ["keyword", _k("abstract and as assert base begin class default delegate do done downcast downto else end exception extern false finally fixed for fun function global if in inherit inline interface lazy let match member module mutable namespace new null of open or override private public rec return sig static struct then to true try type upcast use val void when while with yield")],
    ["type", "\\b[A-Z][A-Za-z0-9_]*\\b"],
    ["bool", _k("true false null")],
    _FUNC,
    ["builtin", _k("printf printfn failwith")],
  ],
  vb: [
    ["comment", "'[^\\n]*"],
    ["comment", "\\bREM\\b[^\\n]*"],
    _CSTR,
    ["number", "&H[0-9a-fA-F]+"],
    _NUM,
    ["keyword", _k("AddHandler AddressOf Alias And AndAlso As Boolean ByRef Byte ByVal Call Case Catch Class Const Continue Declare Default Delegate Dim DirectCast Do Double Each Else ElseIf End EndIf Enum Erase Error Event Exit For Friend Function Get GetType Global GoTo Handles If Implements Imports In Inherits Integer Interface Is IsNot Let Lib Like Long Loop Me Mod Module MustInherit MustOverride MyBase MyClass Namespace New Next Not Nothing Object Of Off On Operator Option Optional Or OrElse Overloads Overridable Overrides ParamArray Partial Private Property Protected Public RaiseEvent ReadOnly ReDim RemoveHandler Resume Return Select Set Shadows Shared Short Single Static Step Stop String Structure Sub SyncLock Then Throw To Try TryCast TypeOf Using Variant Wend When While Widening With WithEvents WriteOnly Xor")],
    ["type", _k("Integer String Boolean Double Object Date Decimal Long Short Byte Char Variant")],
    ["bool", _k("True False Nothing")],
    _FUNC,
    ["builtin", _k("MsgBox InputBox Print Debug Console WriteLine")],
  ],
  pascal: [
    ["comment", "//[^\\n]*"],
    ["comment", "\\{[^$}][^\\n]*\\}"],
    ["comment", "\\(\\*[\\s\\S]*?\\*\\)"],
    ["string", "'([^']|'')*'"],
    ["number", "\\$[0-9a-fA-F]+"],
    _NUM,
    ["keyword", _k("and array as asm begin case class const constructor destructor div do downto else end except exports file finally for function goto if implementation in inherited inline interface is label library mod nil not object of on operator or out packed procedure program property raise record repeat set shl shr string then threadvar to try type unit until uses var while with xor")],
    ["type", _k("Integer String Boolean Char Real Byte Word LongInt Cardinal Pointer TObject")],
    ["bool", _k("True False nil")],
    _FUNC,
    ["builtin", _k("Write WriteLn Read ReadLn")],
  ],
  ada: [
    ["comment", "--[^\\n]*"],
    _CSTR,
    _NUM,
    ["keyword", _k("abort abs abstract accept access aliased all and array at begin body case constant declare delay delta digits do else elsif end entry exception exit for function generic goto if in interface is limited loop mod new not null of or others out overriding package pragma private procedure protected raise range record rem renames requeue return reverse select separate subtype synchronized tagged task terminate then type until use when while with xor")],
    ["type", _k("Integer Float Boolean Character String Natural Positive")],
    ["bool", _k("True False")],
    _FUNC,
    ["builtin", _k("Put_Line Put Get_Line Get")],
  ],
  nim: hashlike({
    triple: true,
    kw: "addr and as asm bind block break case cast concept const continue converter defer discard distinct div do elif else end enum except export finally for from func if import in include inherit interface is isnot iterator let macro method mixin mod nil not notin object of or out proc pure quote raise ref return shl shr static template try tuple type using var when while with without yield",
    types: "int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 float float32 float64 bool char string seq array",
    bools: "true false nil",
    builtins: "echo result",
  }),
  fortran: [
    ["comment", "![^\\n]*"],
    _SSTR, _CSTR,
    ["number", "\\b\\d[\\d.]*(d[+-]?\\d+)?\\b"],
    _NUM,
    ["keyword", _k("program end module subroutine function contains use implicit none integer real double precision complex character logical parameter dimension allocatable allocate deallocate intent optional save data common block equivalence external intrinsic public private pointer target print write read format open close stop call continue cycle exit where elsewhere forall interface operator assignment if then else elseif endif do while select case default return goto")],
    ["bool", "\\.(true|false)\\."],
    ["builtin", _k("print write read")],
  ],
  verilog: [
    ["comment", "//[^\\n]*"],
    ["comment", "/\\*[\\s\\S]*?\\*/"],
    _CSTR,
    ["number", "\\b\\d+'[bhd][0-9a-fA-F_xz?]+\\b"],
    _NUM,
    ["keyword", _k("module endmodule input output inout wire reg logic bit integer int real time realtime parameter localparam assign always initial begin end if else for while repeat forever case endcase casex casez default function endfunction task endtask generate endgenerate genvar posedge negedge edge specify endspecify fork join disable defparam tri supply0 supply1 and or not nand nor xor xnor buf primitive endprimitive table endtable automatic static const ref extern pure virtual extends implements import export package endpackage program endprogram clocking endclocking property endproperty sequence assert assume cover expect before bind constraint random rand randc randcase unique unique0 priority with inside dist foreach return break continue do while typedef struct union enum class endclass new super this null void string byte shortint longint char shortreal chandle event signed unsigned")],
    ["type", _k("wire reg logic bit int integer byte string void")],
    ["decorator", "`[A-Za-z_]\\w*"],
    _FUNC,
  ],
  vhdl: [
    ["comment", "--[^\\n]*"],
    _CSTR,
    _NUM,
    ["keyword", _k("abs access after alias all and architecture array assert assume attribute begin block body buffer bus case component configuration constant context cover disconnect downto else elsif end entity exit fair file for force generate generic group guarded if impure in inertial inout is label library linkage literal loop map mod nand new next nor not null of on open or others out package parameter port postponed procedure process property protected pure range record register reject release rem report restrict rol ror select sequence severity shared signal sla sll sra srl strong subtype then to transport type unaffected units until use variable wait when while with xnor xor")],
    ["type", _k("bit bit_vector std_logic std_ulogic std_logic_vector integer natural positive boolean character string time signed unsigned")],
    ["bool", _k("true false")],
    _FUNC,
  ],
  asm: [
    ["comment", ";[^\\n]*"],
    ["comment", "#[^\\n]*"],
    ["comment", "//[^\\n]*"],
    _CSTR, _SSTR,
    ["number", "\\b[0-9a-fA-F]+[hH]\\b|\\$[0-9a-fA-F]+\\b|%[01]+\\b"],
    _NUM,
    ["keyword", _k("mov push pop lea call ret jmp je jne jz jnz ja jb jae jbe jg jl jge jle jo jno js jns jp jnp jcxz loop nop hlt int iret cli sti cld std pushf popf pusha popa movs stos lods cmps scas add sub mul imul div idiv inc dec neg not and or xor shl shr sal sar rol ror rcl rcr test cmp xchg xadd bswap in out ins outs enter leave bound into arpl lar lsl verr verw sgdt sidt lgdt lidt smsw lmsw lldt ltr clts lock rep repe repz repne repnz pause mfence lfence sfence syscall sysret sysenter sysexit cpuid rdtsc rdmsr wrmsr fxsave fxrstor global extern section text data bss rodata align bits org equ db dw dd dq dt resb resw resd resq times macro endmacro struc endstruc proc endp segment ends assume end model stack code option include includelib public extrn ldr str ldm stm b bl bx blx svc mrs msr adr nop wfi ldc stc mla mul mls smull umull smlal umlal movt movw movs bfc bfi ubfx sbfx rbit rev rev16 clz")],
    ["type", "\\b(rax|rbx|rcx|rdx|rsi|rdi|rsp|rbp|r8|r9|r10|r11|r12|r13|r14|r15|eax|ebx|ecx|edx|esi|edi|esp|ebp|xmm[0-9]+|ymm[0-9]+|r0|r1|r2|r3|r4|r5|r6|r7|sp|lr|pc)\\b"],
    ["decorator", "^\\s*\\.[A-Za-z]+"],
    ["function", "^[A-Za-z_.][\\w.$]*:"],
  ],
  dot: [
    ["comment", "//[^\\n]*"],
    ["comment", "#[^\\n]*"],
    _CSTR,
    ["keyword", _k("strict digraph graph subgraph node edge")],
    ["attr", "[A-Za-z_][\\w]*(?=\\s*=)"],
    ["bool", _k("true false")],
  ],
  po: [
    ["comment", "#[^\\n]*"],
    _CSTR,
    ["keyword", _k("msgid msgstr msgid_plural msgctxt domain")],
  ],
  proto: clike({
    kw: "syntax package import option message enum service rpc returns stream optional repeated required oneof map reserved to max extend extensions",
    types: "double float int32 int64 uint32 uint64 sint32 sint64 fixed32 fixed64 sfixed32 sfixed64 bool string bytes",
    bools: "true false",
  }),
  solidity: clike({
    kw: "pragma solidity contract interface library is abstract constructor modifier function returns return var if else for while do break continue assembly using struct enum mapping address payable public private internal external pure view virtual override immutable constant event emit error revert require assert new delete type using for",
    types: "uint uint8 uint16 uint32 uint64 uint128 uint256 int int8 int16 int32 int64 int128 int256 bytes bytes1 bytes4 bytes8 bytes16 bytes32 byte address bool string mapping",
    bools: "true false",
    builtins: "require assert revert msg block tx gasleft",
  }),
  puppet: hashlike({
    vars: "\\$[A-Za-z_][\\w:]*|\\${[^}]*}",
    kw: "class define node if else elsif unless case default in and or not true false undef application consumes produces environment site",
    bools: "true false undef",
    types: "file package service exec notify cron user group mount yumrepo schedule stage",
    builtins: "include require contain notice fail",
  }),
  matlab: [
    ["comment", "%[^\\n]*"],
    _CSTR, _SSTR,
    _NUM,
    ["keyword", _k("break case catch classdef continue else elseif end for function global if otherwise parfor persistent return spmd switch try while methods properties events enumeration")],
    ["bool", _k("true false")],
    _FUNC,
    ["builtin", _k("disp fprintf plot length size zeros ones eye rand linspace")],
  ],
};

// XML shares the HTML tag/attribute rules (elements, quoted attrs, comments).
HL_RULES.xml = HL_RULES.html;

// Build a compiled highlighter per language (cached).
const _hlCache = {};
function getHighlighter(lang) {
  if (lang === "text" || !HL_RULES[lang]) return null;
  if (_hlCache[lang]) return _hlCache[lang];
  const rules = HL_RULES[lang];
  const parts = rules.map((r, i) => `(?<g${i}>${r[1]})`);
  // "m" so ^-anchored rules (yaml keys, markdown headings, make targets,
  // asm labels, ...) match on every line, not just the first. Case-insensitive
  // languages (SQL, batch, VB, ...) add "i" via HL_FLAGS.
  let flags = HL_FLAGS[lang] || "g";
  if (!flags.includes("m")) flags += "m";
  const re = new RegExp(parts.join("|"), flags);
  const fn = function (code) {
    let out = "", last = 0, m;
    re.lastIndex = 0;
    while ((m = re.exec(code))) {
      if (m.index > last) out += escHtml(code.slice(last, m.index));
      let cls = null;
      for (let i = 0; i < rules.length; i++) {
        if (m.groups[`g${i}`] != null) { cls = rules[i][0]; break; }
      }
      const text = m[0];
      if (cls) out += `<span class="tok-${cls}">${escHtml(text)}</span>`;
      else out += escHtml(text);
      last = m.index + text.length;
      if (text.length === 0) re.lastIndex++;
    }
    out += escHtml(code.slice(last));
    return out;
  };
  _hlCache[lang] = fn;
  return fn;
}

function renderHighlight() {
  if (!editorState) return;
  const code = elEditorInput.value;
  const hl = getHighlighter(editorState.lang);
  if (hl && code.length <= 300000) {
    elEditorCode.innerHTML = hl(code) + "\n";
  } else {
    elEditorCode.textContent = code;
  }
  updateGutter();
}

function updateGutter() {
  const n = elEditorInput.value.split("\n").length;
  let s = "";
  for (let i = 1; i <= n; i++) s += i + "\n";
  elEditorGutter.textContent = s;
}

function scheduleHighlight() {
  if (hlRaf) return;
  hlRaf = requestAnimationFrame(() => {
    hlRaf = null;
    renderHighlight();
  });
}

async function openCodeEditor(path) {
  let content = "", exists = false;
  try {
    const r = await fetch("/api/file?path=" + encodeURIComponent(path));
    if (r.ok) { const j = await r.json(); content = j.content || ""; exists = !!j.exists; }
  } catch (e) { /* offline: open empty */ }
  editorState = { path, lang: langFromPath(path), dirty: false, exists, unit: "    " };
  if (EC) editorState.unit = EC.detectIndent(content, editorState.lang).unit;
  elEditor.removeAttribute("hidden");
  elEditorFile.textContent = (path.split("/").pop() || path) + (exists ? "" : "  (new)");
  elEditorLang.textContent = editorState.lang;
  elEditorStatus.textContent = "";
  elEditorInput.value = content;
  elEditorInput.setSelectionRange(0, 0);   // assigning .value parks the caret at the end
  renderHighlight();
  elEditorInput.scrollTop = 0;
  elEditorGutter.scrollTop = 0;
  const appEl = document.getElementById("app");
  // Drop any lingering pty class so the terminal view can't sit underneath the
  // editor (which would look like overlapping panels).
  appEl.classList.remove("pty");
  appEl.classList.add("editing");
  elFind.hidden = true;
  findOpen = false;
  initEditorFontSize();
  edAfterChange();
  setTimeout(() => elEditorInput.focus({ preventScroll: true }), 60);
}

function closeCodeEditor() {
  if (editorState && editorState.dirty) {
    if (!confirm("Discard unsaved changes to " + editorState.path + "?")) return;
  }
  document.getElementById("app").classList.remove("editing");
  elEditor.setAttribute("hidden", "");
  elFind.hidden = true;
  findOpen = false;
  elEdCur.style.display = "none";
  editorState = null;
  updateView();
  elCmd.focus();
}

async function saveCodeEditor() {
  if (!editorState) return;
  elEditorStatus.textContent = "Saving…";
  try {
    const r = await fetch("/api/file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: editorState.path, content: elEditorInput.value }),
    });
    const j = await r.json();
    if (j.ok) {
      editorState.dirty = false;
      editorState.exists = true;
      elEditorStatus.textContent = "Saved " + new Date().toLocaleTimeString();
    } else {
      elEditorStatus.textContent = "Save failed: " + (j.error || "unknown");
    }
  } catch (e) {
    elEditorStatus.textContent = "Save failed: " + e;
  }
}

// ---------------------------------------------------------------------------
// Editor behaviour. The pure text logic lives in editor-core.js (unit-tested in
// Node); this block applies its edits to the textarea and owns the UI around
// it: status bar, font size, find/replace, symbol bar.
// ---------------------------------------------------------------------------
const EC = window.EditorCore || null;
const $ed = (id) => document.getElementById(id);
const elEdCur = $ed("editor-curline");
const elEdPos = $ed("es-pos");
const elEdLines = $ed("es-lines");
const elEdIndent = $ed("es-indent");
const elFsDown = $ed("es-fs-down");
const elFsUp = $ed("es-fs-up");
const elFind = $ed("editor-find");
const elFq = $ed("ef-query");
const elFr = $ed("ef-repl");
const elFcount = $ed("ef-count");
const elFreplRow = $ed("ef-repl-row");

const ED_FS_KEY = "rconsole.editor.fontsize";
// iOS zooms the page when a text field below 16px gets focus, so never go smaller there.
const ED_IS_IOS = /iP(hone|ad|od)/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const ED_FS_MIN = ED_IS_IOS ? 16 : 9;
const ED_FS_MAX = 32;
const ED_UNITS = ["  ", "    ", "\t"];
const ED_SYMBOLS = "{}()[]<>\"'`;:=/\\|&!_-+*#$%?~";
let edApplying = false;       // true while we drive the browser's own editing command
let edCurLine = 1;
let edStatusRaf = 0;

function edOpts() {
  return { lang: editorState ? editorState.lang : "text", unit: editorState ? editorState.unit : "    " };
}

function markEditorDirty() {
  if (editorState) {
    editorState.dirty = true;
    elEditorStatus.textContent = "● unsaved";
  }
}

function edMetrics() {
  const cs = getComputedStyle(elEditorInput);
  const fs = parseFloat(cs.fontSize) || 14;
  return {
    fs,
    lh: parseFloat(cs.lineHeight) || Math.round(fs * 1.5),
    cw: fs * 0.6,
    padT: parseFloat(cs.paddingTop) || 8,
    padL: parseFloat(cs.paddingLeft) || 12,
  };
}

/** Scroll so `pos` is visible (centred when `center`, e.g. after find / go-to-line). */
function revealPos(pos, center) {
  if (!EC) return;
  const ta = elEditorInput, m = edMetrics();
  const lc = EC.lineCol(ta.value, pos);
  const top = m.padT + (lc.line - 1) * m.lh;
  const vh = ta.clientHeight;
  if (center) {
    if (top < ta.scrollTop || top + m.lh > ta.scrollTop + vh) ta.scrollTop = Math.max(0, top - vh / 2);
  } else if (top < ta.scrollTop + m.lh) {
    ta.scrollTop = Math.max(0, top - m.lh);
  } else if (top + m.lh * 2 > ta.scrollTop + vh) {
    ta.scrollTop = top + m.lh * 2 - vh;
  }
  const prefix = ta.value.slice(EC.lineStartOf(ta.value, pos), pos);
  let vis = 0;
  for (const ch of prefix) vis = ch === "\t" ? vis - (vis % 4) + 4 : vis + 1;
  const x = m.padL + vis * m.cw;
  if (x < ta.scrollLeft + 20) ta.scrollLeft = Math.max(0, x - 40);
  else if (x > ta.scrollLeft + ta.clientWidth - 30) ta.scrollLeft = x - ta.clientWidth + 80;
}

function updateCurLine(recompute) {
  if (!EC || !editorState) { elEdCur.style.display = "none"; return; }
  const ta = elEditorInput;
  if (recompute !== false) {
    if (ta.selectionStart !== ta.selectionEnd && ta.value.slice(ta.selectionStart, ta.selectionEnd).includes("\n")) {
      elEdCur.style.display = "none";
      return;
    }
    edCurLine = EC.lineCol(ta.value, ta.selectionStart).line;
  }
  const m = edMetrics();
  elEdCur.style.display = "block";
  elEdCur.style.height = m.lh + "px";
  elEdCur.style.top = (m.padT + (edCurLine - 1) * m.lh - ta.scrollTop) + "px";
}

function edIndentLabel() {
  if (!editorState) return "";
  return editorState.unit === "\t" ? "Tabs" : "Spaces: " + editorState.unit.length;
}

function updateStatusBar() {
  if (edStatusRaf) return;
  edStatusRaf = requestAnimationFrame(() => {
    edStatusRaf = 0;
    if (!EC || !editorState) return;
    const ta = elEditorInput, v = ta.value;
    const lc = EC.lineCol(v, ta.selectionStart);
    const n = ta.selectionEnd - ta.selectionStart;
    elEdPos.textContent = "Ln " + lc.line + ", Col " + lc.col + (n ? " (" + n + " sel)" : "");
    let lines = 1, i = -1;
    while ((i = v.indexOf("\n", i + 1)) !== -1) lines++;
    elEdLines.textContent = lines + (lines === 1 ? " line" : " lines");
    elEdIndent.textContent = edIndentLabel();
  });
}

function edAfterChange() {
  updateCurLine(true);
  updateStatusBar();
}

/**
 * Apply an edit from editor-core through the browser's editing stack so that
 * undo / redo (Ctrl+Z, Ctrl+Y, the symbol-bar buttons) keep working. If the
 * browser refuses execCommand we fall back to writing the value directly.
 */
function applyEdit(ed) {
  if (!ed) return false;
  const ta = elEditorInput;
  if (ed.insert === "" && ed.from === ed.to) {          // caret move only
    ta.setSelectionRange(ed.selStart, ed.selEnd);
  } else {
    const old = ta.value;
    const expected = old.slice(0, ed.from) + ed.insert + old.slice(ed.to);
    if (document.activeElement !== ta) ta.focus({ preventScroll: true });
    ta.setSelectionRange(ed.from, ed.to);
    let done = false;
    edApplying = true;
    try {
      done = ed.insert === "" ? document.execCommand("delete") : document.execCommand("insertText", false, ed.insert);
    } catch (e) { done = false; }
    edApplying = false;
    if (!done || ta.value !== expected) {
      ta.value = expected;                              // no native undo for this one step
      markEditorDirty();
      scheduleHighlight();
    }
    ta.setSelectionRange(ed.selStart, ed.selEnd);
  }
  revealPos(ed.selEnd, false);
  edAfterChange();
  return true;
}

function edTypeChar(ch) {
  const ta = elEditorInput, v = ta.value, s = ta.selectionStart, en = ta.selectionEnd;
  const ed = EC && EC.typeChar(v, s, en, ch, edOpts());
  applyEdit(ed || { from: s, to: en, insert: ch, selStart: s + ch.length, selEnd: s + ch.length });
}

// ---- font size -------------------------------------------------------------
let edFs = 14;   // current editor font size in px

function setEditorFontSize(px, persist) {
  px = Math.max(ED_FS_MIN, Math.min(ED_FS_MAX, Math.round(px)));
  edFs = px;
  elEditor.style.setProperty("--editor-fs", px + "px");
  elFsDown.disabled = px <= ED_FS_MIN;
  elFsUp.disabled = px >= ED_FS_MAX;
  elFsDown.title = "Smaller text (now " + px + " px)";
  elFsUp.title = "Larger text (now " + px + " px)";
  if (persist) { try { localStorage.setItem(ED_FS_KEY, String(px)); } catch (e) { /* private mode */ } }
  if (editorState) { updateCurLine(false); revealPos(elEditorInput.selectionEnd, false); }
}

function initEditorFontSize() {
  let saved = NaN;
  try { saved = parseInt(localStorage.getItem(ED_FS_KEY) || "", 10); } catch (e) { /* ignore */ }
  if (saved >= ED_FS_MIN && saved <= ED_FS_MAX) {
    setEditorFontSize(saved, false);
  } else {
    edFs = Math.max(ED_FS_MIN, Math.min(ED_FS_MAX, Math.round(edMetrics().fs)));   // the CSS default (16px on touch)
    setEditorFontSize(edFs, false);
  }
}
elFsDown.addEventListener("click", () => setEditorFontSize(edFs - 1, true));
elFsUp.addEventListener("click", () => setEditorFontSize(edFs + 1, true));

// ---- go to line ------------------------------------------------------------
function gotoLine() {
  if (!EC || !editorState) return;
  const raw = prompt("Go to line (or line:column):", "");
  const m = raw && /^\s*(\d+)(?:\s*[:,]\s*(\d+))?\s*$/.exec(raw);
  if (!m) return;
  const ta = elEditorInput, v = ta.value;
  const idx = EC.lineStartIndex(v, parseInt(m[1], 10));
  const pos = Math.min(idx + (m[2] ? parseInt(m[2], 10) - 1 : 0), EC.lineEndOf(v, idx));
  ta.focus({ preventScroll: true });
  ta.setSelectionRange(pos, pos);
  revealPos(pos, true);
  edAfterChange();
}
elEdPos.addEventListener("click", gotoLine);

elEdIndent.addEventListener("click", () => {
  if (!editorState) return;
  editorState.unit = ED_UNITS[(ED_UNITS.indexOf(editorState.unit) + 1) % ED_UNITS.length];
  elEdIndent.textContent = edIndentLabel();
  elEditorInput.focus({ preventScroll: true });
});

// ---- find / replace --------------------------------------------------------
const findOpts = { caseSensitive: false, wholeWord: false, regex: false };
let findMatches = [];
let findOpen = false;
let findTimer = null;

function updateFindCount(note) {
  elFcount.classList.remove("bad");
  if (note) { elFcount.textContent = note; return; }
  if (!elFq.value) { elFcount.textContent = ""; return; }
  if (!findMatches.length) { elFcount.textContent = "No results"; return; }
  const at = findMatches.findIndex((m) => m.index === elEditorInput.selectionStart);
  elFcount.textContent = at >= 0 ? (at + 1) + " of " + findMatches.length : findMatches.length + " found";
}

function runFind(jump) {
  if (!EC) return;
  const r = EC.findAll(elEditorInput.value, elFq.value, findOpts);
  findMatches = r.matches;
  if (r.error) { elFcount.textContent = r.error; elFcount.classList.add("bad"); return; }
  if (jump && findMatches.length) selectMatch(EC.nextIndex(findMatches, elEditorInput.selectionStart, false));
  else updateFindCount();
}

function selectMatch(i) {
  const m = findMatches[i];
  if (!m) return;
  const ta = elEditorInput;
  ta.setSelectionRange(m.index, m.index + m.length);
  revealPos(m.index, true);
  updateFindCount();
  edAfterChange();
}

function findStep(back) {
  if (!findMatches.length) runFind(false);
  if (!findMatches.length) return;
  const ta = elEditorInput;
  selectMatch(EC.nextIndex(findMatches, back ? ta.selectionStart : ta.selectionEnd, back));
}

function openFind(withReplace) {
  if (!EC || !editorState) return;
  const ta = elEditorInput;
  const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
  if (sel && !sel.includes("\n") && sel.length <= 200) elFq.value = sel;
  elFind.hidden = false;
  findOpen = true;
  if (withReplace) elFreplRow.hidden = false;
  elFq.focus();
  elFq.select();
  runFind(true);
}

function closeFind() {
  elFind.hidden = true;
  findOpen = false;
  if (editorState) elEditorInput.focus({ preventScroll: true });
}

function replaceCurrent() {
  const ta = elEditorInput;
  const i = findMatches.findIndex((m) => m.index === ta.selectionStart && m.index + m.length === ta.selectionEnd);
  if (i < 0) { findStep(false); return; }
  const m = findMatches[i];
  const rep = EC.replacementFor(m, elFq.value, elFr.value, findOpts);
  applyEdit({ from: m.index, to: m.index + m.length, insert: rep, selStart: m.index + rep.length, selEnd: m.index + rep.length });
  runFind(true);
}

function replaceEverything() {
  if (!EC || !elFq.value) return;
  const ta = elEditorInput;
  const r = EC.replaceAll(ta.value, elFq.value, elFr.value, findOpts);
  if (!r.count) { updateFindCount(); return; }
  const caret = Math.min(ta.selectionStart, r.text.length);
  applyEdit({ from: 0, to: ta.value.length, insert: r.text, selStart: caret, selEnd: caret });
  runFind(false);
  updateFindCount("Replaced " + r.count);
}

function toggleFindOpt(id, key) {
  findOpts[key] = !findOpts[key];
  $ed(id).classList.toggle("on", findOpts[key]);
  runFind(true);
}

elFq.addEventListener("input", () => runFind(true));
elFq.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); findStep(e.shiftKey); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeFind(); }
});
elFr.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); replaceCurrent(); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeFind(); }
});
$ed("ef-next").addEventListener("click", () => findStep(false));
$ed("ef-prev").addEventListener("click", () => findStep(true));
$ed("ef-close").addEventListener("click", closeFind);
$ed("ef-case").addEventListener("click", () => toggleFindOpt("ef-case", "caseSensitive"));
$ed("ef-word").addEventListener("click", () => toggleFindOpt("ef-word", "wholeWord"));
$ed("ef-regex").addEventListener("click", () => toggleFindOpt("ef-regex", "regex"));
$ed("ef-replace").addEventListener("click", replaceCurrent);
$ed("ef-all").addEventListener("click", replaceEverything);
$ed("ef-toggle").addEventListener("click", () => {
  elFreplRow.hidden = !elFreplRow.hidden;
  $ed("ef-toggle").innerHTML = elFreplRow.hidden ? "&#9656;" : "&#9662;";
});
$ed("editor-find-btn").addEventListener("click", () => openFind(false));

// ---- symbol bar (touch) ----------------------------------------------------
(function buildSymbolBar() {
  const host = $ed("ek-syms");
  for (const ch of ED_SYMBOLS) {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.ins = ch;
    b.textContent = ch;
    host.appendChild(b);
  }
})();
$ed("editor-keys").addEventListener("mousedown", (e) => {
  if (e.target.closest("button")) e.preventDefault();      // keep the textarea focused (and the soft keyboard up)
});
$ed("editor-keys").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || !editorState || !EC) return;
  const ta = elEditorInput;
  if (b.dataset.act !== "find") ta.focus({ preventScroll: true });
  if (b.dataset.ins !== undefined) { edTypeChar(b.dataset.ins); return; }
  const v = ta.value, s = ta.selectionStart, en = ta.selectionEnd, o = edOpts();
  switch (b.dataset.act) {
    case "tab": applyEdit(EC.tab(v, s, en, false, o)); break;
    case "outdent": applyEdit(EC.tab(v, s, en, true, o)); break;
    case "undo": try { document.execCommand("undo"); } catch (err) { /* unsupported */ } edAfterChange(); break;
    case "redo": try { document.execCommand("redo"); } catch (err) { /* unsupported */ } edAfterChange(); break;
    case "comment": applyEdit(EC.toggleComment(v, s, en, o)); break;
    case "find": openFind(false); break;
    default: break;
  }
});

// ---- textarea events -------------------------------------------------------
elEditorInput.addEventListener("beforeinput", (e) => {
  if (!EC || !editorState || edApplying || !e.cancelable || e.isComposing) return;
  const ta = elEditorInput, v = ta.value, s = ta.selectionStart, en = ta.selectionEnd, o = edOpts();
  let ed = null;
  if (e.inputType === "insertText") ed = e.data && e.data.length === 1 ? EC.typeChar(v, s, en, e.data, o) : null;
  else if (e.inputType === "insertLineBreak" || e.inputType === "insertParagraph") ed = EC.enter(v, s, en, o);
  else if (e.inputType === "deleteContentBackward") ed = EC.backspace(v, s, en, o);
  if (ed) { e.preventDefault(); applyEdit(ed); }
});

elEditorInput.addEventListener("input", () => {
  markEditorDirty();
  scheduleHighlight();
  edAfterChange();
  if (findOpen) { clearTimeout(findTimer); findTimer = setTimeout(() => runFind(false), 150); }
});

elEditorInput.addEventListener("scroll", () => {
  elEditorPre.scrollTop = elEditorInput.scrollTop;
  elEditorPre.scrollLeft = elEditorInput.scrollLeft;
  elEditorGutter.scrollTop = elEditorInput.scrollTop;
  updateCurLine(false);
});

for (const ev of ["keyup", "mouseup", "touchend", "focus"]) {
  elEditorInput.addEventListener(ev, () => { if (editorState) edAfterChange(); });
}
document.addEventListener("selectionchange", () => {
  if (editorState && document.activeElement === elEditorInput) edAfterChange();
});

elEditorInput.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  const k = e.key;
  const lower = k.length === 1 ? k.toLowerCase() : k;
  if (mod && !e.altKey && lower === "s") { e.preventDefault(); saveCodeEditor(); return; }
  if (k === "Escape") { e.preventDefault(); if (findOpen) closeFind(); else closeCodeEditor(); return; }
  const ta = elEditorInput;
  if (!EC) {                                              // editor-core failed to load: minimal Tab support
    if (k === "Tab") {
      e.preventDefault();
      ta.setRangeText("    ", ta.selectionStart, ta.selectionEnd, "end");
      markEditorDirty();
      scheduleHighlight();
    }
    return;
  }
  const v = ta.value, s = ta.selectionStart, en = ta.selectionEnd, o = edOpts();
  const run = (ed) => { e.preventDefault(); if (ed) applyEdit(ed); };

  if (k === "Tab" && !mod && !e.altKey) { run(EC.tab(v, s, en, e.shiftKey, o)); return; }
  if (k === "Home" && !mod && !e.altKey) {
    const backward = ta.selectionDirection === "backward";
    const caret = s === en || backward ? s : en;
    const target = EC.homeTarget(v, caret);
    if (target === caret) return;
    e.preventDefault();
    if (e.shiftKey) {
      const anchor = s === en ? s : (backward ? en : s);
      ta.setSelectionRange(Math.min(anchor, target), Math.max(anchor, target), target < anchor ? "backward" : "forward");
    } else {
      ta.setSelectionRange(target, target);
    }
    edAfterChange();
    return;
  }
  if (e.altKey && !mod && (k === "ArrowUp" || k === "ArrowDown")) {
    const dir = k === "ArrowUp" ? -1 : 1;
    run(e.shiftKey ? EC.duplicateLines(v, s, en, dir) : EC.moveLines(v, s, en, dir));
    return;
  }
  if (k === "F3") { e.preventDefault(); if (elFq.value) findStep(e.shiftKey); else openFind(false); return; }
  if (!mod || e.altKey) return;
  if (k === "/") { run(EC.toggleComment(v, s, en, o)); return; }
  if (k === "]") { run(EC.indentLines(v, s, en, 1, o)); return; }
  if (k === "[") { run(EC.indentLines(v, s, en, -1, o)); return; }
  if (k === "Enter") { run(EC.insertLine(v, s, en, !e.shiftKey, o)); return; }
  if (lower === "k" && e.shiftKey) { run(EC.deleteLines(v, s, en)); return; }
  if (lower === "l" && !e.shiftKey) { run(EC.selectLine(v, s, en)); return; }
  if (lower === "j" && !e.shiftKey) { run(EC.joinLines(v, s, en)); return; }
  if (lower === "f" && !e.shiftKey) { e.preventDefault(); openFind(false); return; }
  if (lower === "h" && !e.shiftKey) { e.preventDefault(); openFind(true); return; }
  if (lower === "g" && !e.shiftKey) { e.preventDefault(); gotoLine(); }
});

window.addEventListener("beforeunload", (e) => {
  if (editorState && editorState.dirty) { e.preventDefault(); e.returnValue = ""; }
});
elEditorGutter.addEventListener("click", (e) => {
  const rect = elEditorGutter.getBoundingClientRect();
  const lh = parseFloat(getComputedStyle(elEditorInput).lineHeight) || 21;
  const line = Math.floor((e.clientY - rect.top + elEditorGutter.scrollTop) / lh) + 1;
  const lines = elEditorInput.value.split("\n");
  if (line < 1 || line > lines.length) return;
  let pos = 0;
  for (let i = 0; i < line - 1; i++) pos += lines[i].length + 1;
  elEditorInput.focus();
  elEditorInput.selectionStart = elEditorInput.selectionEnd = pos;
});
document.getElementById("editor-save").addEventListener("click", saveCodeEditor);
document.getElementById("editor-close").addEventListener("click", closeCodeEditor);

// ---------------------------------------------------------------------------
// Visual file navigator (drawer). Browse directories, open files in the
// editor, create/rename/delete files and folders — all through /api/dir and
// /api/fs, so it works with touch + soft keyboard where a PTY is fragile.
// ---------------------------------------------------------------------------
const elFiles = document.getElementById("files");
const elFilesCrumb = document.getElementById("files-crumb");
const elFilesPath = document.getElementById("files-path");
const elFilesList = document.getElementById("files-list");
const elFilesStatus = document.getElementById("files-status");
const filesState = { open: false, cwd: null, parent: null, entries: [], creating: null, renaming: null,
                     uploading: false, maxUploadMb: 64 };

const FILE_ICONS = {
  "🖼": ["png", "jpg", "jpeg", "gif", "webp", "ico", "bmp", "tif", "tiff", "heic", "avif", "svg"],
  "🎵": ["mp3", "wav", "ogg", "oga", "flac", "aac", "m4a", "opus", "mid", "midi"],
  "🎬": ["mp4", "mkv", "avi", "mov", "webm", "m4v", "ogv", "flv", "wmv", "mpg", "mpeg"],
  "📦": ["zip", "tar", "gz", "tgz", "bz2", "tbz2", "xz", "txz", "7z", "rar", "zst", "lz4", "cab", "deb", "rpm", "apk", "jar", "war", "ear"],
  "📕": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "epub"],
  "🔤": ["ttf", "otf", "woff", "woff2", "eot"],
};
function fileIcon(name, isDir) {
  if (isDir) return "📁";
  const ext = (name.split(".").pop() || "").toLowerCase();
  for (const icon of Object.keys(FILE_ICONS)) {
    if (FILE_ICONS[icon].includes(ext)) return icon;
  }
  return langFromPath(name) !== "text" ? "📝" : "📄";
}
function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}
function joinPath(dir, name) {
  const sep = (dir || "").includes("\\") ? "\\" : "/";
  return (dir || "").replace(/[/\\]+$/, "") + sep + name;
}
function parentDir(p) {
  const i = Math.max((p || "").lastIndexOf("/"), (p || "").lastIndexOf("\\"));
  if (i <= 0) return p;
  return p.slice(0, i);
}
function pathSegments(p) {
  const sep = (p || "").includes("\\") ? "\\" : "/";
  const parts = (p || "").split(sep);
  const segs = [];
  let acc = "";
  parts.forEach((part, i) => {
    if (i === 0 && part === "") { segs.push({ name: sep, path: sep }); return; }
    acc = acc ? acc + sep + part : part;
    segs.push({ name: part === "" ? sep : part, path: acc });
  });
  return segs;
}
async function fsApi(action, payload) {
  try {
    const r = await fetch("/api/fs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ action }, payload)),
    });
    return await r.json();
  } catch (e) { return { ok: false, error: String(e) }; }
}
async function loadDir(path) {
  elFilesStatus.textContent = "Loading…";
  try {
    const r = await fetch("/api/dir?path=" + encodeURIComponent(path || ""));
    const j = await r.json();
    if (!r.ok || j.error) {
      elFilesStatus.textContent = "Error: " + (j.error || ("HTTP " + r.status));
      return;
    }
    const prevCwd = filesState.cwd;
    filesState.cwd = j.path;
    filesState.parent = j.parent;
    filesState.entries = j.entries || [];
    if (j.max_upload_mb) filesState.maxUploadMb = j.max_upload_mb;
    if (j.archives) filesState.archives = j.archives;
    if (j.path !== prevCwd) { filesState.selected = new Set(); filesState.anchor = null; }
    else filesState.selected = new Set(FM.pruneSelection(filesOrder(), [...filesState.selected]));
    filesState.creating = null;
    filesState.renaming = null;
    elFilesStatus.textContent = "";
    if (!filesState.tipShown) {
      filesState.tipShown = true;
      elFilesStatus.textContent = document.body.classList.contains("touch")
        ? "Tip: long-press a row to select it, then drag to select more."
        : "Tip: click the blank space of a row to select it; drag to select many.";
    }
    renderFiles();
  } catch (e) { elFilesStatus.textContent = "Error: " + e; }
}
function fileRow(e) {
  const row = document.createElement("div");
  row.className = "files-row";
  row.dataset.dir = e.is_dir ? "1" : "0";
  row.dataset.path = e.path;
  row.setAttribute("role", "option");
  row.setAttribute("aria-selected", "false");
  const open = document.createElement("button");
  open.type = "button";
  open.className = "files-name";
  open.title = e.path;
  const ic = document.createElement("span");
  ic.className = "files-icon";
  ic.textContent = fileIcon(e.name, e.is_dir);
  open.appendChild(ic);
  const nm = document.createElement("span");
  nm.className = "files-label";
  nm.textContent = e.name + (e.is_dir ? "/" : "");
  open.appendChild(nm);
  row.appendChild(open);                      // opening / selecting is handled by the list's click handler
  const fill = document.createElement("span");
  fill.className = "files-fill";
  row.appendChild(fill);
  const meta = document.createElement("span");
  meta.className = "files-meta";
  meta.textContent = e.is_dir ? "—" : fmtSize(e.size);
  row.appendChild(meta);
  const dl = document.createElement("button");
  dl.type = "button";
  dl.className = "files-act files-dl";
  dl.textContent = "⬇";
  dl.title = e.is_dir ? "Download folder as .zip" : "Download";
  dl.onclick = (ev) => { ev.stopPropagation(); downloadEntry(e); };
  row.appendChild(dl);
  const rn = document.createElement("button");
  rn.type = "button";
  rn.className = "files-act";
  rn.textContent = "✎";
  rn.title = "Rename";
  rn.onclick = (ev) => { ev.stopPropagation(); filesState.renaming = e.path; renderFiles(); };
  row.appendChild(rn);
  const del = document.createElement("button");
  del.type = "button";
  del.className = "files-act files-del";
  del.textContent = "🗑";
  del.title = "Delete";
  del.onclick = (ev) => { ev.stopPropagation(); deleteEntry(e); };
  row.appendChild(del);
  return row;
}
function inlineRow(initial, placeholder, onCommit) {
  const row = document.createElement("div");
  row.className = "files-row files-inline";
  const inp = document.createElement("input");
  inp.type = "text";
  inp.value = initial || "";
  inp.placeholder = placeholder || "name";
  inp.setAttribute("autocomplete", "off");
  inp.setAttribute("autocorrect", "off");
  inp.setAttribute("autocapitalize", "off");
  inp.setAttribute("spellcheck", "false");
  const commit = () => {
    const v = inp.value.trim();
    filesState.creating = null;
    filesState.renaming = null;
    if (v) onCommit(v);
    else renderFiles();
  };
  const ok = document.createElement("button");
  ok.type = "button";
  ok.textContent = "✓";
  ok.title = "Confirm";
  ok.onclick = commit;
  inp.onkeydown = (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); commit(); }
    else if (ev.key === "Escape") {
      ev.stopPropagation();
      filesState.creating = null;
      filesState.renaming = null;
      renderFiles();
    }
  };
  row.appendChild(inp);
  row.appendChild(ok);
  setTimeout(() => { inp.focus(); if (initial) inp.select(); }, 30);
  return row;
}
function renderFiles() {
  elFilesCrumb.innerHTML = "";
  pathSegments(filesState.cwd || "").forEach((s, i, arr) => {
    if (i > 0) elFilesCrumb.appendChild(document.createTextNode(" / "));
    const b = document.createElement("button");
    b.type = "button";
    b.className = "crumb";
    b.textContent = s.name;
    b.title = s.path;
    if (i === arr.length - 1) b.disabled = true;
    else b.onclick = () => loadDir(s.path);
    elFilesCrumb.appendChild(b);
  });
  if (document.activeElement !== elFilesPath) elFilesPath.value = filesState.cwd || "";
  elFilesList.innerHTML = "";
  if (filesState.creating) {
    const kind = filesState.creating;
    elFilesList.appendChild(inlineRow("", kind === "dir" ? "folder name" : "file name", (name) => {
      createEntry(kind, name);
    }));
  }
  if (!filesState.entries.length && !filesState.creating) {
    const d = document.createElement("div");
    d.className = "files-empty";
    d.textContent = "(empty folder)";
    elFilesList.appendChild(d);
  }
  filesState.entries.forEach((e) => {
    if (filesState.renaming === e.path) {
      elFilesList.appendChild(inlineRow(e.name, "new name", (name) => renameEntry(e, name)));
    } else {
      elFilesList.appendChild(fileRow(e));
    }
  });
  applySelectionUI();
}
async function createEntry(kind, name) {
  if (/[/\\]/.test(name)) {
    elFilesStatus.textContent = "Name can't contain path separators.";
    renderFiles();
    return;
  }
  elFilesStatus.textContent = "Creating…";
  const r = await fsApi(kind === "dir" ? "mkdir" : "touch", { path: joinPath(filesState.cwd, name) });
  if (!r.ok) {
    elFilesStatus.textContent = "Create failed: " + (r.error || "unknown");
    renderFiles();
    return;
  }
  if (kind === "file") {
    openCodeEditor(r.path);
    hideFiles();
    return;
  }
  loadDir(filesState.cwd);
}
async function renameEntry(e, name) {
  if (name === e.name) { renderFiles(); return; }
  if (/[/\\]/.test(name)) {
    elFilesStatus.textContent = "Name can't contain path separators.";
    renderFiles();
    return;
  }
  elFilesStatus.textContent = "Renaming…";
  const dst = joinPath(parentDir(e.path), name);
  const r = await fsApi("rename", { src: e.path, dst });
  if (!r.ok) {
    elFilesStatus.textContent = "Rename failed: " + (r.error || "unknown");
    renderFiles();
    return;
  }
  // Keep the editor pointed at the file if it was open.
  if (editorState && editorState.path === e.path) {
    editorState.path = r.path;
    elEditorFile.textContent = name;
  }
  loadDir(filesState.cwd);
}
// ---- download: a file as-is, a folder as a streamed .zip (the server builds it on the fly)
function downloadEntry(e) {
  const a = document.createElement("a");
  a.href = "/api/download?path=" + encodeURIComponent(e.path);
  a.download = "";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 1000);
  elFilesStatus.textContent = e.is_dir ? "Zipping " + e.name + " — the download starts shortly…"
                                       : "Downloading " + e.name + "…";
}

// ---- upload: files or a whole folder, one request per file so progress and errors are per file
const UPLOAD_PARALLEL = 3;
function uploadOne(file, dir, rel, overwrite, onProgress) {
  return new Promise((resolve) => {
    const fd = new FormData();
    fd.append("dir", dir);
    fd.append("rel", rel);
    if (overwrite) fd.append("overwrite", "1");
    fd.append("file", file, file.name);
    const x = new XMLHttpRequest();
    x.open("POST", "/api/upload");
    x.upload.onprogress = (ev) => { if (ev.lengthComputable) onProgress(ev.loaded); };
    x.onload = () => {
      let j = {};
      try { j = JSON.parse(x.responseText); } catch (err) { /* not JSON (proxy error page) */ }
      resolve({ ok: x.status === 200 && !!j.ok, status: x.status, exists: !!j.exists,
                error: j.error || ("HTTP " + x.status) });
    };
    x.onerror = () => resolve({ ok: false, status: 0, exists: false, error: "network error" });
    x.send(fd);
  });
}
async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  if (filesState.uploading) { elFilesStatus.textContent = "An upload is already running."; return; }
  const dir = filesState.cwd;
  if (!dir) { elFilesStatus.textContent = "Open a folder first."; return; }
  filesState.uploading = true;
  document.getElementById("files-upfiles").disabled = true;
  document.getElementById("files-upfolder").disabled = true;
  const limit = filesState.maxUploadMb * 1048576;
  const failed = [];
  const items = [];
  files.forEach((f) => {
    if (f.size > limit) failed.push((f.webkitRelativePath || f.name) + ": too large (limit " + filesState.maxUploadMb + " MB per file)");
    else items.push({ file: f, rel: f.webkitRelativePath || f.name, loaded: 0 });
  });
  const total = items.reduce((n, it) => n + it.file.size, 0);
  let done = 0, doneBytes = 0;
  const conflicts = [];
  const refresh = () => {
    const live = items.reduce((n, it) => n + it.loaded, 0);
    elFilesStatus.textContent = "Uploading " + done + "/" + items.length + " · " +
      fmtSize(doneBytes + live) + " of " + fmtSize(total);
  };
  const pool = async (list, overwrite) => {
    let next = 0;
    const worker = async () => {
      while (next < list.length) {
        const it = list[next++];
        const r = await uploadOne(it.file, dir, it.rel, overwrite, (n) => { it.loaded = n; refresh(); });
        it.loaded = 0;
        if (r.ok) { done++; doneBytes += it.file.size; }
        else if (r.status === 409 && r.exists && !overwrite) conflicts.push(it);
        else failed.push(it.rel + ": " + r.error);
        refresh();
      }
    };
    await Promise.all(Array.from({ length: Math.min(UPLOAD_PARALLEL, list.length) }, worker));
  };
  let skipped = 0;
  try {
    refresh();
    await pool(items, false);
    if (conflicts.length) {
      const again = conflicts.splice(0, conflicts.length);
      if (confirm(again.length + " file(s) already exist in " + dir + ". Overwrite them?")) await pool(again, true);
      else skipped = again.length;
    }
  } finally {
    filesState.uploading = false;
    document.getElementById("files-upfiles").disabled = false;
    document.getElementById("files-upfolder").disabled = false;
  }
  let msg = "Uploaded " + done + " file(s)" + (done ? " (" + fmtSize(doneBytes) + ")" : "") + ".";
  if (skipped) msg += " Skipped " + skipped + " existing.";
  if (failed.length) msg += " " + failed.length + " failed — " + failed[0];
  elFilesStatus.textContent = msg;
  if (filesState.open) {
    const keep = elFilesStatus.textContent;
    await loadDir(filesState.cwd);
    elFilesStatus.textContent = keep;
  }
}
function pickAndUpload(input) {
  const files = input.files;
  uploadFiles(files).finally(() => { input.value = ""; });
}
document.getElementById("files-upfiles").addEventListener("click", () => document.getElementById("files-pick").click());
document.getElementById("files-upfolder").addEventListener("click", () => document.getElementById("files-pickdir").click());
document.getElementById("files-pick").addEventListener("change", (ev) => pickAndUpload(ev.target));
document.getElementById("files-pickdir").addEventListener("change", (ev) => pickAndUpload(ev.target));

async function deleteEntry(e) {
  if (!confirm("Delete " + e.path + "?")) return;
  elFilesStatus.textContent = "Deleting…";
  let r = await fsApi("rm", { path: e.path });
  if (!r.ok && e.is_dir && /not empty|directory not empty/i.test(r.error || "")) {
    if (confirm("Folder is not empty. Delete everything inside it?")) {
      r = await fsApi("rmtree", { path: e.path });
    } else {
      elFilesStatus.textContent = "";
      return;
    }
  }
  if (!r.ok) {
    elFilesStatus.textContent = "Delete failed: " + (r.error || "unknown");
    return;
  }
  loadDir(filesState.cwd);
}
// ---------------------------------------------------------------------------
// File manager: selection (click, ctrl/shift, drag rectangle, touch long-press),
// compress, extract, properties and multi-delete. What a gesture selects is
// decided in files-core.js (unit-tested); this block owns the elements/events.
// ---------------------------------------------------------------------------
const FM = window.FilesCore;
const FM_LONGPRESS_MS = 450;
const FM_FORMAT_KEY = "rconsole.files.archiveformat";
const $f = (id) => document.getElementById(id);
const fmSleep = (ms) => new Promise((res) => setTimeout(res, ms));
const elFSel = $f("files-sel");
const elFSelCount = $f("fsel-count");
const elFDialog = $f("files-dialog");
const elFDTitle = $f("fd-title");
const elFDBody = $f("fd-body");
const elFDActions = $f("fd-actions");
Object.assign(filesState, {
  selected: new Set(), anchor: null, archives: { compress: [], extract: [] },
  noClickUntil: 0, touchAt: 0, jobRunning: false, dialogToken: 0, tipShown: false,
});

function filesOrder() { return filesState.entries.map((e) => e.path); }
function entryByPath(p) { return filesState.entries.find((e) => e.path === p); }
function selectedEntries() { return filesState.entries.filter((e) => filesState.selected.has(e.path)); }

function setSelection(paths, anchor) {
  filesState.selected = new Set(paths);
  if (anchor !== undefined) filesState.anchor = anchor;
  applySelectionUI();
}
function clearSelection() { setSelection([], null); }
function selectAll() { setSelection(filesOrder(), filesState.anchor); }

/** Reflect the selection in the rows and the selection bar (no row is rebuilt: a drag stays smooth). */
function applySelectionUI() {
  const sel = filesState.selected;
  elFilesList.querySelectorAll(".files-row[data-path]").forEach((row) => {
    const on = sel.has(row.dataset.path);
    row.classList.toggle("sel", on);
    row.setAttribute("aria-selected", on ? "true" : "false");
  });
  const items = selectedEntries();
  elFSel.hidden = items.length === 0;
  const total = filesState.entries.length;
  const chk = $f("fsel-check");
  chk.checked = total > 0 && items.length === total;
  if (!items.length) return;
  const d = FM.describeSelection(items);
  elFSelCount.textContent = d.count + (d.count === 1 ? " item" : " items") + " selected" + (d.files ? " · " + FM.fmtBytes(d.bytes) : "");
  $f("fsel-props").hidden = items.length !== 1;                       // Properties: only for a single item
  const exts = filesState.archives.extract;
  $f("fsel-extract").hidden = !items.every((e) => !e.is_dir && FM.isArchive(e.name, exts));
  $f("fsel-compress").disabled = !filesState.archives.compress.length;
}

function openEntry(e) {
  if (!e) return;
  if (e.is_dir) loadDir(e.path);
  else if (mediaKind(e.name)) openViewer(e.path);          // photo / video: show it, keep the browser behind
  else { openCodeEditor(e.path); hideFiles(); }
}

// ---- media viewer: photos are shown, videos play (served inline by /api/media, Range-aware) ----------------
// Keep these lists in sync with MEDIA_TYPES in app.py (a test checks it).
const IMAGE_EXTS = ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif", "svg"];
const VIDEO_EXTS = ["mp4", "m4v", "webm", "ogv", "mov"];
function mediaKind(name) {
  const ext = ((name || "").split(".").pop() || "").toLowerCase();
  if (IMAGE_EXTS.includes(ext)) return "image";
  if (VIDEO_EXTS.includes(ext)) return "video";
  return "";
}
const elViewer = document.getElementById("viewer");
const elViewerStage = document.getElementById("viewer-stage");
const elViewerName = document.getElementById("viewer-name");
const elViewerCount = document.getElementById("viewer-count");
const viewerState = { list: [], idx: -1 };
function mediaUrl(path) { return "/api/media?path=" + encodeURIComponent(path); }
function viewerClearStage() {
  for (const v of elViewerStage.querySelectorAll("video")) { try { v.pause(); } catch (_) {} v.removeAttribute("src"); v.load(); }
  elViewerStage.textContent = "";
}
function viewerMessage(text, entry) {
  elViewerStage.textContent = "";
  const box = document.createElement("div");
  box.className = "viewer-msg";
  box.textContent = text;
  if (entry) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "⬇ Download instead";
    b.onclick = () => downloadEntry(entry);
    box.appendChild(b);
  }
  elViewerStage.appendChild(box);
}
function viewerShow(i) {
  const list = viewerState.list;
  if (!list.length) return;
  viewerState.idx = (i + list.length) % list.length;
  const e = list[viewerState.idx];
  viewerClearStage();
  elViewerName.textContent = e.name;
  elViewerName.title = e.path;
  elViewerCount.textContent = list.length > 1 ? (viewerState.idx + 1) + " / " + list.length : "";
  const multi = list.length > 1;
  document.getElementById("viewer-prev").hidden = !multi;
  document.getElementById("viewer-next").hidden = !multi;
  if (mediaKind(e.name) === "image") {
    const img = document.createElement("img");
    img.alt = e.name;
    img.className = "viewer-img";
    img.onerror = () => viewerMessage("This image can't be displayed by your browser.", e);
    img.onclick = () => img.classList.toggle("actual");            // fit-to-screen <-> actual size
    img.src = mediaUrl(e.path);
    elViewerStage.appendChild(img);
  } else {
    const v = document.createElement("video");
    v.className = "viewer-video";
    v.controls = true;
    v.playsInline = true;
    v.preload = "metadata";
    v.setAttribute("playsinline", "");
    v.onerror = () => viewerMessage("Your browser can't play this video (its codec isn't supported).", e);
    v.src = mediaUrl(e.path);
    elViewerStage.appendChild(v);
  }
}
function openViewer(path) {
  const list = filesState.entries.filter((e) => !e.is_dir && mediaKind(e.name));
  const idx = list.findIndex((e) => e.path === path);
  if (idx < 0) return;
  viewerState.list = list;
  elViewer.removeAttribute("hidden");
  viewerShow(idx);
  elViewer.focus({ preventScroll: true });
}
function closeViewer() {
  viewerClearStage();
  elViewer.setAttribute("hidden", "");
  viewerState.list = [];
  viewerState.idx = -1;
}
document.getElementById("viewer-close").onclick = closeViewer;
document.getElementById("viewer-prev").onclick = () => viewerShow(viewerState.idx - 1);
document.getElementById("viewer-next").onclick = () => viewerShow(viewerState.idx + 1);
document.getElementById("viewer-dl").onclick = () => {
  const e = viewerState.list[viewerState.idx];
  if (e) downloadEntry(e);
};
elViewer.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); closeViewer(); return; }
  if (ev.target && ev.target.tagName === "VIDEO") return;           // arrows seek inside a video
  if (ev.key === "ArrowLeft") { ev.preventDefault(); viewerShow(viewerState.idx - 1); }
  else if (ev.key === "ArrowRight") { ev.preventDefault(); viewerShow(viewerState.idx + 1); }
});
// Swipe left / right on a photo to move between the folder's photos and videos.
(() => {
  let x0 = null, y0 = 0;
  elViewerStage.addEventListener("touchstart", (ev) => {
    if (ev.touches.length !== 1 || ev.target.tagName === "VIDEO") { x0 = null; return; }
    x0 = ev.touches[0].clientX; y0 = ev.touches[0].clientY;
  }, { passive: true });
  elViewerStage.addEventListener("touchend", (ev) => {
    if (x0 === null) return;
    const t = ev.changedTouches[0];
    const dx = t.clientX - x0, dy = t.clientY - y0;
    x0 = null;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) viewerShow(viewerState.idx + (dx < 0 ? 1 : -1));
  }, { passive: true });
})();

function selectClick(path, o) {
  const r = FM.applyClick(filesOrder(), [...filesState.selected], filesState.anchor, path, o);
  setSelection(r.selected, r.anchor);
  elFilesList.focus({ preventScroll: true });
}

// One click handler for the whole list. The name opens the item (as before); the blank space of a row
// selects it. On touch devices a tap only selects once something is selected (a long-press starts it).
elFilesList.addEventListener("click", (ev) => {
  if (Date.now() < filesState.noClickUntil) { ev.preventDefault(); ev.stopPropagation(); return; }
  const row = ev.target.closest(".files-row[data-path]");
  if (!row) {
    if (ev.target === elFilesList && ev.pointerType !== "touch") clearSelection();   // blank area below the rows
    return;
  }
  if (ev.target.closest(".files-inline")) return;
  const touch = ev.pointerType === "touch";
  const mod = ev.ctrlKey || ev.metaKey;
  const shift = ev.shiftKey;
  const btn = ev.target.closest("button");
  if (btn && !btn.classList.contains("files-name")) return;                        // download / rename / delete
  if (btn) {
    const selecting = (touch && filesState.selected.size > 0) || mod || shift;
    if (!selecting) { openEntry(entryByPath(row.dataset.path)); return; }
    ev.preventDefault();
  } else if (touch && !filesState.selected.size) {
    return;
  }
  selectClick(row.dataset.path, { touch, mod, shift });
});
elFilesList.addEventListener("dblclick", (ev) => {
  const row = ev.target.closest(".files-row[data-path]");
  if (!row || ev.target.closest("button, input")) return;
  openEntry(entryByPath(row.dataset.path));
});

// ---- drag a rectangle (mouse / pen) ----------------------------------------
let fmBand = null;
function fmToContent(cx, cy) {
  const r = elFilesList.getBoundingClientRect();
  return { x: cx - r.left + elFilesList.scrollLeft, y: cy - r.top + elFilesList.scrollTop };
}
function fmUpdateBand() {
  const p = fmToContent(fmBand.cx, fmBand.cy), s = fmBand.start;
  const x1 = Math.min(s.x, p.x), y1 = Math.min(s.y, p.y), x2 = Math.max(s.x, p.x), y2 = Math.max(s.y, p.y);
  const st = fmBand.el.style;
  st.left = x1 + "px"; st.top = y1 + "px"; st.width = (x2 - x1) + "px"; st.height = (y2 - y1) + "px";
  const rows = [...elFilesList.querySelectorAll(".files-row[data-path]")];
  const hits = FM.bandHits(rows.map((r) => ({ top: r.offsetTop, height: r.offsetHeight })), y1, y2);
  setSelection(FM.bandSelection(rows.map((r) => r.dataset.path), fmBand.base, hits, fmBand.additive));
}
function fmBandAutoScroll() {
  if (!fmBand || !fmBand.moved || fmBand.raf) return;
  const r = elFilesList.getBoundingClientRect();
  const speed = FM.autoScrollSpeed(fmBand.cy, r.top, r.bottom);
  if (!speed) return;
  fmBand.raf = requestAnimationFrame(() => {
    if (!fmBand) return;
    fmBand.raf = 0;
    elFilesList.scrollTop += speed;
    fmUpdateBand();
    fmBandAutoScroll();
  });
}
function fmBandMove(ev) {
  if (!fmBand) return;
  fmBand.cx = ev.clientX;
  fmBand.cy = ev.clientY;
  if (!fmBand.moved) {
    if (Math.hypot(ev.clientX - fmBand.x0, ev.clientY - fmBand.y0) < 5) return;
    fmBand.moved = true;
    fmBand.el = document.createElement("div");
    fmBand.el.id = "files-band";
    elFilesList.appendChild(fmBand.el);
  }
  fmUpdateBand();
  fmBandAutoScroll();
}
function fmBandEnd() {
  document.removeEventListener("pointermove", fmBandMove);
  document.removeEventListener("pointerup", fmBandEnd);
  document.removeEventListener("pointercancel", fmBandEnd);
  if (fmBand && fmBand.moved) {
    if (fmBand.el) fmBand.el.remove();
    filesState.noClickUntil = Date.now() + 80;                 // the click that follows a drag must not re-select
  }
  fmBand = null;
}
elFilesList.addEventListener("pointerdown", (ev) => {
  if (ev.pointerType === "touch" || (ev.button !== undefined && ev.button !== 0)) return;
  if (ev.target.closest("button, input, .files-inline")) return;
  fmBand = {
    x0: ev.clientX, y0: ev.clientY, cx: ev.clientX, cy: ev.clientY, moved: false, el: null, raf: 0,
    start: fmToContent(ev.clientX, ev.clientY), additive: ev.ctrlKey || ev.metaKey || ev.shiftKey,
    base: new Set(filesState.selected),
  };
  document.addEventListener("pointermove", fmBandMove);
  document.addEventListener("pointerup", fmBandEnd);
  document.addEventListener("pointercancel", fmBandEnd);
});

// ---- touch: long-press selects, then drag to sweep over more rows ----------------
let fmLp = null;
function fmLpEnd() {
  if (!fmLp) return;
  clearTimeout(fmLp.timer);
  if (fmLp.raf) cancelAnimationFrame(fmLp.raf);
  if (fmLp.fired) filesState.noClickUntil = Date.now() + 700;   // swallow the click that follows the lift-off
  fmLp = null;
}
function fmLpFire(me) {
  if (fmLp !== me) return;
  me.fired = true;
  try { if (navigator.vibrate) navigator.vibrate(12); } catch (e) { /* not supported */ }
  const path = me.row.dataset.path;
  me.base = new Set(filesState.selected);
  me.base.add(path);
  me.anchorIdx = filesOrder().indexOf(path);
  setSelection([...me.base], path);
}
function fmLpAutoScroll() {
  if (!fmLp || !fmLp.fired || fmLp.raf) return;
  const r = elFilesList.getBoundingClientRect();
  const speed = FM.autoScrollSpeed(fmLp.cy, r.top, r.bottom);
  if (!speed) return;
  fmLp.raf = requestAnimationFrame(() => {
    if (!fmLp) return;
    fmLp.raf = 0;
    elFilesList.scrollTop += speed;
    fmLpSweep();
    fmLpAutoScroll();
  });
}
function fmLpSweep() {
  const el = document.elementFromPoint ? document.elementFromPoint(fmLp.cx, fmLp.cy) : null;
  const row = el && el.closest ? el.closest(".files-row[data-path]") : null;
  if (!row) return;
  const order = filesOrder();
  setSelection(FM.sweepSelection(order, fmLp.base, fmLp.anchorIdx, order.indexOf(row.dataset.path)));
}
elFilesList.addEventListener("touchstart", (ev) => {
  filesState.touchAt = Date.now();
  fmLpEnd();
  if (ev.touches.length !== 1) return;
  const row = ev.target.closest ? ev.target.closest(".files-row[data-path]") : null;
  if (!row || ev.target.closest(".files-inline")) return;
  const t = ev.touches[0];
  const me = { x: t.clientX, y: t.clientY, cx: t.clientX, cy: t.clientY, row, fired: false, raf: 0, timer: 0, base: null, anchorIdx: -1 };
  me.timer = setTimeout(() => fmLpFire(me), FM_LONGPRESS_MS);
  fmLp = me;
}, { passive: true });
elFilesList.addEventListener("touchmove", (ev) => {
  if (!fmLp) return;
  const t = ev.touches[0];
  if (!fmLp.fired) {                                            // moved before the timer: it is a scroll
    if (Math.hypot(t.clientX - fmLp.x, t.clientY - fmLp.y) > 10) fmLpEnd();
    return;
  }
  if (ev.cancelable) ev.preventDefault();                       // the gesture is ours now: do not scroll
  fmLp.cx = t.clientX;
  fmLp.cy = t.clientY;
  fmLpSweep();
  fmLpAutoScroll();
}, { passive: false });
elFilesList.addEventListener("touchend", fmLpEnd, { passive: true });
elFilesList.addEventListener("touchcancel", fmLpEnd, { passive: true });
elFilesList.addEventListener("contextmenu", (ev) => {
  if (Date.now() - filesState.touchAt < 2000) ev.preventDefault();   // no browser menu after a long-press
});

// ---- dialogs -----------------------------------------------------------------
function dialogOpen() { return !elFDialog.hidden; }
function closeDialog() {
  filesState.dialogToken++;
  elFDialog.hidden = true;
  elFDBody.textContent = "";
  elFDActions.textContent = "";
}
function openDialog(title, buildBody, actions) {
  filesState.dialogToken++;
  elFDTitle.textContent = title;
  elFDBody.textContent = "";
  elFDActions.textContent = "";
  buildBody(elFDBody);
  setDialogActions(actions);
  elFDialog.hidden = false;
  const first = elFDBody.querySelector("input[type=text], select") || elFDActions.querySelector(".primary");
  if (first) setTimeout(() => first.focus({ preventScroll: true }), 30);
}
function setDialogActions(actions) {
  elFDActions.textContent = "";
  actions.forEach((a) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = a.label;
    if (a.primary) b.classList.add("primary");
    if (a.danger) b.classList.add("danger");
    b.addEventListener("click", a.onClick);
    elFDActions.appendChild(b);
  });
}
function dialogNote(parent, text, cls) {
  const p = document.createElement("p");
  p.className = cls || "fd-note";
  p.textContent = text;
  parent.appendChild(p);
  return p;
}
elFDialog.addEventListener("click", (ev) => {
  if (ev.target === elFDialog && !filesState.jobRunning) closeDialog();       // tap outside the card
});
elFDialog.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && ev.target.matches && ev.target.matches("input[type=text]")) {
    const ok = elFDActions.querySelector(".primary");
    if (ok && !ok.disabled) { ev.preventDefault(); ok.click(); }
  }
});

// ---- compress / extract (background jobs, polled) ------------------------------
function fmFormats() { return filesState.archives.compress || []; }

function openCompressDialog() {
  const items = selectedEntries();
  if (!items.length) return;
  const fmts = fmFormats();
  if (!fmts.length) { elFilesStatus.textContent = "Compression is not available on this server."; return; }
  let saved = "";
  try { saved = localStorage.getItem(FM_FORMAT_KEY) || ""; } catch (e) { /* private mode */ }
  let sel, nameInp, extSpan, touched = false;
  const fmtById = (id) => fmts.find((f) => f.id === id);
  const refresh = () => {
    const f = fmtById(sel.value);
    extSpan.textContent = f ? f.ext : "";
    if (!touched && f) nameInp.value = FM.suggestArchiveName(items, filesState.cwd, f.single);
  };
  const go = () => {
    const name = nameInp.value.trim();
    if (!name || /[/\\]/.test(name) || name === "." || name === "..") {
      let err = elFDBody.querySelector(".fd-error");
      if (!err) err = dialogNote(elFDBody, "", "fd-error");
      err.textContent = "Enter a name without slashes.";
      return;
    }
    try { localStorage.setItem(FM_FORMAT_KEY, sel.value); } catch (e) { /* private mode */ }
    runArchiveJob({ op: "compress", items: items.map((e) => e.path), format: sel.value, name },
      "Compressing " + (items.length === 1 ? items[0].name : items.length + " items"));
  };
  openDialog("Compress " + (items.length === 1 ? items[0].name : items.length + " items"), (body) => {
    const l1 = document.createElement("label");
    l1.textContent = "Format";
    l1.htmlFor = "fd-format";
    sel = document.createElement("select");
    sel.id = "fd-format";
    fmts.forEach((f) => {
      const o = document.createElement("option");
      o.value = f.id;
      o.textContent = f.label + " (" + f.ext + ")";
      o.disabled = !FM.formatFits(f, items);
      sel.appendChild(o);
    });
    const pick = [saved, "zip"].find((id) => id && fmtById(id) && FM.formatFits(fmtById(id), items));
    sel.value = pick || (fmts.find((f) => FM.formatFits(f, items)) || fmts[0]).id;
    const l2 = document.createElement("label");
    l2.textContent = "Name";
    l2.htmlFor = "fd-name";
    const row = document.createElement("div");
    row.className = "fd-namerow";
    nameInp = document.createElement("input");
    nameInp.type = "text";
    nameInp.id = "fd-name";
    nameInp.autocomplete = "off";
    nameInp.setAttribute("autocapitalize", "off");
    nameInp.setAttribute("spellcheck", "false");
    extSpan = document.createElement("span");
    row.appendChild(nameInp);
    row.appendChild(extSpan);
    body.appendChild(l1); body.appendChild(sel); body.appendChild(l2); body.appendChild(row);
    dialogNote(body, "Saved in " + (filesState.cwd || "this folder") + ". An existing file is never overwritten.");
    if (items.length > 1 || items[0].is_dir) dialogNote(body, "One-file formats (gz, bz2, xz, zst) need a single plain file, so they are greyed out here.");
    nameInp.addEventListener("input", () => { touched = true; });
    sel.addEventListener("change", refresh);
    refresh();
  }, [
    { label: "Compress", primary: true, onClick: go },
    { label: "Cancel", onClick: closeDialog },
  ]);
}

function openExtractDialog() {
  const items = selectedEntries();
  if (!items.length) return;
  let mode = "folder";
  openDialog("Extract " + (items.length === 1 ? items[0].name : items.length + " archives"), (body) => {
    [["folder", "Extract to a new folder",
      "Uses the archive's own top folder if it has exactly one, otherwise a folder named after the archive."],
     ["here", "Extract here", "Into this folder. Files that already exist are never overwritten."]].forEach(([val, title, hint]) => {
      const lab = document.createElement("label");
      lab.className = "fd-choice";
      const r = document.createElement("input");
      r.type = "radio";
      r.name = "fd-mode";
      r.value = val;
      r.checked = val === mode;
      r.addEventListener("change", () => { mode = val; });
      const txt = document.createElement("span");
      txt.textContent = title;
      const small = document.createElement("small");
      small.textContent = hint;
      txt.appendChild(small);
      lab.appendChild(r);
      lab.appendChild(txt);
      body.appendChild(lab);
    });
  }, [
    { label: "Extract", primary: true,
      onClick: () => runArchiveJob({ op: "extract", items: items.map((e) => e.path), mode },
        "Extracting " + (items.length === 1 ? items[0].name : items.length + " archives")) },
    { label: "Cancel", onClick: closeDialog },
  ]);
}

async function runArchiveJob(body, title) {
  filesState.jobRunning = true;
  elFDTitle.textContent = title;
  elFDBody.textContent = "";
  const note = dialogNote(elFDBody, "Starting…");
  const bar = document.createElement("progress");
  bar.className = "fd-progress";
  elFDBody.appendChild(bar);
  let jobId = null;
  const cancelBtn = { label: "Cancel", onClick: async () => {
    note.textContent = "Cancelling…";
    if (jobId) { try { await fetch("/api/archive/" + jobId + "/cancel", { method: "POST" }); } catch (e) { /* the poll will tell */ } }
  } };
  setDialogActions([cancelBtn]);
  const fail = (msg) => {
    filesState.jobRunning = false;
    elFDBody.textContent = "";
    dialogNote(elFDBody, msg, "fd-error");
    setDialogActions([{ label: "Close", primary: true, onClick: closeDialog }]);
    loadDir(filesState.cwd);
  };
  let r;
  try {
    const resp = await fetch("/api/archive", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    r = await resp.json();
  } catch (e) { r = { ok: false, error: String(e) }; }
  if (!r.ok) { fail(r.error || "Could not start."); return; }
  jobId = r.job;
  let misses = 0;
  for (;;) {
    await fmSleep(filesState.jobRunning && jobId ? 300 : 0);
    let j;
    try {
      const resp = await fetch("/api/archive/" + jobId);
      j = await resp.json();
      // a job object always has "state"; only transport / lookup failures lack it (its error field is the job's own)
      if (!resp.ok || !j.state) throw new Error(j.error || ("HTTP " + resp.status));
      misses = 0;
    } catch (e) {
      if (++misses > 20) { fail("Lost contact with the server: " + e); return; }
      continue;
    }
    if (j.state === "running") {
      note.textContent = (j.note ? j.note + " · " : "") + (j.done ? fmtSize(j.done) + (j.total ? " of " + fmtSize(j.total) : "") : "working…");
      if (j.total > 0) { bar.max = j.total; bar.value = Math.min(j.done, j.total); } else bar.removeAttribute("value");
      continue;
    }
    filesState.jobRunning = false;
    if (j.state === "done") { await archiveJobDone(body.op, j.result || {}); return; }
    if (j.state === "cancelled") {
      closeDialog();
      await loadDir(filesState.cwd);
      elFilesStatus.textContent = "Cancelled.";
      return;
    }
    fail(j.error || "failed");
    return;
  }
}

async function archiveJobDone(op, res) {
  closeDialog();
  await loadDir(filesState.cwd);
  const names = op === "compress" ? [res.name] : (res.names || []);
  const fresh = filesState.entries.filter((e) => names.includes(e.name)).map((e) => e.path);
  if (fresh.length) setSelection(fresh, fresh[0]);
  elFilesStatus.textContent = op === "compress"
    ? "Created " + res.name + (res.size ? " (" + fmtSize(res.size) + ")" : "") + "."
    : "Extracted " + (res.files || 0) + " file(s)" + (res.skipped ? " · " + res.skipped + " skipped (already there or not extractable)" : "") + ".";
}

// ---- properties ----------------------------------------------------------------
function fmtWhen(sec) { return sec ? new Date(sec * 1000).toLocaleString() : "—"; }
async function showProperties(e) {
  if (!e) return;
  openDialog("Properties", (body) => { dialogNote(body, "Loading…"); }, [{ label: "Close", primary: true, onClick: closeDialog }]);
  const token = filesState.dialogToken;
  let j;
  try {
    const r = await fetch("/api/props?path=" + encodeURIComponent(e.path));
    j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status));
  } catch (err) {
    if (token !== filesState.dialogToken) return;
    elFDBody.textContent = "";
    dialogNote(elFDBody, String(err.message || err), "fd-error");
    return;
  }
  if (token !== filesState.dialogToken) return;                  // the dialog was closed / replaced meanwhile
  elFDTitle.textContent = "Properties — " + j.name;
  const rows = [];
  const add = (k, v) => { if (v !== undefined && v !== null && v !== "") rows.push([k, v]); };
  const kind = { folder: "Folder", file: "File", link: "Symbolic link", special: "Special file" }[j.kind] || j.kind;
  add("Name", j.name);
  add("Type", kind + (j.mime ? " (" + j.mime + ")" : ""));
  add("Location", parentDir(j.path));
  if (j.kind === "folder" && j.contents) {
    const c = j.contents;
    add("Contains", c.files + " file(s), " + c.folders + " folder(s)" + (c.partial ? " (counted so far; too large to finish)" : ""));
    add("Size", FM.fmtBytes(j.size) + " (" + j.size.toLocaleString() + " bytes)");
  } else if (j.kind !== "link") {
    add("Size", FM.fmtBytes(j.size) + " (" + j.size.toLocaleString() + " bytes)");
  }
  if (j.disk) add("On disk", FM.fmtBytes(j.disk));
  if (j.kind === "link") add("Points to", j.target + (j.target_exists === false ? " (missing)" : ""));
  if (j.archive) add("Archive holds", j.archive.entries + " entries, " + FM.fmtBytes(j.archive.uncompressed) + " unpacked");
  add("Modified", fmtWhen(j.mtime));
  add("Accessed", fmtWhen(j.atime));
  add("Changed", fmtWhen(j.ctime));
  add("Permissions", j.mode_text + "  (" + j.mode + ")");
  if (j.owner) {
    add("Owner", j.owner.user ? j.owner.user + " (" + j.owner.uid + ")" : String(j.owner.uid));
    add("Group", j.owner.group ? j.owner.group + " (" + j.owner.gid + ")" : String(j.owner.gid));
  }
  add("Access", (j.readable ? "readable" : "not readable") + ", " + (j.writable ? "writable" : "read-only"));
  add("Hard links", j.links > 1 ? j.links : "");
  elFDBody.textContent = "";
  const table = document.createElement("table");
  table.className = "fd-props";
  rows.forEach(([k, v]) => {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.textContent = k;
    const td = document.createElement("td");
    td.textContent = String(v);
    tr.appendChild(th);
    tr.appendChild(td);
    table.appendChild(tr);
  });
  elFDBody.appendChild(table);
}

// ---- delete the selection --------------------------------------------------------
async function deleteSelected() {
  const items = selectedEntries();
  if (!items.length) return;
  const folders = items.filter((e) => e.is_dir).length;
  const msg = items.length === 1
    ? "Delete " + items[0].path + (folders ? " and everything inside it" : "") + "?"
    : "Delete " + items.length + " items" + (folders ? " (" + folders + (folders === 1 ? " folder" : " folders") + " with everything inside)" : "") + "?";
  if (!confirm(msg)) return;
  elFilesStatus.textContent = "Deleting…";
  const r = await fsApi("delete_many", { paths: items.map((e) => e.path) });
  clearSelection();
  await loadDir(filesState.cwd);
  if (r.error && r.deleted === undefined) { elFilesStatus.textContent = "Delete failed: " + r.error; return; }
  elFilesStatus.textContent = "Deleted " + (r.deleted || 0) + (r.deleted === 1 ? " item" : " items") +
    (r.failed && r.failed.length ? " · " + r.failed.length + " failed (" + r.failed[0].error + ")" : "") + ".";
}

$f("fsel-compress").addEventListener("click", openCompressDialog);
$f("fsel-extract").addEventListener("click", openExtractDialog);
$f("fsel-props").addEventListener("click", () => showProperties(selectedEntries()[0]));
$f("fsel-delete").addEventListener("click", deleteSelected);
$f("fsel-clear").addEventListener("click", clearSelection);
$f("fsel-check").addEventListener("click", () => {
  const total = filesState.entries.length;
  if (total && filesState.selected.size === total) clearSelection(); else selectAll();
});

function hideFiles() {
  elFiles.setAttribute("hidden", "");
  filesState.open = false;
  filesState.creating = null;
  filesState.renaming = null;
  clearSelection();
}
function toggleFiles(force) {
  const show = typeof force === "boolean" ? force : elFiles.hasAttribute("hidden");
  if (!show) { hideFiles(); return; }
  const t = (typeof activeTab === "function") ? activeTab() : null;
  elFiles.removeAttribute("hidden");
  filesState.open = true;
  loadDir(filesState.cwd || (t && t.cwd) || "");
  setTimeout(() => elFilesPath.focus({ preventScroll: true }), 60);
}
document.getElementById("filesbtn").addEventListener("click", () => toggleFiles());
document.getElementById("files-close").addEventListener("click", () => hideFiles());
document.getElementById("files-up").addEventListener("click", () => {
  if (filesState.cwd && filesState.parent && filesState.parent !== filesState.cwd) loadDir(filesState.parent);
  else if (filesState.cwd) loadDir(parentDir(filesState.cwd));
});
document.getElementById("files-reload").addEventListener("click", () => loadDir(filesState.cwd || ""));
document.getElementById("files-newfile").addEventListener("click", () => {
  filesState.creating = "file";
  filesState.renaming = null;
  renderFiles();
});
document.getElementById("files-newdir").addEventListener("click", () => {
  filesState.creating = "dir";
  filesState.renaming = null;
  renderFiles();
});
document.getElementById("files-goto").addEventListener("submit", (ev) => {
  ev.preventDefault();
  loadDir(elFilesPath.value.trim());
});
elFiles.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") {
    ev.stopPropagation();
    if (dialogOpen()) { if (!filesState.jobRunning) closeDialog(); }
    else if (filesState.selected.size) clearSelection();
    else hideFiles();
    return;
  }
  if (dialogOpen() || (ev.target.closest && ev.target.closest("input, textarea, select"))) return;
  if ((ev.ctrlKey || ev.metaKey) && (ev.key === "a" || ev.key === "A")) { ev.preventDefault(); selectAll(); }
  else if (ev.key === "Delete") { ev.preventDefault(); deleteSelected(); }
});

// On load, rebuild tabs from the server so persistent terminal sessions
// (e.g. a long-running bot) survive a page refresh and can be re-attached.
async function init() {
  let serverTabs = [];
  try {
    const r = await fetch("/api/tabs");
    if (r.ok) serverTabs = (await r.json()).tabs || [];
  } catch (e) { /* offline: start fresh */ }

  if (!serverTabs.length) {
    newTab();
    return;
  }
  serverTabs.forEach((st, i) => {
    makeTab({
      id: st.id,
      name: st.name,
      cwd: st.cwd,
      isTerminal: st.is_terminal,
      cmd: st.cmd,
      first: i === 0,
    });
  });
  selectTab(tabs[0].id);
}

// ---------------------------------------------------------------------------
// Systems: the dropdown in the tab bar and the inline "choose a system" picker.
// ---------------------------------------------------------------------------
const elTarget = document.getElementById("targetsel");
let targetsInfo = null;
let targetsTimer = null;

async function runFromUi(cmd, tabId) {
  const t = tabs.find((x) => x.id === tabId) || activeTab();
  if (!t) return;
  t.lines.push({ text: promptText(t.cwd) + cmd });
  if (isActiveTab(t)) renderOutput();
  await send(cmd, t.id);
}

function addChoice(tab, choice) {
  const box = document.createElement("div");
  box.className = "choice-box";
  const sel = document.createElement("select");
  sel.className = "choice-select";
  sel.setAttribute("aria-label", choice.title || "Choose a system");
  const ph = new Option((choice.title || "Choose") + " …", "");
  ph.disabled = true;
  ph.selected = true;
  sel.appendChild(ph);
  const main = document.createElement("optgroup");
  main.label = "Run in";
  const inst = document.createElement("optgroup");
  inst.label = "Install another";
  choice.options.forEach((o, i) => {
    const opt = new Option(o.label, String(i));
    (o.group === "install" ? inst : main).appendChild(opt);
  });
  if (main.children.length) sel.appendChild(main);
  if (inst.children.length) sel.appendChild(inst);
  sel.addEventListener("change", async () => {
    const o = choice.options[Number(sel.value)];
    if (!o) return;
    sel.disabled = true;
    for (const c of o.cmds) await runFromUi(c, tab.id);
  });
  box.appendChild(sel);
  // Nothing in here may trigger the screen-level handlers (focus, copy, scroll).
  ["click", "mousedown", "mouseup", "touchstart", "touchend", "pointerdown", "keydown"].forEach(
    (ev) => box.addEventListener(ev, (e) => e.stopPropagation()));
  tab.lines.push({ el: box });
}

function buildTargetSelect(info) {
  targetsInfo = info;
  const before = TARGET_LABEL;
  if (info.label) TARGET_LABEL = info.label;
  if (before !== TARGET_LABEL && typeof renderOutput === "function" && activeTab()) renderOutput();
  const canInstall = info.installable && info.installable.length > 0;
  if (!info.supported && info.targets.length <= 1) { elTarget.hidden = true; return; }
  elTarget.hidden = false;
  elTarget.innerHTML = "";
  if (!info.current) {
    const ph = new Option("choose system…", "");
    ph.disabled = true;
    ph.selected = true;
    elTarget.appendChild(ph);
  }
  info.targets.forEach((t) => {
    const o = new Option((t.kind === "host" ? "🖥 " : "🐧 ") + t.label, t.id);
    if (t.id === info.current) o.selected = true;
    elTarget.appendChild(o);
  });
  if (canInstall && info.supported) {
    const g = document.createElement("optgroup");
    g.label = "Install another";
    info.installable.forEach((d) => g.appendChild(new Option(`+ ${d.name} (${d.size})`, "install:" + d.id)));
    elTarget.appendChild(g);
  }
  updateTargetRow();
  // While something installs, keep the menu fresh so it appears when ready.
  clearTimeout(targetsTimer);
  if (info.installing && info.installing.length) targetsTimer = setTimeout(refreshTargets, 4000);
}

async function refreshTargets() {
  try {
    const r = await fetch("/api/targets", { credentials: "same-origin" });
    if (r.ok) buildTargetSelect(await r.json());
  } catch (e) { /* offline: keep the old menu */ }
}

elTarget.addEventListener("change", async () => {
  const v = elTarget.value;
  const t = activeTab();
  if (v.startsWith("install:")) {
    await runFromUi("vm install " + v.slice(8), t.id);
    refreshTargets();
    return;
  }
  try {
    const r = await fetch("/api/target", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: v }),
    });
    const data = await r.json();
    if (!data.ok) {
      t.lines.push({ text: "target: " + (data.error || "could not switch"), cls: "term-red" });
      renderOutput();
      refreshTargets();
      return;
    }
    tabs.forEach((x) => { if (data.cwds && data.cwds[x.id]) x.cwd = data.cwds[x.id]; });
    buildTargetSelect(data);
    t.lines.push({ text: `Switched to ${data.label}. Plain commands run there now.`, cls: "term-dim" });
    renderOutput();
  } catch (e) {
    refreshTargets();
  }
});

// ---------------------------------------------------------------------------
// Phone layout: the tab bar only has room for the tabs, "+", files and ⚙. The
// theme button and the system dropdown are the *same elements*, moved into the
// settings menu on small screens and back into the bar on larger ones — so no
// feature differs between layouts.
// ---------------------------------------------------------------------------
const elLogout = document.getElementById("logoutbtn");
const elSettingsBtn = document.getElementById("settingsbtn");
const elSettingsMenu = document.getElementById("settingsmenu");
const elTargetRow = document.getElementById("sm-target-row");
const mqPhone = window.matchMedia("(max-width: 640px)");

function updateTargetRow() {
  if (elTargetRow) elTargetRow.hidden = elTarget.hidden || !mqPhone.matches;
}

function closeSettings() {
  elSettingsMenu.hidden = true;
  elSettingsBtn.setAttribute("aria-expanded", "false");
}

function toggleSettings() {
  if (!elSettingsMenu.hidden) { closeSettings(); return; }
  const bar = document.getElementById("tabbar").getBoundingClientRect();
  elSettingsMenu.style.top = Math.round(bar.bottom + 4) + "px";
  elSettingsMenu.hidden = false;
  elSettingsBtn.setAttribute("aria-expanded", "true");
}

function placeControls() {
  const bar = document.getElementById("tabbar");
  const phone = mqPhone.matches;
  if (phone) {
    document.getElementById("sm-theme-slot").appendChild(elTheme);
    document.getElementById("sm-target-slot").appendChild(elTarget);
    document.getElementById("sm-logout-slot").appendChild(elLogout);
  } else {
    bar.insertBefore(elTarget, document.getElementById("filesbtn"));
    bar.insertBefore(elTheme, elSettingsBtn);
    bar.insertBefore(elLogout, elSettingsBtn);
    closeSettings();
  }
  // On wide screens the menu only holds what is not already in the bar (the admin Dashboard link).
  for (const id of ["sm-theme-row", "sm-logout-row"]) {
    const row = document.getElementById(id);
    if (row) row.hidden = !phone;
  }
  updateTargetRow();
}

// The logout button does exactly what the `logout` command does (ends the login, keeps your
// terminals and data so you can come back to them).
elLogout.addEventListener("click", async () => {
  elLogout.disabled = true;
  try {
    const r = await fetch("/api/logout", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    window.location = d.redirect || "/login";
  } catch (e) {
    elLogout.disabled = false;
  }
});

elSettingsBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleSettings(); });
document.addEventListener("click", (e) => {
  if (!elSettingsMenu.hidden && !elSettingsMenu.contains(e.target)) closeSettings();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !elSettingsMenu.hidden) closeSettings();
});
if (mqPhone.addEventListener) mqPhone.addEventListener("change", placeControls);
else if (mqPhone.addListener) mqPhone.addListener(placeControls);   // older Safari
// Choosing a system inside the menu should dismiss it.
elTarget.addEventListener("change", () => { if (mqPhone.matches) closeSettings(); });
placeControls();

init();
applyTheme(currentTheme);
refreshTargets();
