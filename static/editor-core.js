/* rconsole code editor: pure editing logic (no DOM).
 *
 * Every function takes the text plus the selection (start, end) and returns an
 * "edit" -- {from, to, insert, selStart, selEnd} -- or null when the browser's
 * own behaviour should run. console.js applies edits through the browser's
 * editing stack so Ctrl+Z / Ctrl+Y keep working. Being DOM-free, this file is
 * unit-tested in plain Node (tests/js/editor_check.js).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.EditorCore = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---- language tables (ids match console.js LANG_BY_EXT) -----------------
  const TABS = new Set(["go", "make"]);
  const TWO = new Set(["javascript", "typescript", "json", "css", "html", "xml", "yaml", "toml", "ruby",
    "lua", "dart", "elixir", "clojure", "lisp", "ocaml", "haskell", "markdown", "graphql", "proto", "latex"]);
  const LINE_C = {
    python: "#", bash: "#", ruby: "#", yaml: "#", toml: "#", ini: "#", perl: "#", r: "#", julia: "#",
    powershell: "#", awk: "#", tcl: "#", puppet: "#", cmake: "#", docker: "#", make: "#", nginx: "#",
    apache: "#", graphql: "#", nim: "#", elixir: "#", po: "#",
    dot: "//", javascript: "//", typescript: "//", c: "//", cpp: "//", csharp: "//", java: "//", kotlin: "//",
    scala: "//", groovy: "//", go: "//", rust: "//", zig: "//", swift: "//", dart: "//", php: "//",
    proto: "//", solidity: "//", verilog: "//", fsharp: "//", pascal: "//",
    sql: "--", lua: "--", haskell: "--", ada: "--", vhdl: "--",
    lisp: ";", clojure: ";", asm: ";", fortran: "!", vim: "\"", latex: "%", matlab: "%", vb: "'", erlang: "%", batch: "REM",
  };
  const BLOCK_C = {
    html: ["<!-- ", " -->"], xml: ["<!-- ", " -->"], markdown: ["<!-- ", " -->"],
    css: ["/* ", " */"], ocaml: ["(* ", " *)"],
  };
  const CLIKE = new Set(["javascript", "typescript", "c", "cpp", "csharp", "java", "kotlin", "scala", "groovy",
    "go", "rust", "zig", "swift", "dart", "php", "proto", "solidity", "css", "fsharp"]);
  const NO_APOS = new Set(["text", "markdown", "rst", "latex", "rust", "lisp", "clojure", "ocaml", "fsharp", "po", "diff"]);
  const NO_BTICK = new Set(["text", "rst", "latex", "po", "diff"]);
  const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr"]);

  const PAIR = { "(": ")", "[": "]", "{": "}", "\"": "\"", "'": "'", "`": "`" };
  const OPEN = "([{", CLOSE = ")]}", QUOTES = "\"'`";
  const CLOSE_BEFORE = ";:.,=}])>";
  let WORD;
  try { WORD = new RegExp("[\\p{L}\\p{N}_$]", "u"); } catch (e) { WORD = /[A-Za-z0-9_$]/; }

  // ---- small helpers -------------------------------------------------------
  const lineStartOf = (t, i) => t.lastIndexOf("\n", i - 1) + 1;
  const lineEndOf = (t, i) => { const k = t.indexOf("\n", i); return k < 0 ? t.length : k; };
  const leadingWs = (s) => /^[ \t]*/.exec(s)[0];
  const edit = (from, to, insert, selStart, selEnd) =>
    ({ from, to, insert, selStart, selEnd: selEnd === undefined ? selStart : selEnd });
  const move = (pos) => edit(pos, pos, "", pos);   // cursor-only "edit"
  const unitWidth = (unit) => (unit === "\t" ? 4 : unit.length);

  function defaultIndent(lang) {
    if (TABS.has(lang)) return "\t";
    return TWO.has(lang) ? "  " : "    ";
  }

  /** Guess the file's indentation from its content; fall back to the language default. */
  function detectIndent(text, lang) {
    const lines = text.split("\n", 3000);
    let tabs = 0, spaces = 0, prev = 0;
    const diffs = {};
    for (const l of lines) {
      if (!l.trim()) continue;
      if (l[0] === "\t") { tabs++; continue; }
      const n = /^ */.exec(l)[0].length;
      if (n > 0) {
        spaces++;
        const d = Math.abs(n - prev);
        if (d >= 2 && d <= 8) diffs[d] = (diffs[d] || 0) + 1;
      }
      prev = n;
    }
    if (tabs > spaces && tabs > 0) return { unit: "\t", detected: true };
    let best = 0, bestN = 0;
    for (const d of [2, 4, 3, 8]) if ((diffs[d] || 0) > bestN) { best = d; bestN = diffs[d]; }
    if (best && bestN >= 1) return { unit: " ".repeat(best), detected: true };
    return { unit: defaultIndent(lang), detected: false };
  }

  function lineCol(text, pos) {
    let line = 1, i = -1;
    while ((i = text.indexOf("\n", i + 1)) !== -1 && i < pos) line++;
    return { line, col: pos - lineStartOf(text, pos) + 1 };
  }

  /** Index where 1-based `line` starts (clamped to the last line). */
  function lineStartIndex(text, line) {
    let idx = 0;
    for (let n = 1; n < line; n++) {
      const k = text.indexOf("\n", idx);
      if (k < 0) break;
      idx = k + 1;
    }
    return idx;
  }

  // ---- Enter ---------------------------------------------------------------
  function enter(text, start, end, o) {
    const lang = o.lang, unit = o.unit;
    const ls = lineStartOf(text, start);
    const before = text.slice(ls, start);
    const trimmed = before.replace(/[ \t]+$/, "");
    const last = trimmed.slice(-1);   // "" when the line is blank (note: "abc".includes("") is true)
    const after = text.slice(end);
    const next = after.charAt(0);
    let indent = leadingWs(before);
    const blankBefore = before.length > 0 && /^[ \t]+$/.test(before);
    const from = blankBefore ? ls : start;   // a whitespace-only line is cleaned up

    // markdown lists: continue the bullet / number, or end the list on an empty item
    if (lang === "markdown") {
      const m = /^(\s*)([-*+]|\d+[.)])(\s+)(\[[ xX]\]\s+)?(.*)$/.exec(before);
      if (m) {
        if (m[5].trim() === "" && start === end) return edit(ls, start, "", ls);
        let bullet = m[2];
        const num = /^(\d+)([.)])$/.exec(bullet);
        if (num) bullet = (parseInt(num[1], 10) + 1) + num[2];
        const ins = "\n" + m[1] + bullet + m[3] + (m[4] ? "[ ] " : "");
        return edit(start, end, ins, start + ins.length);
      }
    }
    // /** doc comments: keep the leading * going
    if (CLIKE.has(lang) && lang !== "css") {
      let m = /^(\s*)\/\*\*?(?!.*\*\/)/.exec(before);
      if (m) { const ins = "\n" + m[1] + " * "; return edit(start, end, ins, start + ins.length); }
      m = /^(\s*)\*(\s|$)/.exec(before);
      if (m && /^\s*\*\s?[^/]*$/.test(before)) { const ins = "\n" + m[1] + "* "; return edit(start, end, ins, start + ins.length); }
    }
    // Python: return/pass/break/continue/raise end a block -> dedent one level
    if (lang === "python" && /^\s*(return|pass|break|continue|raise)\b/.test(before) && indent.endsWith(unit)) {
      indent = indent.slice(0, indent.length - unit.length);
    }

    let extra = "";
    if (last && OPEN.includes(last)) extra = unit;
    else if ((lang === "python" || lang === "make") && last === ":") extra = unit;
    else if (lang === "yaml" && (/:\s*$/.test(before) || /[|>][+-]?\s*$/.test(before))) extra = unit;
    else if (lang === "bash" && /(^|[\s;])(then|do|else)\s*$/.test(before)) extra = unit;

    // HTML / XML: <div>|</div> splits, an open tag indents
    let tagOpen = false;
    if (lang === "html" || lang === "xml") {
      const m = /<([A-Za-z][\w:.-]*)(?:\s[^<>]*)?>$/.exec(trimmed);
      if (m && !/\/>$/.test(trimmed) && !(lang === "html" && VOID.has(m[1].toLowerCase()))) {
        tagOpen = true; extra = unit;
      }
    }

    const between = (last && OPEN.includes(last) && next === PAIR[last] && before.endsWith(last)) ||
      (tagOpen && after.startsWith("</"));
    if (between) {
      const ins = "\n" + indent + extra + "\n" + indent;
      return edit(from, end, ins, from + 1 + indent.length + extra.length);
    }
    const ins = "\n" + indent + extra;
    return edit(from, end, ins, from + ins.length);
  }

  // ---- typing a character --------------------------------------------------
  function canPairQuote(text, start, ch, lang, next) {
    if (ch === "'" && NO_APOS.has(lang)) return false;
    if (ch === "`" && NO_BTICK.has(lang)) return false;
    const prev = text.charAt(start - 1);
    if (prev === ch) return false;
    if (prev && WORD.test(prev)) {
      const prefix = lang === "python" && /[rbfuRBFU]/.test(prev) && !WORD.test(text.charAt(start - 2));
      if (!prefix) return false;
    }
    if (next && (WORD.test(next) || !(CLOSE_BEFORE.includes(next) || /\s/.test(next)))) return false;
    return true;
  }

  function typeChar(text, start, end, ch, o) {
    if (!ch || ch.length !== 1) return null;
    const lang = o.lang, unit = o.unit;
    const next = text.charAt(end);
    const has = start !== end;

    if (has) {   // wrap the selection
      if (OPEN.includes(ch) || QUOTES.includes(ch)) {
        const close = PAIR[ch];
        return edit(start, end, ch + text.slice(start, end) + close, start + 1, end + 1);
      }
      if (lang === "markdown" && "*_~".includes(ch)) {
        return edit(start, end, ch + text.slice(start, end) + ch, start + 1, end + 1);
      }
      return null;
    }
    // type over a closer that is already there
    if ((CLOSE.includes(ch) || QUOTES.includes(ch)) && next === ch) return move(start + 1);

    if ("}])".includes(ch)) {   // closer on a whitespace-only line: step one level back
      const ls = lineStartOf(text, start);
      const before = text.slice(ls, start);
      if (before && /^[ \t]+$/.test(before) && before.endsWith(unit)) {
        const ind = before.slice(0, before.length - unit.length);
        return edit(ls, start, ind + ch, ls + ind.length + 1);
      }
      return null;
    }
    if (OPEN.includes(ch)) {
      if (next === "" || /\s/.test(next) || CLOSE_BEFORE.includes(next)) {
        return edit(start, start, ch + PAIR[ch], start + 1);
      }
      return null;
    }
    if (QUOTES.includes(ch)) {
      // Python docstrings: "" + " -> """|"""
      if (lang === "python" && ch !== "`" && text.slice(start - 2, start) === ch + ch &&
          text.charAt(start - 3) !== ch && next !== ch) {
        return edit(start, start, ch + ch + ch + ch, start + 1);
      }
      if (canPairQuote(text, start, ch, lang, next)) return edit(start, start, ch + ch, start + 1);
      return null;
    }
    if (ch === ">" && (lang === "html" || lang === "xml")) {   // <div> -> <div>|</div>
      const before = text.slice(lineStartOf(text, start), start);
      const m = /<([A-Za-z][\w:.-]*)(?:\s[^<>]*)?$/.exec(before);
      if (m && text.charAt(start - 1) !== "/" && !(lang === "html" && VOID.has(m[1].toLowerCase())) &&
          !text.slice(start).startsWith("</" + m[1])) {
        return edit(start, start, ">" + "</" + m[1] + ">", start + 1);
      }
    }
    return null;
  }

  // ---- Backspace -----------------------------------------------------------
  function backspace(text, start, end, o) {
    if (start !== end || start === 0) return null;
    const prev = text.charAt(start - 1), next = text.charAt(start);
    if (PAIR[prev] && PAIR[prev] === next) {
      if (QUOTES.includes(prev) && WORD.test(text.charAt(start - 2) || " ") && prev !== "`") {
        // e.g. don't"| -- not an auto pair, delete normally
        return null;
      }
      return edit(start - 1, start + 1, "", start - 1);
    }
    const unit = o.unit;
    if (unit !== "\t") {
      const ls = lineStartOf(text, start);
      const before = text.slice(ls, start);
      if (before.length > 0 && /^ +$/.test(before)) {   // inside the indentation: delete a whole level
        const w = unit.length;
        const del = Math.min(before.length, before.length % w || w);
        return edit(start - del, start, "", start - del);
      }
    }
    return null;
  }

  // ---- line-block helpers --------------------------------------------------
  function blockRange(text, start, end) {
    const endAdj = end > start && text.charAt(end - 1) === "\n" ? end - 1 : end;
    return { ls: lineStartOf(text, start), le: lineEndOf(text, endAdj), endAdj };
  }

  function outdentLine(l, unit) {
    if (l.charAt(0) === "\t") return l.slice(1);
    const m = /^ +/.exec(l);
    if (!m) return l;
    return l.slice(Math.min(m[0].length, unitWidth(unit)));
  }

  /** Re-map a selection after each line of a block changed by a per-line delta. */
  function mapSelection(text, ls, lines, out, start, end, endAdj) {
    let pos = ls, npos = ls, ns = start, ne = end, total = 0, endDone = false, startDone = false;
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i], d = out[i].length - l.length, lEnd = pos + l.length;
      if (!startDone && start >= pos && start <= lEnd) {
        ns = start === pos ? npos : Math.max(npos, start + d);
        startDone = true;
      }
      if (!endDone && endAdj >= pos && endAdj <= lEnd) {
        ne = Math.max(npos, end + total + d);
        endDone = true;
      }
      total += d; pos = lEnd + 1; npos += out[i].length + 1;
    }
    if (start === end) ne = ns;
    return [ns, ne];
  }

  function indentLines(text, start, end, dir, o) {
    const { ls, le, endAdj } = blockRange(text, start, end);
    const lines = text.slice(ls, le).split("\n");
    const out = lines.map((l) => (dir > 0 ? (l.length ? o.unit + l : l) : outdentLine(l, o.unit)));
    const block = out.join("\n");
    if (block === text.slice(ls, le)) return null;
    const [ns, ne] = mapSelection(text, ls, lines, out, start, end, endAdj);
    return edit(ls, le, block, ns, ne);
  }

  /** Tab / Shift+Tab. */
  function tab(text, start, end, shift, o) {
    if (!shift && start === end) {
      if (o.unit === "\t") return edit(start, end, "\t", start + 1);
      const col = start - lineStartOf(text, start);
      const n = o.unit.length - (col % o.unit.length);
      return edit(start, end, " ".repeat(n), start + n);
    }
    return indentLines(text, start, end, shift ? -1 : 1, o);
  }

  // ---- comments ------------------------------------------------------------
  function commentTokens(lang) { return { line: LINE_C[lang] || null, block: BLOCK_C[lang] || null }; }

  function toggleComment(text, start, end, o) {
    const { line, block } = commentTokens(o.lang);
    if (!line && !block) return null;
    const { ls, le } = blockRange(text, start, end);
    const lines = text.slice(ls, le).split("\n");
    const live = lines.filter((l) => l.trim() !== "");
    if (!live.length) return null;
    let out;
    if (line) {
      const all = live.every((l) => l.trimStart().startsWith(line));
      if (all) {
        out = lines.map((l) => {
          if (!l.trim()) return l;
          const ind = leadingWs(l), rest = l.slice(ind.length + line.length);
          return ind + (rest.startsWith(" ") ? rest.slice(1) : rest);
        });
      } else {
        const col = Math.min(...live.map((l) => leadingWs(l).length));
        out = lines.map((l) => (l.trim() ? l.slice(0, col) + line + " " + l.slice(col) : l));
      }
    } else {
      const [op, cl] = block, opT = op.trim(), clT = cl.trim();
      const all = live.every((l) => { const t = l.trim(); return t.startsWith(opT) && t.endsWith(clT) && t.length >= opT.length + clT.length; });
      if (all) {
        out = lines.map((l) => {
          if (!l.trim()) return l;
          const ind = leadingWs(l);
          let body = l.slice(ind.length).trimEnd().slice(opT.length);
          body = body.slice(0, body.length - clT.length);
          if (body.startsWith(" ")) body = body.slice(1);
          if (body.endsWith(" ")) body = body.slice(0, -1);
          return ind + body;
        });
      } else {
        out = lines.map((l) => { if (!l.trim()) return l; const ind = leadingWs(l); return ind + op + l.slice(ind.length).trimEnd() + cl; });
      }
    }
    const newBlock = out.join("\n");
    if (start === end) {
      const li = text.slice(ls, start).split("\n").length - 1;
      const d = out[li].length - lines[li].length;
      const caret = Math.max(ls, start + d);
      return edit(ls, le, newBlock, caret);
    }
    return edit(ls, le, newBlock, ls, ls + newBlock.length);
  }

  // ---- line commands -------------------------------------------------------
  function duplicateLines(text, start, end, dir) {
    const { ls, le } = blockRange(text, start, end);
    const block = text.slice(ls, le);
    const shift = block.length + 1;
    return edit(le, le, "\n" + block, dir > 0 ? start + shift : start, dir > 0 ? end + shift : end);
  }

  function deleteLines(text, start, end) {
    const { ls, le } = blockRange(text, start, end);
    let from = ls, to = le + 1;
    if (le >= text.length) { to = le; if (ls > 0) from = ls - 1; }
    return edit(from, to, "", from);
  }

  function moveLines(text, start, end, dir) {
    const { ls, le } = blockRange(text, start, end);
    const block = text.slice(ls, le);
    if (dir < 0) {
      if (ls === 0) return null;
      const ps = lineStartOf(text, ls - 1), prev = text.slice(ps, ls - 1);
      const sh = -(prev.length + 1);
      return edit(ps, le, block + "\n" + prev, start + sh, end + sh);
    }
    if (le >= text.length) return null;
    const ne = lineEndOf(text, le + 1), nxt = text.slice(le + 1, ne);
    const sh = nxt.length + 1;
    return edit(ls, ne, nxt + "\n" + block, start + sh, end + sh);
  }

  function insertLine(text, start, end, below, o) {
    const { ls, le } = blockRange(text, start, end);
    if (below) return enter(text, le, le, o);
    const ind = leadingWs(text.slice(ls, le));
    return edit(ls, ls, ind + "\n", ls + ind.length);
  }

  function joinLines(text, start, end) {
    const { ls, le, endAdj } = blockRange(text, start, end);
    let stop = le;
    if (start === end || endAdj === start) { if (le >= text.length) return null; stop = lineEndOf(text, le + 1); }
    const block = text.slice(ls, stop);
    const joined = block.replace(/[ \t]*\n\s*/g, " ");
    const first = block.search(/[ \t]*\n/);
    const caret = ls + (first < 0 ? block.length : first) + 1;
    return edit(ls, stop, joined, start === end ? caret : ls, start === end ? caret : ls + joined.length);
  }

  function selectLine(text, start, end) {
    const { ls, le } = blockRange(text, start, end);
    return edit(ls, ls, "", ls, Math.min(text.length, le + 1));
  }

  /** Smart Home: first press goes to the first non-blank, second to column 0. */
  function homeTarget(text, pos) {
    const ls = lineStartOf(text, pos);
    const first = ls + leadingWs(text.slice(ls, lineEndOf(text, ls))).length;
    return pos === first ? ls : first;
  }

  // ---- find / replace ------------------------------------------------------
  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  function makeRegex(query, o) {
    let src = o.regex ? query : escapeRe(query);
    if (o.wholeWord) src = "\\b(?:" + src + ")\\b";
    try { return new RegExp(src, "gm" + (o.caseSensitive ? "" : "i")); } catch (e) { return null; }
  }

  function findAll(text, query, o) {
    o = o || {};
    if (!query) return { matches: [], error: null };
    const re = makeRegex(query, o);
    if (!re) return { matches: [], error: "Invalid regular expression" };
    const matches = [];
    let m;
    while ((m = re.exec(text)) !== null && matches.length < 20000) {
      matches.push({ index: m.index, length: m[0].length, text: m[0] });
      if (m[0].length === 0) re.lastIndex++;
    }
    return { matches, error: null };
  }

  function nextIndex(matches, pos, backwards) {
    if (!matches.length) return -1;
    if (!backwards) {
      for (let i = 0; i < matches.length; i++) if (matches[i].index >= pos) return i;
      return 0;
    }
    for (let i = matches.length - 1; i >= 0; i--) if (matches[i].index < pos) return i;
    return matches.length - 1;
  }

  function replacementFor(match, query, repl, o) {
    if (!o.regex) return repl;
    const re = makeRegex(query, o);
    if (!re) return repl;
    return match.text.replace(new RegExp(re.source, re.flags.replace("g", "")), repl);
  }

  function replaceAll(text, query, repl, o) {
    const re = makeRegex(query, o || {});
    if (!re || !query) return { text, count: 0 };
    let count = 0;
    const out = text.replace(re, (...a) => {
      count++;
      if (!(o && o.regex)) return repl;
      const m = a[0];
      return m.replace(new RegExp(re.source, re.flags.replace("g", "")), repl);
    });
    return { text: out, count };
  }

  return {
    defaultIndent, detectIndent, lineCol, lineStartIndex, lineStartOf, lineEndOf,
    enter, typeChar, backspace, tab, indentLines, commentTokens, toggleComment,
    duplicateLines, deleteLines, moveLines, insertLine, joinLines, selectLine, homeTarget,
    findAll, nextIndex, replacementFor, replaceAll,
  };
});
