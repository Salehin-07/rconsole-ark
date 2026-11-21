/* Rconsole agent page.
   Plain DOM, no dependencies. Everything the model or a tool produced reaches the page as text and is put in
   with textContent / createElement, never innerHTML, so a hostile web page or file cannot inject markup.
   A turn runs on the server; this page only follows its event log (EventSource + Last-Event-ID), so a dropped
   connection or a reloaded tab simply picks the turn up again. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  /* ================= tiny syntax highlighter ================= */
  var KW = {
    js: "async await break case catch class const continue default delete do else export extends finally for from function if import in instanceof let new of return static super switch this throw try typeof var void while yield true false null undefined",
    py: "and as assert async await break class continue def del elif else except finally for from global if import in is lambda None nonlocal not or pass raise return try while with yield True False self",
    sh: "if then else elif fi for while do done case esac in function return exit export local echo cd set unset source sudo true false",
    c: "auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while class public private protected namespace using new delete template typename try catch throw true false null nullptr bool string",
    go: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var true false nil",
    rs: "as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait type unsafe use where while true false",
    sql: "select from where insert into values update set delete create table drop alter add join left right inner outer on group by order having limit as and or not null is in like distinct union primary key foreign references index",
    json: "true false null", css: "important", html: "", yaml: "true false null yes no"
  };
  var ALIAS = { javascript: "js", jsx: "js", ts: "js", typescript: "js", tsx: "js", node: "js", mjs: "js", python: "py", python3: "py",
    bash: "sh", shell: "sh", zsh: "sh", console: "sh", java: "c", cpp: "c", "c++": "c", cs: "c", csharp: "c", kotlin: "c", swift: "c", php: "c", golang: "go", rust: "rs",
    yml: "yaml", toml: "yaml", ini: "yaml", jsonc: "json", xml: "html", svg: "html", scss: "css", less: "css", dockerfile: "sh", makefile: "sh" };
  var rxCache = {};
  function langRx(l) {
    if (rxCache[l]) return rxCache[l];
    var cm = (l === "py" || l === "sh" || l === "yaml") ? "#[^\\n]*" : (l === "sql" ? "--[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/" : (l === "html" ? "<!--[\\s\\S]*?-->" : "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/"));
    var str = (l === "py" ? "\"\"\"[\\s\\S]*?\"\"\"|'''[\\s\\S]*?'''|" : "") + "\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*'" + (l === "js" || l === "go" ? "|`(?:\\\\.|[^`\\\\])*`" : "");
    var extra = l === "html" ? "|(<\\/?[A-Za-z][\\w:-]*)" : (l === "sh" ? "|(\\$\\{?[A-Za-z_][\\w]*\\}?)" : "");
    rxCache[l] = new RegExp("(" + cm + ")|(" + str + ")|(\\b\\d[\\d_.]*(?:e[+-]?\\d+)?\\b|\\b0x[0-9a-fA-F]+\\b)|([A-Za-z_$][\\w$-]*)" + extra, "g");
    return rxCache[l];
  }
  function highlight(code, lang, into) {
    lang = (lang || "").toLowerCase();
    lang = ALIAS[lang] || lang;
    if (lang === "diff" || lang === "patch") {
      code.split("\n").forEach(function (ln, i, a) {
        var d = el("span", /^\+/.test(ln) && !/^\+\+\+/.test(ln) ? "tk-s" : (/^-/.test(ln) && !/^---/.test(ln) ? "tk-k" : (/^@@/.test(ln) ? "tk-c" : "")), ln + (i < a.length - 1 ? "\n" : ""));
        into.appendChild(d);
      });
      return;
    }
    if (!KW[lang] && lang !== "html" || code.length > 20000) { into.appendChild(document.createTextNode(code)); return; }
    var kw = {}; KW[lang].split(" ").forEach(function (k) { if (k) kw[k] = 1; });
    var rx = langRx(lang), last = 0, m;
    rx.lastIndex = 0;
    function plain(s) { if (s) into.appendChild(document.createTextNode(s)); }
    while ((m = rx.exec(code))) {
      plain(code.slice(last, m.index));
      var cls = m[1] ? "tk-c" : m[2] ? "tk-s" : m[3] ? "tk-n" : m[4] ? (kw[m[4]] || (lang === "sql" && kw[m[4].toLowerCase()]) ? "tk-k" : (code.charAt(rx.lastIndex) === "(" ? "tk-f" : "")) : (m[5] ? (lang === "html" ? "tk-k" : "tk-f") : "");
      if (cls) into.appendChild(el("span", cls, m[0])); else plain(m[0]);
      last = rx.lastIndex;
      if (m[0].length === 0) rx.lastIndex++;
    }
    plain(code.slice(last));
  }

  /* ================= markdown -> DOM ================= */
  var INL = /`([^`\n]+)`|\*\*([^*\n]+?)\*\*|__([^_\n]+?)__|~~([^~\n]+?)~~|\*([^*\s][^*\n]*?)\*|(?:^|(?<=[\s(]))_([^_\n]+?)_(?![\w])|\[([^\]\n]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>)\]]+[^\s<>)\].,;:!?'"])/g;
  function safeUrl(u) { return /^(https?:\/\/|mailto:)/i.test(u) ? u : null; }
  function inline(text, parent) {
    var last = 0, m;
    INL.lastIndex = 0;
    while ((m = INL.exec(text))) {
      var end = m.index + m[0].length;          // nested inline() calls reuse INL: remember where we are
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      var node;
      if (m[1] != null) node = el("code", null, m[1]);
      else if (m[2] != null || m[3] != null) { node = el("strong"); inline(m[2] != null ? m[2] : m[3], node); }
      else if (m[4] != null) { node = el("del"); inline(m[4], node); }
      else if (m[5] != null || m[6] != null) { node = el("em"); inline(m[5] != null ? m[5] : m[6], node); }
      else if (m[7] != null) {
        var u = safeUrl(m[8]);
        if (u) { node = el("a"); node.href = u; node.target = "_blank"; node.rel = "noopener noreferrer"; inline(m[7], node); }
        else node = document.createTextNode(m[0]);
      } else { node = el("a", null, m[9]); node.href = m[9]; node.target = "_blank"; node.rel = "noopener noreferrer"; }
      parent.appendChild(node);
      last = end; INL.lastIndex = end;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }
  function codeBlock(code, lang) {
    var box = el("div", "code"), h = el("div", "code-h"), pre = el("pre"), c = el("code");
    h.appendChild(el("span", null, lang || "text"));
    var b = el("button", null, "Copy"); b.type = "button";
    b.addEventListener("click", function () { copyText(code).then(function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1200); }); });
    h.appendChild(b);
    highlight(code, lang, c);
    pre.appendChild(c); box.appendChild(h); box.appendChild(pre);
    return box;
  }
  function listIndent(s) { return s.match(/^ */)[0].length; }
  var LI = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
  function renderList(lines, i, parent) {
    var m = LI.exec(lines[i]), base = m[1].length, ordered = /\d/.test(m[2]);
    var list = el(ordered ? "ol" : "ul");
    if (ordered && parseInt(m[2], 10) > 1) list.start = parseInt(m[2], 10);
    while (i < lines.length) {
      var mm = LI.exec(lines[i]);
      if (!mm) {
        if (lines[i].trim() === "") {
          var nx = i + 1 < lines.length ? LI.exec(lines[i + 1]) : null;
          if (nx && nx[1].length >= base && (nx[1].length > base || /\d/.test(nx[2]) === ordered)) { i++; continue; }
          break;
        }
        if (listIndent(lines[i]) > base && list.lastChild) { list.lastChild.appendChild(document.createTextNode(" " + lines[i].trim())); i++; continue; }
        break;
      }
      var ind = mm[1].length;
      if (ind < base || (ind === base && /\d/.test(mm[2]) !== ordered)) break;
      if (ind > base) { var r = renderList(lines, i, list.lastChild || list); i = r; continue; }
      var li = el("li");
      var task = /^\[( |x|X)\]\s+/.exec(mm[3]);
      if (task) { var cb = el("input"); cb.type = "checkbox"; cb.disabled = true; cb.checked = task[1] !== " "; li.appendChild(cb); li.appendChild(document.createTextNode(" ")); }
      inline(task ? mm[3].slice(task[0].length) : mm[3], li);
      list.appendChild(li);
      i++;
    }
    parent.appendChild(list);
    return i;
  }
  function splitRow(r) { return r.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); }); }
  function renderBlocks(text, into) {
    var lines = text.replace(/\r/g, "").split("\n"), i = 0;
    var para = [];
    function flush() {
      if (!para.length) return;
      var p = el("p");
      para.forEach(function (ln, k) { if (k) p.appendChild(el("br")); inline(ln, p); });
      into.appendChild(p); para = [];
    }
    while (i < lines.length) {
      var ln = lines[i], m;
      if ((m = /^\s*(```+|~~~+)\s*([\w+#.-]*)\s*$/.exec(ln))) {
        flush();
        var fence = m[1], buf = []; i++;
        while (i < lines.length && lines[i].trim().indexOf(fence) !== 0) { buf.push(lines[i]); i++; }
        i++;
        into.appendChild(codeBlock(buf.join("\n"), m[2]));
        continue;
      }
      if (/^\s*$/.test(ln)) { flush(); i++; continue; }
      if ((m = /^(#{1,4})\s+(.*)$/.exec(ln))) { flush(); var h = el("h" + m[1].length); inline(m[2], h); into.appendChild(h); i++; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(ln)) { flush(); into.appendChild(el("hr")); i++; continue; }
      if (/^\s*>/.test(ln)) {
        flush(); var q = []; while (i < lines.length && /^\s*>/.test(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
        var bq = el("blockquote"); renderBlocks(q.join("\n"), bq); into.appendChild(bq); continue;
      }
      if (LI.test(ln)) { flush(); i = renderList(lines, i, into); continue; }
      if (ln.indexOf("|") >= 0 && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
        flush();
        var wrap = el("div", "tablewrap"), tb = el("table"), th = el("thead"), tr = el("tr");
        splitRow(ln).forEach(function (c) { var t = el("th"); inline(c, t); tr.appendChild(t); });
        th.appendChild(tr); tb.appendChild(th); i += 2;
        var body = el("tbody");
        while (i < lines.length && lines[i].indexOf("|") >= 0 && lines[i].trim()) {
          var r = el("tr"); splitRow(lines[i]).forEach(function (c) { var d = el("td"); inline(c, d); r.appendChild(d); }); body.appendChild(r); i++;
        }
        tb.appendChild(body); wrap.appendChild(tb); into.appendChild(wrap); continue;
      }
      para.push(ln); i++;
    }
    flush();
  }
  /* Incremental renderer: finished blocks are rendered once, only the unfinished tail is redone per update. */
  function MD(root) { this.root = root; this.done = 0; this.tail = null; this.text = ""; }
  MD.prototype.safeSplit = function (text) {
    var pos = 0, fence = false, safe = 0, lines = text.split("\n");
    for (var i = 0; i < lines.length; i++) {
      if (/^\s*(```|~~~)/.test(lines[i])) fence = !fence;
      pos += lines[i].length + 1;
      if (!fence && lines[i].trim() === "" && i < lines.length - 1) safe = Math.min(pos, text.length);
    }
    return safe;
  };
  MD.prototype.update = function (text, final) {
    this.text = text;
    var cut = final ? text.length : this.safeSplit(text);
    if (cut > this.done) {
      var frag = document.createElement("div");
      renderBlocks(text.slice(this.done, cut), frag);
      if (this.tail) { this.root.removeChild(this.tail); this.tail = null; }
      while (frag.firstChild) this.root.appendChild(frag.firstChild);
      this.done = cut;
    }
    if (!final) {
      if (!this.tail) { this.tail = el("div", "tail"); this.root.appendChild(this.tail); }
      this.tail.textContent = "";
      renderBlocks(text.slice(this.done), this.tail);
    } else if (this.tail) { this.root.removeChild(this.tail); this.tail = null; }
  };
  function renderMd(text) { var d = el("div"); renderBlocks(text || "", d); return d; }

  /* ================= helpers ================= */
  function copyText(t) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(t).catch(function () { return legacyCopy(t); });
    return legacyCopy(t);
  }
  function legacyCopy(t) { return new Promise(function (res) { var a = el("textarea"); a.value = t; a.style.position = "fixed"; a.style.opacity = "0"; document.body.appendChild(a); a.select(); try { document.execCommand("copy"); } catch (e) { /* ignore */ } document.body.removeChild(a); res(); }); }
  var toastT;
  function toast(msg) { var t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toastT); toastT = setTimeout(function () { t.classList.remove("on"); }, 2600); }
  function fmtTok(n) { n = n || 0; return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? (n / 1000).toFixed(n >= 1e4 ? 0 : 1) + "k" : String(n); }
  function ago(ts) {
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now"; if (s < 3600) return Math.floor(s / 60) + "m ago"; if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 172800) return "yesterday"; return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function group(ts) {
    var d = new Date(ts * 1000), n = new Date(), day = 864e5, a = new Date(n.getFullYear(), n.getMonth(), n.getDate()).getTime();
    if (d.getTime() >= a) return "Today"; if (d.getTime() >= a - day) return "Yesterday"; if (d.getTime() >= a - 6 * day) return "Previous 7 days"; return "Older";
  }
  function api(path, opt) {
    opt = opt || {};
    var o = { credentials: "same-origin", headers: {} };
    if (opt.body !== undefined) { o.method = "POST"; o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(opt.body); }
    if (opt.method) o.method = opt.method;
    return fetch(path, o).then(function (r) {
      if (r.status === 401) { location.href = "/login"; return Promise.reject(new Error("signed out")); }
      return r.json().catch(function () { return {}; }).then(function (j) { j._status = r.status; return j; });
    });
  }

  /* ================= state ================= */
  var S = { id: null, meta: {}, info: {}, run: false, es: null, last: 0, cmds: [], stick: true, bot: null, say: null, md: null, sayText: "", thinkEl: null,
            tools: {}, sessions: [], live: null, t0: 0, hist: [], hi: -1, pop: null, lastUndo: 0, auto: null };
  var feed = $("feed"), wrap = null, input = $("input"), sendBtn = $("send");
  try { S.hist = JSON.parse(localStorage.getItem("agent.hist") || "[]"); } catch (e) { S.hist = []; }

  function resetFeed() {
    feed.textContent = ""; wrap = el("div", "wrap"); feed.appendChild(wrap);
    S.bot = S.say = S.md = S.thinkEl = S.live = null; S.sayText = ""; S.tools = {};
  }
  function nearBottom() { return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90; }
  var raf = 0;
  function scrollDown(force) {
    if (!force && !S.stick) return;
    if (raf) return;
    raf = requestAnimationFrame(function () { raf = 0; feed.scrollTop = feed.scrollHeight; });
  }
  feed.addEventListener("scroll", function () { S.stick = nearBottom(); $("jump").hidden = S.stick; });
  $("jump").addEventListener("click", function () { S.stick = true; scrollDown(true); $("jump").hidden = true; });

  /* ================= rendering messages ================= */
  var ICON = { bash: "$", bash_output: "$", bash_kill: "■", read: "▤", write: "✎", edit: "✎", grep: "⌕", glob: "⌕", ls: "☰", web_fetch: "⇩", web_search: "⌕", todo_write: "☑", ask_user: "?", exit_plan_mode: "⚑" };
  function userBubble(text) {
    var b = el("div", "msg-user");
    var parts = String(text).split(/(@[\w./~-][\w./~-]*)/g);
    parts.forEach(function (p) { if (/^@[\w./~-]/.test(p)) b.appendChild(el("span", "pill", p)); else if (p) b.appendChild(document.createTextNode(p)); });
    wrap.appendChild(b); S.bot = S.say = S.md = S.thinkEl = null; scrollDown();
    return b;
  }
  function ensureBot() {
    if (S.bot) return S.bot;
    var row = el("div", "msg-bot"), av = el("div", "avatar", "A"), body = el("div", "bot-body");
    row.appendChild(av); row.appendChild(body); wrap.appendChild(row);
    S.bot = body; return body;
  }
  function startSay() {
    if (S.say) return;
    S.say = el("div", "say cursor"); S.sayText = ""; S.md = new MD(S.say); ensureBot().appendChild(S.say);
  }
  function endSay() {
    if (!S.say) return;
    S.md.update(S.sayText, true); S.say.classList.remove("cursor");
    if (!S.sayText.trim()) S.say.remove();
    S.say = S.md = null; S.sayText = "";
  }
  function addText(d) { startSay(); S.sayText += d; S.md.update(S.sayText, false); scrollDown(); }
  function addThink(d) {
    var body = ensureBot();
    if (!S.thinkEl) { var det = el("details", "think"); det.appendChild(el("summary", null, "Thinking")); det.appendChild(el("div", "tx")); body.appendChild(det); S.thinkEl = det; }
    S.thinkEl.lastChild.textContent += d; scrollDown();
  }
  function note(text, cls) { var n = el("div", "note" + (cls ? " " + cls : "")); n.textContent = text; wrap.appendChild(n); S.bot = S.say = S.md = null; scrollDown(); return n; }
  function sysBox(text) { var n = el("div", "sys", text); wrap.appendChild(n); S.bot = S.say = S.md = null; scrollDown(); }
  function sepLine(text) { var s = el("div", "sep", text); wrap.appendChild(s); scrollDown(); }

  function diffView(text) {
    var d = el("div", "diff");
    String(text).split("\n").forEach(function (ln) {
      var c = /^\+\+\+|^---/.test(ln) ? "h" : /^\+/.test(ln) ? "a" : /^-/.test(ln) ? "d" : /^@@/.test(ln) ? "h" : "";
      d.appendChild(el("div", c, ln || " "));
    });
    return d;
  }
  function diffStat(text) { var p = 0, m = 0; String(text).split("\n").forEach(function (l) { if (/^\+(?!\+\+)/.test(l)) p++; else if (/^-(?!--)/.test(l)) m++; }); return [p, m]; }
  function makeTool(id, name, title, args) {
    endSay(); S.thinkEl = null;
    var card = el("div", "tool run"), h = el("div", "tool-h"), body = el("div", "tool-b");
    body.hidden = true;
    h.appendChild(el("span", "ic", ICON[name] || "•")); var t = el("span", "ttl", title || name); h.appendChild(t);
    var meta = el("span", "meta"); h.appendChild(meta); h.appendChild(el("span", "sx"));
    h.addEventListener("click", function () { if (body.firstChild) body.hidden = !body.hidden; });
    card.appendChild(h); card.appendChild(body);
    ensureBot().appendChild(card);
    var rec = { card: card, body: body, meta: meta, title: t, name: name, args: args || {}, out: null };
    S.tools[id] = rec; scrollDown();
    return rec;
  }
  function toolOut(id, d) {
    var r = S.tools[id]; if (!r) return;
    if (!r.out) { r.out = el("pre"); r.body.appendChild(r.out); r.body.hidden = false; }
    r.out.textContent = (r.out.textContent + d).slice(-8000);
    r.body.scrollTop = r.body.scrollHeight; scrollDown();
  }
  function toolEnd(id, ok, ui) {
    var r = S.tools[id]; if (!r) return;
    ui = ui || {};
    r.card.className = "tool " + (ok ? "ok" : "bad");
    if (ui.title) r.title.textContent = ui.title;
    var bits = [];
    if (ui.detail) bits.push(ui.detail);
    if (ui.secs >= 1) bits.push(ui.secs.toFixed(1) + "s");
    r.meta.textContent = bits.join(" · ");
    r.body.textContent = ""; r.out = null;
    if (ui.diff) {
      var st = diffStat(ui.diff);
      if (st[0] || st[1]) { var s = el("span", "stat"); s.appendChild(el("span", "p", "+" + st[0])); s.appendChild(document.createTextNode(" ")); s.appendChild(el("span", "m", "−" + st[1])); r.meta.appendChild(s); }
      r.body.appendChild(diffView(ui.diff)); r.body.hidden = false;
    } else if (r.name === "bash" || ui.out) {
      var pre = el("pre"); pre.textContent = (ui.cmd ? "$ " + ui.cmd + "\n" : "") + (ui.out || ""); r.body.appendChild(pre); r.body.hidden = !(ui.out && ui.out.length < 700 && !ok);
    } else if (ui.plan) { r.body.appendChild(renderMd(ui.plan)); r.body.hidden = true; }
    if (!ok && ui.error && ui.error !== "stopped") { var e = el("div", "err", ui.error); r.body.appendChild(e); r.body.hidden = ui.error === "denied" ? true : false; }
    if (r.name === "read" || r.name === "todo_write") r.body.hidden = true;
    scrollDown();
  }

  function askCard(ev) {
    endSay();
    var c = el("div", "ask " + (ev.kind === "plan" ? "plan" : ""));
    c.appendChild(el("div", "q", ev.kind === "permission" ? "Allow: " + ev.title : ev.title));
    if (ev.detail) {
      var det = el("div", "det");
      if (ev.kind === "plan") { var pt = el("div", "plantext say"); renderBlocks(ev.detail, pt); det.appendChild(pt); }
      else { var pre = el("pre"); pre.textContent = ev.detail; det.appendChild(pre); }
      c.appendChild(det);
    }
    var btns = el("div", "btns"), done = false;
    function answer(v, label) {
      if (done) return; done = true; c.classList.add("done");
      var res = el("div", "res", "→ " + (label || v)); c.appendChild(res);
      api("/api/agent/session/" + S.id + "/answer", { body: { id: ev.id, value: v } });
      input.focus();
    }
    var opts = ev.options || [];
    opts.forEach(function (o, i) {
      var b = el("button", "btn" + (ev.kind === "permission" ? (i === 0 ? " ok" : (o === "Deny" ? " danger" : "")) : (i === 0 && ev.kind === "plan" ? " ok" : "")), o);
      b.type = "button"; b.addEventListener("click", function () { answer(o); }); btns.appendChild(b);
    });
    c.appendChild(btns);
    if (ev.kind === "question") {
      var f = el("input", "free"); f.placeholder = opts.length ? "...or type your own answer and press Enter" : "Type your answer and press Enter";
      f.addEventListener("keydown", function (e) { if (e.key === "Enter" && f.value.trim()) { e.preventDefault(); answer(f.value.trim()); f.disabled = true; } });
      c.appendChild(f); setTimeout(function () { f.focus(); }, 50);
    }
    ensureBot().appendChild(c); S.say = null; scrollDown(true);
  }

  function setTodos(items) {
    var box = $("todos");
    if (!items || !items.length) { box.hidden = true; box.textContent = ""; return; }
    var done = items.filter(function (i) { return i.status === "completed"; }).length;
    var det = el("details"); det.open = true;
    var sum = el("summary"); sum.appendChild(el("span", null, "Tasks")); sum.appendChild(el("span", null, done + "/" + items.length));
    det.appendChild(sum);
    var list = el("div", "list");
    items.forEach(function (i) { var r = el("div", "it " + i.status); r.appendChild(el("span", "b", i.status === "completed" ? "✓" : i.status === "in_progress" ? "▸" : "○")); r.appendChild(el("span", null, i.content)); list.appendChild(r); });
    det.appendChild(list); box.textContent = ""; box.appendChild(det); box.hidden = false;
  }

  function setLive(text) {
    if (!text) { if (S.live) { S.live.remove(); S.live = null; } return; }
    if (!S.live) { S.live = el("div", "live"); S.live.appendChild(el("span", "sp")); S.live.appendChild(el("span", "lt")); }
    S.live.lastChild.textContent = text;
    wrap.appendChild(S.live); scrollDown();
  }

  /* ================= events ================= */
  function setRunning(on) {
    S.run = on; sendBtn.classList.toggle("stop", on); sendBtn.innerHTML = on ? "&#9632;" : "&#10148;"; sendBtn.setAttribute("aria-label", on ? "Stop" : "Send");
    if (on) { S.t0 = Date.now(); $("status").textContent = "working..."; } else setLive(null);
  }
  function handle(ev) {
    S.last = ev.seq || S.last;
    switch (ev.t) {
      case "text": setLive(null); addText(ev.d); break;
      case "think": setLive(null); addThink(ev.d); break;
      case "status": setLive(ev.m); break;
      case "tool": setLive(null); makeTool(ev.id, ev.name, ev.title, ev.args); setLive("Running " + (ev.title || ev.name) + "..."); break;
      case "tool_out": toolOut(ev.id, ev.d); break;
      case "tool_end": toolEnd(ev.id, ev.ok, ev.ui); setLive("Working..."); break;
      case "ask": setLive(null); askCard(ev); $("status").textContent = "waiting for you"; break;
      case "todos": setTodos(ev.items); break;
      case "mode": S.meta.mode = ev.mode; paintChips(); break;
      case "usage": usage(ev); break;
      case "compacted": setLive(null); sepLine("Earlier context summarised"); break;
      case "notice": endSay(); note(ev.m); break;
      case "error": setLive(null); endSay();
        var n = note(ev.m, "err");
        if (ev.action === "setup") { n.appendChild(document.createTextNode(" ")); var a = el("a", null, "Set up a provider"); a.href = "/dashboard#agent"; n.appendChild(a); }
        break;
      case "done": finish(ev); break;
      default: break;
    }
  }
  function usage(u) {
    var pct = u.window ? Math.min(100, Math.round(u.ctx * 100 / u.window)) : 0;
    var m = $("meter"); m.firstElementChild.style.width = pct + "%"; m.className = pct > 85 ? "high" : pct > 60 ? "mid" : "";
    m.title = "Context: " + fmtTok(u.ctx) + " of " + fmtTok(u.window) + " tokens (" + pct + "%)";
    S.meta.ctx_tokens = u.ctx; S.meta.window = u.window;
    $("status").textContent = fmtTok(u.ctx) + " / " + fmtTok(u.window) + " ctx" + (u.cached ? " · " + fmtTok(u.cached) + " cached" : "");
  }
  function finish(ev) {
    endSay(); setLive(null); setRunning(false); S.lastUndo = ev.undo || 0;
    if (ev.reason === "cancel") note("Stopped.");
    var secs = ev.secs ? " · " + ev.secs + "s" : "";
    $("status").textContent = (S.meta.ctx_tokens ? fmtTok(S.meta.ctx_tokens) + " / " + fmtTok(S.meta.window) + " ctx" : "") + secs;
    if (S.es) { S.es.close(); S.es = null; }
    api("/api/agent/session/" + S.id + "?light=1").then(function (d) { if (d.meta) { S.meta = d.meta; paintChips(); } });
    loadSessions(); input.focus();
  }
  var errCount = 0;
  function attach(after) {
    if (S.es) S.es.close();
    var id = S.id, es = new EventSource("/api/agent/session/" + id + "/stream?after=" + (after || 0));
    S.es = es; errCount = 0;
    es.onmessage = function (m) { errCount = 0; try { handle(JSON.parse(m.data)); } catch (e) { /* ignore a bad frame */ } };
    es.addEventListener("end", function () { es.close(); if (S.es === es) S.es = null; if (S.run) finish({ reason: "stop" }); });
    es.addEventListener("reset", function () { es.close(); openSession(id, true); });
    es.onerror = function () { errCount++; if (errCount > 6) { es.close(); S.es = null; openSession(id, true); } };
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden && S.run && S.id && (!S.es || S.es.readyState === 2)) attach(S.last); });

  /* ================= transcript ================= */
  function renderMessages(msgs, compactUpto) {
    resetFeed();
    var pending = {};
    msgs.forEach(function (m) {
      if (compactUpto && m.i === compactUpto) sepLine("Earlier context summarised above");
      if (m.role === "user") {
        if ((m.content || "").indexOf("[Summary of the earlier") === 0) return;
        if ((m.content || "").indexOf("(You returned nothing") === 0) return;
        userBubble(m.shown || m.content || "");
      } else if (m.role === "assistant") {
        if (m.think) { var det = el("details", "think"); det.appendChild(el("summary", null, "Thinking")); det.appendChild(el("div", "tx", m.think)); ensureBot().appendChild(det); }
        if (m.content) { var s = el("div", "say"); renderBlocks(m.content, s); ensureBot().appendChild(s); }
        (m.tool_calls || []).forEach(function (c) { var r = makeTool(c.id, c.name, c.title || titleOf(c), c.args); pending[c.id] = r; });
        S.say = null; if (!(m.tool_calls || []).length) { S.bot = null; }
      } else if (m.role === "tool") {
        var r = S.tools[m.tool_call_id], ui = m.ui || {};
        if (r) { toolEnd(m.tool_call_id, ui.ok !== false && !ui.error, ui); delete pending[m.tool_call_id]; if (m.name === "todo_write" && ui.todos) { /* shown via meta */ } }
      }
    });
    Object.keys(pending).forEach(function (k) { toolEnd(k, false, { error: "interrupted" }); });
    S.bot = S.say = S.md = null;
    if (!msgs.length) emptyState();
    S.stick = true; scrollDown(true);
  }
  function titleOf(c) { var a = c.args || {}; return c.name + (a.path ? " " + a.path : a.command ? ": " + String(a.command).slice(0, 80) : ""); }
  function emptyState() {
    var e = el("div", "empty"); e.appendChild(el("h2", null, "What should we work on?"));
    var sub = el("div", null, S.info.configured === false ? "No AI provider is set up yet." : "I can read, search, edit and run things for you in " + (S.meta.cwd || "this directory") + ".");
    e.appendChild(sub);
    if (S.info.configured === false) { var a = el("a", "btn primary", "Set up a provider"); a.href = "/dashboard#agent"; a.style.display = "inline-block"; a.style.marginTop = "14px"; a.style.textDecoration = "none"; e.appendChild(a); }
    var tips = el("div", "tips");
    [["Explore this project", "Summarise the layout, how to run it and what looks risky"], ["Find and fix a bug", "Search for the cause, change as little as possible, run the tests"],
     ["Write tests", "Add focused tests for the code I name, then run them"], ["Plan before you touch anything", "/plan then describe the change"]].forEach(function (t) {
      var b = el("div", "tip", t[0]); b.appendChild(el("small", null, t[1]));
      b.addEventListener("click", function () { input.value = t[0] === "Plan before you touch anything" ? "/plan" : t[0] + ": "; input.focus(); autosize(); });
      tips.appendChild(b);
    });
    e.appendChild(tips); wrap.appendChild(e);
  }

  /* ================= header ================= */
  var MODE_LABEL = { plan: "Plan", ask: "Ask", edit: "Edit", auto: "Auto" };
  function paintChips() {
    var m = S.meta, cm = $("chip-model"), cd = $("chip-mode");
    cm.textContent = (m.model || S.info.default_model || "no model"); cm.title = (m.provider_name || m.provider || "") + " — choose the model";
    var mode = m.mode || S.info.default_mode || "ask";
    cd.textContent = ""; var pip = el("span", "pip"); cd.appendChild(pip); cd.appendChild(document.createTextNode(MODE_LABEL[mode] || mode)); cd.dataset.mode = mode;
    $("chip-cwd").textContent = "⌂ " + (m.cwd || "…");
    $("title").textContent = m.title || "New chat"; document.title = (m.title && m.title !== "New chat" ? m.title + " — " : "") + "Agent — Rconsole";
    var pct = m.window ? Math.min(100, Math.round((m.ctx_tokens || 0) * 100 / m.window)) : 0;
    var mt = $("meter"); mt.firstElementChild.style.width = pct + "%"; mt.className = pct > 85 ? "high" : pct > 60 ? "mid" : "";
    setTodos(m.todos);
  }

  /* ================= sessions ================= */
  function loadSessions() {
    var q = $("sq").value.trim();
    return api("/api/agent/sessions" + (q ? "?q=" + encodeURIComponent(q) : "")).then(function (d) { S.sessions = d.sessions || []; paintSessions(); return S.sessions; });
  }
  function paintSessions() {
    var box = $("slist"); box.textContent = ""; var cur = "";
    S.sessions.filter(function (s) { return s.turns > 0 || s.id === S.id; }).forEach(function (s) {
      var g = group(s.updated); if (g !== cur) { cur = g; box.appendChild(el("div", "sgroup", g)); }
      var it = el("div", "sitem" + (s.id === S.id ? " on" : "")); it.setAttribute("role", "listitem");
      var st = el("div", "st"); st.appendChild(el("div", "t", s.title || "New chat")); st.appendChild(el("div", "m", (s.model || "") + " · " + ago(s.updated)));
      it.appendChild(st);
      if (s.running) it.appendChild(el("span", "dot"));
      var x = el("button", "x", "×"); x.type = "button"; x.title = "Delete"; x.addEventListener("click", function (e) { e.stopPropagation(); delChat(s); }); it.appendChild(x);
      it.addEventListener("click", function () { openSession(s.id); document.body.classList.remove("side-open"); });
      box.appendChild(it);
    });
    if (!box.firstChild) box.appendChild(el("div", "note", "No chats yet"));
  }
  function delChat(s) {
    if (!confirm("Delete “" + (s.title || "this chat") + "”? This cannot be undone.")) return;
    api("/api/agent/session/" + s.id + "/delete", { body: {} }).then(function () {
      if (s.id === S.id) newChat(); else loadSessions();
    });
  }
  function openSession(id, quiet) {
    if (S.es) { S.es.close(); S.es = null; }
    return api("/api/agent/session/" + id).then(function (d) {
      if (d._status === 404 || !d.id) { toast("That chat no longer exists"); return newChat(); }
      S.id = d.id; S.meta = d.meta; S.cmds = d.commands || [];
      history.replaceState(null, "", "/agent?s=" + d.id);
      renderMessages(d.messages || [], d.compact);
      setRunning(false); paintChips(); S.lastUndo = d.meta.undo || 0;
      if (d.run && d.run.active) { setRunning(true); attach(0); }
      loadSessions(); if (!quiet) input.focus();
      return d;
    });
  }
  function newChat(cwd) {
    return loadSessions().then(function (rows) {
      var reuse = rows.find(function (s) { return s.turns === 0; });
      if (reuse && !cwd) return openSession(reuse.id);
      return api("/api/agent/session", { body: { cwd: cwd || (S.meta && S.meta.cwd) || "" } }).then(function (d) {
        if (!d.id) { toast(d.error || "Could not start a chat"); return null; }
        return openSession(d.id);
      });
    });
  }

  /* ================= sending ================= */
  function remember(text) {
    S.hist = S.hist.filter(function (t) { return t !== text; }); S.hist.unshift(text); S.hist = S.hist.slice(0, 60); S.hi = -1;
    try { localStorage.setItem("agent.hist", JSON.stringify(S.hist)); } catch (e) { /* storage may be unavailable */ }
  }
  function send() {
    if (S.run) { stopRun(); return; }
    var text = input.value.trim(); if (!text || !S.id) return;
    input.value = ""; autosize(); closePop(); remember(text);
    var empty = wrap.querySelector(".empty"); if (empty) empty.remove();
    var bubble = userBubble(text); S.stick = true;
    sendBtn.disabled = true;
    api("/api/agent/session/" + S.id + "/send", { body: { text: text } }).then(function (d) {
      sendBtn.disabled = false;
      if (d.error) { bubble.remove(); input.value = text; autosize(); toast(d.error); return; }
      if (d.started) { setRunning(true); S.last = 0; setLive("Starting..."); attach(0); return; }
      if (d.notice) sysBox(d.notice);
      if (d.refresh) api("/api/agent/session/" + S.id + "?light=1").then(function (x) { if (x.meta) { S.meta = x.meta; paintChips(); } });
      if (d.action === "new") newChat();
      else if (d.action === "open" && d.id) openSession(d.id);
      else if (d.action === "sessions") document.body.classList.add("side-open");
      else if (d.action === "pick-model") modelSheet();
      else if (d.action === "pick-mode") modeSheet();
      else if (d.action === "export") location.href = "/api/agent/session/" + S.id + "/export";
    }).catch(function () { sendBtn.disabled = false; toast("Could not reach the server"); });
  }
  function stopRun() { if (!S.id) return; api("/api/agent/session/" + S.id + "/cancel", { body: {} }); setLive("Stopping..."); }
  sendBtn.addEventListener("click", send);

  function autosize() { input.style.height = "auto"; input.style.height = Math.min(220, input.scrollHeight) + "px"; }
  input.addEventListener("input", function () { autosize(); popFor(); try { if (S.id) localStorage.setItem("agent.draft." + S.id, input.value); } catch (e) { /* ignore */ } });
  var coarse = window.matchMedia && matchMedia("(pointer: coarse)").matches;
  input.addEventListener("keydown", function (e) {
    if (S.pop && S.pop.items.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); S.pop.i = (S.pop.i + (e.key === "ArrowDown" ? 1 : S.pop.items.length - 1)) % S.pop.items.length; paintPop(); return; }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) { e.preventDefault(); pickPop(S.pop.i); return; }
      if (e.key === "Escape") { e.preventDefault(); closePop(); return; }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !coarse) { e.preventDefault(); send(); return; }
    if (e.key === "Escape" && S.run) { e.preventDefault(); stopRun(); return; }
    if (e.key === "ArrowUp" && !e.shiftKey && input.selectionStart === 0 && input.selectionEnd === 0 && (input.value === "" || S.hi >= 0) && S.hist.length) {
      e.preventDefault(); S.hi = Math.min(S.hist.length - 1, S.hi + 1); input.value = S.hist[S.hi]; autosize();
    } else if (e.key === "ArrowDown" && S.hi >= 0 && input.selectionStart === input.value.length) {
      e.preventDefault(); S.hi--; input.value = S.hi >= 0 ? S.hist[S.hi] : ""; autosize();
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !$("sheet").hidden) { closeSheet(); return; }
    if (e.key === "Escape" && S.run && document.activeElement !== input) stopRun();
    if (e.key === "/" && !/^(INPUT|TEXTAREA)$/.test((document.activeElement || {}).tagName || "") && $("sheet").hidden) { e.preventDefault(); input.focus(); }
  });

  /* ----- slash and @ popups ----- */
  function closePop() { S.pop = null; $("pop").hidden = true; }
  function paintPop() {
    var p = $("pop"); p.textContent = "";
    S.pop.items.forEach(function (it, i) {
      var r = el("div", "row" + (i === S.pop.i ? " on" : "")); r.appendChild(el("span", "n", it.label)); r.appendChild(el("span", "d", it.desc || ""));
      r.addEventListener("mousedown", function (e) { e.preventDefault(); pickPop(i); }); p.appendChild(r);
    });
    p.hidden = !S.pop.items.length;
  }
  function pickPop(i) {
    var it = S.pop.items[i]; if (!it) return;
    if (S.pop.kind === "cmd") { input.value = "/" + it.value + (it.args ? " " : ""); if (!it.args) { closePop(); autosize(); send(); return; } }
    else { var pos = input.selectionStart, before = input.value.slice(0, pos).replace(/@[^\s@]*$/, "@" + it.value + " "); input.value = before + input.value.slice(pos); }
    closePop(); autosize(); input.focus();
  }
  var mentionT, popSeq = 0;
  function popFor() {
    var v = input.value, pos = input.selectionStart, mySeq = ++popSeq;
    var m = /^\/([\w-]*)$/.exec(v);
    if (m) {
      var q = m[1].toLowerCase(), items = S.cmds.filter(function (c) { return c.name.indexOf(q) === 0; }).slice(0, 8).map(function (c) { return { label: "/" + c.name + (c.args ? " " + c.args : ""), desc: c.desc, value: c.name, args: !!c.args }; });
      S.pop = { kind: "cmd", items: items, i: 0 }; paintPop(); return;
    }
    var at = /(?:^|\s)@([^\s@]*)$/.exec(v.slice(0, pos));
    if (at && S.id) {
      clearTimeout(mentionT);
      mentionT = setTimeout(function () {
        api("/api/agent/files?s=" + S.id + "&q=" + encodeURIComponent(at[1])).then(function (d) {
          if (mySeq !== popSeq) return;                    // the text moved on while this was loading
          if (!(d.files || []).length) { closePop(); return; }
          S.pop = { kind: "file", items: d.files.map(function (f) { return { label: f, value: f }; }), i: 0 }; paintPop();
        });
      }, 140);
      return;
    }
    closePop();
  }

  /* ================= sheets ================= */
  function openSheet(build) {
    var sh = $("sheet"); sh.textContent = ""; var card = el("div", "card"); sh.appendChild(card); sh.hidden = false;
    sh.onclick = function (e) { if (e.target === sh) closeSheet(); };
    build(card); return card;
  }
  function closeSheet() { $("sheet").hidden = true; $("sheet").textContent = ""; input.focus(); }
  function sheetHead(card, title) { var h = el("h3"); h.appendChild(el("span", null, title)); var x = el("button", null, "×"); x.type = "button"; x.addEventListener("click", closeSheet); h.appendChild(x); card.appendChild(h); }
  function setMeta(body) {
    return api("/api/agent/session/" + S.id + "/meta", { body: body }).then(function (d) {
      if (d.error) { toast(d.error); return false; }
      if (d.id !== undefined || d.mode !== undefined) { S.meta = d; paintChips(); } return true;
    });
  }
  function modeSheet() {
    openSheet(function (card) {
      sheetHead(card, "What may the agent do on its own?");
      var list = el("div", "list");
      (S.info.modes || []).forEach(function (m) {
        var o = el("div", "opt" + (m.id === (S.meta.mode || S.info.default_mode) ? " on" : "")); var l = el("div", "l"); l.appendChild(el("b", null, MODE_LABEL[m.id] || m.id)); l.appendChild(el("span", null, m.help)); o.appendChild(l);
        o.addEventListener("click", function () { setMeta({ mode: m.id }).then(closeSheet); }); list.appendChild(o);
      });
      card.appendChild(list);
    });
  }
  function modelSheet() {
    openSheet(function (card) {
      sheetHead(card, "Choose a model");
      var provs = S.info.providers || [], cur = S.meta.provider || S.info.default;
      if (!provs.length) { card.appendChild(el("div", "note", "No provider is set up yet.")); var a = el("a", "btn primary", "Open Dashboard › AI agent"); a.href = "/dashboard#agent"; a.style.textAlign = "center"; a.style.textDecoration = "none"; card.appendChild(a); return; }
      card.classList.add("mpick");
      // providers: one horizontal row of chips (never pushes the list off the screen)
      var chips = el("div", "pchips"); chips.setAttribute("role", "tablist");
      provs.forEach(function (p) {
        var c = el("button", "pchip" + (p.id === cur ? " on" : ""), p.name); c.type = "button"; c.setAttribute("role", "tab"); c.dataset.id = p.id;
        c.addEventListener("click", function () { if (cur === p.id) return; cur = p.id; s.value = ""; paintP(); loadModels(false); }); chips.appendChild(c);
      });
      card.appendChild(chips);
      function paintP() { Array.prototype.forEach.call(chips.children, function (c) { var on = c.dataset.id === cur; c.classList.toggle("on", on); c.setAttribute("aria-selected", on ? "true" : "false"); }); }
      var bar = el("div", "mbar");
      var s = el("input", "s"); s.type = "search"; s.placeholder = "Search, or type a model id"; s.setAttribute("autocomplete", "off"); s.setAttribute("autocapitalize", "none"); s.setAttribute("spellcheck", "false"); s.setAttribute("enterkeyhint", "go");
      var rl = el("button", "mreload", "↻"); rl.type = "button"; rl.title = "Load the list again"; rl.setAttribute("aria-label", "Load the list again"); rl.addEventListener("click", function () { loadModels(true); });
      bar.appendChild(s); bar.appendChild(rl); card.appendChild(bar);
      var info = el("div", "minfo"); card.appendChild(info);
      var list = el("div", "list mlist"); card.appendChild(list);
      var all = [], rows = [], act = -1, busy = true, ticket = 0;
      function curModel() { var p = provs.filter(function (x) { return x.id === cur; })[0]; return cur === S.meta.provider ? (S.meta.model || (p && p.model) || "") : ((p && p.model) || ""); }
      function mk(id, sub, typed) {
        var o = el("div", "opt" + (id === curModel() && !typed ? " on" : "")); o.setAttribute("role", "option");
        var l = el("div", "l"); l.appendChild(el("b", null, id)); if (sub) l.appendChild(el("span", null, sub)); o.appendChild(l);
        if (id === curModel() && !typed) o.appendChild(el("span", "tag", "current"));
        o.addEventListener("click", function () { pick(id); }); return o;
      }
      function setAct(i) { rows.forEach(function (r, n) { r.classList.toggle("act", n === i); }); act = i; if (rows[i] && rows[i].scrollIntoView) rows[i].scrollIntoView({ block: "nearest" }); }
      function paintM() {
        list.textContent = ""; rows = []; var t = s.value.trim(), q = t.toLowerCase();
        var hits = all.filter(function (m) { return !q || m.toLowerCase().indexOf(q) >= 0; }), shown = hits.slice(0, 120);
        if (t && all.indexOf(t) < 0) rows.push(mk(t, "use this id as typed", true));
        shown.forEach(function (m) { rows.push(mk(m)); });
        rows.forEach(function (r) { list.appendChild(r); });
        info.textContent = hits.length > shown.length ? "Showing " + shown.length + " of " + hits.length + ". Type to narrow it down." : (all.length ? hits.length + " of " + all.length + " models" : "");
        setAct(rows.length ? 0 : -1);
      }
      function pick(m) { setMeta({ provider: cur, model: m }).then(function (ok) { if (ok) closeSheet(); }); }
      function loadModels(fresh) {
        var my = ++ticket; busy = true; all = []; rows = []; rl.disabled = true; info.textContent = "Loading models…"; list.textContent = "";
        api("/api/agent/models", { body: { provider: cur, refresh: !!fresh } }).then(function (d) {
          if (my !== ticket) return;                                  // another provider was chosen meanwhile
          busy = false; rl.disabled = false;
          if (d.error) { info.textContent = ""; list.textContent = ""; var e = el("div", "note err", d.error); list.appendChild(e); list.appendChild(el("div", "note", "You can still type the model id above and press Enter.")); return; }
          all = d.models || [];
          if (!all.length) { info.textContent = ""; list.appendChild(el("div", "note", "This provider did not list any models. Type the model id above and press Enter.")); return; }
          paintM();
        }).catch(function () { if (my !== ticket) return; busy = false; rl.disabled = false; info.textContent = ""; list.textContent = ""; list.appendChild(el("div", "note err", "Could not reach the server.")); });
      }
      s.addEventListener("input", function () { if (!busy) paintM(); });
      s.addEventListener("keydown", function (e) {
        if (e.key === "ArrowDown") { e.preventDefault(); setAct(Math.min(rows.length - 1, act + 1)); }
        else if (e.key === "ArrowUp") { e.preventDefault(); setAct(Math.max(0, act - 1)); }
        else if (e.key === "Enter") { e.preventDefault(); if (rows[act]) rows[act].click(); else if (s.value.trim()) pick(s.value.trim()); }
      });
      loadModels(false);
      // no auto-focus on touch screens: the keyboard would cover the list
      if (!(window.matchMedia && matchMedia("(pointer: coarse)").matches)) setTimeout(function () { s.focus(); }, 50);
    });
  }
  function cwdSheet() {
    openSheet(function (card) {
      sheetHead(card, "Working directory");
      var s = el("input", "s"); s.value = S.meta.cwd || ""; s.placeholder = "/path/to/project"; card.appendChild(s);
      card.appendChild(el("div", "note", "File tools and commands start here. Relative paths work too."));
      var go = el("button", "btn primary", "Use this directory"); go.type = "button";
      function apply() { setMeta({ cwd: s.value.trim() }).then(function (ok) { if (ok) closeSheet(); }); }
      go.addEventListener("click", apply); s.addEventListener("keydown", function (e) { if (e.key === "Enter") apply(); }); card.appendChild(go);
      setTimeout(function () { s.focus(); s.select(); }, 50);
    });
  }
  function menuSheet() {
    openSheet(function (card) {
      sheetHead(card, "Chat");
      var list = el("div", "list");
      function row(label, hint, fn, cls) { var o = el("div", "opt"); var l = el("div", "l"); l.appendChild(el("b", null, label)); if (hint) l.appendChild(el("span", null, hint)); if (cls) l.firstChild.style.color = "var(--bad)"; o.appendChild(l); o.addEventListener("click", function () { closeSheet(); fn(); }); list.appendChild(o); }
      row("Rename", "Give this chat a name", startRename);
      row("Branch this chat", "Continue from here in a copy", function () { api("/api/agent/session/" + S.id + "/fork", { body: {} }).then(function (d) { if (d.id) openSession(d.id); }); });
      row("Compact context", "Summarise earlier messages to free space", function () { input.value = "/compact"; send(); });
      row("Undo last turn’s file changes", S.lastUndo ? S.lastUndo + " saved" : "nothing saved yet", function () { input.value = "/undo"; send(); });
      row("Download as Markdown", null, function () { location.href = "/api/agent/session/" + S.id + "/export"; });
      row("Slash commands", "/help", function () { input.value = "/help"; send(); });
      row("Providers & settings", "Dashboard", function () { location.href = "/dashboard#agent"; });
      row("Delete this chat", null, function () { delChat({ id: S.id, title: S.meta.title }); }, true);
      card.appendChild(list);
    });
  }
  function startRename() {
    var t = $("title"), inp = el("input", "title-edit"); inp.value = S.meta.title || ""; t.replaceWith(inp); inp.focus(); inp.select();
    var done = false;
    function fin(save) { if (done) return; done = true; inp.replaceWith(t); if (save && inp.value.trim()) setMeta({ title: inp.value.trim() }).then(loadSessions); }
    inp.addEventListener("keydown", function (e) { if (e.key === "Enter") fin(true); else if (e.key === "Escape") fin(false); });
    inp.addEventListener("blur", function () { fin(true); });
  }

  /* ================= wiring ================= */
  $("title").addEventListener("click", startRename);
  $("side-back").addEventListener("click", function (e) {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) return;
    try { var r = document.referrer ? new URL(document.referrer) : null; if (r && r.origin === location.origin && r.pathname.replace(/\/+$/, "") === "/console" && history.length > 1) { e.preventDefault(); history.back(); } } catch (x) { /* plain link */ }
  });
  $("chip-model").addEventListener("click", modelSheet); $("chip-mode").addEventListener("click", modeSheet); $("chip-cwd").addEventListener("click", cwdSheet);
  $("menu").addEventListener("click", menuSheet); $("newchat").addEventListener("click", function () { newChat().then(function () { document.body.classList.remove("side-open"); }); });
  $("burger").addEventListener("click", function () { document.body.classList.add("side-open"); loadSessions(); });
  $("scrim").addEventListener("click", function () { document.body.classList.remove("side-open"); });
  var sqT; $("sq").addEventListener("input", function () { clearTimeout(sqT); sqT = setTimeout(loadSessions, 200); });
  window.addEventListener("beforeunload", function () { if (S.es) S.es.close(); });

  function init() {
    var qs = new URLSearchParams(location.search);
    api("/api/agent/state").then(function (info) {
      S.info = info;
      var s = qs.get("s"), cwd = qs.get("cwd"), q = qs.get("q");
      var start = s ? openSession(s) : newChat(cwd);
      Promise.resolve(start).then(function () {
        if (q && S.id && !S.run && info.configured) { input.value = q; autosize(); history.replaceState(null, "", "/agent?s=" + S.id); send(); }
        else { try { var d = localStorage.getItem("agent.draft." + S.id); if (d && !input.value) { input.value = d; autosize(); } } catch (e) { /* ignore */ } }
      });
    });
  }
  window.__agent = { md: renderMd, highlight: highlight, MD: MD, fmtTok: fmtTok, handle: handle, S: S, renderMessages: renderMessages, resetFeed: resetFeed, diffView: diffView, init: init };
  if (!window.__AGENT_NO_INIT) { resetFeed(); init(); }
})();
