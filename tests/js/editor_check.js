// Unit tests for static/editor-core.js (pure editing logic). Run: node tests/js/editor_check.js
const assert = require("assert");
const EC = require("../../static/editor-core.js");

let n = 0;
const ok = (name, fn) => { try { fn(); n++; } catch (e) { console.error("FAIL: " + name + "\n  " + e.message); process.exitCode = 1; } };

// Build state from a string with | as caret or [..] as selection
function parse(s) {
  let a = s.indexOf("["), b = s.indexOf("]");
  if (a >= 0 && b > a) return { text: s.slice(0, a) + s.slice(a + 1, b) + s.slice(b + 1), start: a, end: b - 1 };
  const c = s.indexOf("|");
  return { text: s.replace("|", ""), start: c, end: c };
}
function apply(text, ed) {
  const t = text.slice(0, ed.from) + ed.insert + text.slice(ed.to);
  return t.slice(0, ed.selStart) + (ed.selStart === ed.selEnd ? "|" : "[") + t.slice(ed.selStart, ed.selEnd) + (ed.selStart === ed.selEnd ? "" : "]") + t.slice(ed.selEnd);
}
const run = (fn, s, ...args) => { const p = parse(s); const ed = fn(p.text, p.start, p.end, ...args); return ed ? apply(p.text, ed) : null; };
const O = (lang, unit) => ({ lang, unit: unit || EC.defaultIndent(lang) });

ok("indent detection", () => {
  assert.strictEqual(EC.detectIndent("def a():\n    x\n    if y:\n        z\n", "python").unit, "    ");
  assert.strictEqual(EC.detectIndent("a:\n  b:\n    c: 1\n", "yaml").unit, "  ");
  assert.strictEqual(EC.detectIndent("f {\n\tx\n\ty\n}\n", "c").unit, "\t");
  assert.strictEqual(EC.detectIndent("x\n", "go").unit, "\t");
  assert.strictEqual(EC.detectIndent("x\n", "javascript").unit, "  ");
  assert.strictEqual(EC.detectIndent("x\n", "python").unit, "    ");
});

ok("enter keeps indent", () => assert.strictEqual(run(EC.enter, "  foo|", O("javascript")), "  foo\n  |"));
ok("enter after {", () => assert.strictEqual(run(EC.enter, "if (x) {|", O("javascript")), "if (x) {\n  |"));
ok("enter between {}", () => assert.strictEqual(run(EC.enter, "f() {|}", O("javascript")), "f() {\n  |\n}"));
ok("enter between {} indented", () => assert.strictEqual(run(EC.enter, "  f() {|}", O("javascript")), "  f() {\n    |\n  }"));
ok("enter between ()", () => assert.strictEqual(run(EC.enter, "f(|)", O("python")), "f(\n    |\n)"));
ok("enter python colon", () => assert.strictEqual(run(EC.enter, "def f():|", O("python")), "def f():\n    |"));
ok("enter python return dedents", () => assert.strictEqual(run(EC.enter, "    return 1|", O("python")), "    return 1\n|"));
ok("enter yaml colon", () => assert.strictEqual(run(EC.enter, "a:|", O("yaml")), "a:\n  |"));
ok("enter tabs", () => assert.strictEqual(run(EC.enter, "\tfoo {|", O("go")), "\tfoo {\n\t\t|"));
ok("enter on blank indent line clears it", () => assert.strictEqual(run(EC.enter, "a\n    |", O("python")), "a\n\n    |"));
ok("enter html tag splits", () => assert.strictEqual(run(EC.enter, "<div>|</div>", O("html")), "<div>\n  |\n</div>"));
ok("enter html void no indent", () => assert.strictEqual(run(EC.enter, "<br>|", O("html")), "<br>\n|"));
ok("enter markdown bullet", () => assert.strictEqual(run(EC.enter, "- one|", O("markdown")), "- one\n- |"));
ok("enter markdown number", () => assert.strictEqual(run(EC.enter, "1. one|", O("markdown")), "1. one\n2. |"));
ok("enter markdown empty bullet ends list", () => assert.strictEqual(run(EC.enter, "- one\n- |", O("markdown")), "- one\n|"));
ok("enter doc comment", () => assert.strictEqual(run(EC.enter, "/**|", O("javascript")), "/**\n * |"));
ok("enter bash then", () => assert.strictEqual(run(EC.enter, "if x; then|", O("bash")), "if x; then\n    |"));
ok("enter replaces selection", () => assert.strictEqual(run(EC.enter, "a[bc]d", O("javascript")), "a\n|d"));

ok("auto-close bracket", () => assert.strictEqual(run(EC.typeChar, "f|", "(", O("javascript")), "f(|)"));
ok("no auto-close before word", () => assert.strictEqual(run(EC.typeChar, "|abc", "(", O("javascript")), null));
ok("auto-close curly", () => assert.strictEqual(run(EC.typeChar, "|", "{", O("javascript")), "{|}"));
ok("overtype closer", () => assert.strictEqual(run(EC.typeChar, "f(a|)", ")", O("javascript")), "f(a)|"));
ok("auto-close quote", () => assert.strictEqual(run(EC.typeChar, "x = |", '"', O("python")), 'x = "|"'));
ok("no quote pair after word (apostrophe)", () => assert.strictEqual(run(EC.typeChar, "don|", "'", O("javascript")), null));
ok("overtype quote", () => assert.strictEqual(run(EC.typeChar, 'x = "a|"', '"', O("python")), 'x = "a"|'));
ok("python prefix quote", () => assert.strictEqual(run(EC.typeChar, "x = f|", '"', O("python")), 'x = f"|"'));
ok("python triple quote", () => assert.strictEqual(run(EC.typeChar, '""|', '"', O("python")), '"""|"""'));
ok("markdown no apostrophe pair", () => assert.strictEqual(run(EC.typeChar, "it|", "'", O("markdown")), null));
ok("wrap selection brackets", () => assert.strictEqual(run(EC.typeChar, "a[bc]d", "(", O("javascript")), "a([bc])d"));
ok("wrap selection quotes", () => assert.strictEqual(run(EC.typeChar, "a[bc]d", '"', O("javascript")), 'a"[bc]"d'));
ok("markdown wrap bold", () => assert.strictEqual(run(EC.typeChar, "a[bc]d", "*", O("markdown")), "a*[bc]*d"));
ok("closer dedents on blank line", () => assert.strictEqual(run(EC.typeChar, "f {\n  |", "}", O("javascript")), "f {\n}|"));
ok("html auto close tag", () => assert.strictEqual(run(EC.typeChar, "<div|", ">", O("html")), "<div>|</div>"));
ok("html void tag not closed", () => assert.strictEqual(run(EC.typeChar, "<br|", ">", O("html")), null));
ok("html attrs tag", () => assert.strictEqual(run(EC.typeChar, '<a href="x"|', ">", O("html")), '<a href="x">|</a>'));

ok("backspace deletes pair", () => assert.strictEqual(run(EC.backspace, "f(|)", O("javascript")), "f|"));
ok("backspace deletes quote pair", () => assert.strictEqual(run(EC.backspace, 'x = "|"', O("python")), "x = |"));
ok("backspace smart unindent", () => assert.strictEqual(run(EC.backspace, "a\n        |", O("python")), "a\n    |"));
ok("backspace smart unindent partial", () => assert.strictEqual(run(EC.backspace, "a\n      |", O("python")), "a\n    |"));
ok("backspace normal", () => assert.strictEqual(run(EC.backspace, "ab|", O("python")), null));
ok("backspace ignores selection", () => assert.strictEqual(run(EC.backspace, "[ab]", O("python")), null));

ok("tab inserts to tab stop", () => assert.strictEqual(run(EC.tab, "ab|", false, O("python")), "ab  |"));
ok("tab inserts full unit at col 0", () => assert.strictEqual(run(EC.tab, "|", false, O("python")), "    |"));
ok("tab char", () => assert.strictEqual(run(EC.tab, "|", false, O("go")), "\t|"));
ok("tab indents selection", () => assert.strictEqual(run(EC.tab, "[a\nb]", false, O("python")), "[    a\n    b]"));
ok("tab skips blank lines", () => assert.strictEqual(run(EC.tab, "[a\n\nb]", false, O("python")), "[    a\n\n    b]"));
ok("shift-tab outdents", () => assert.strictEqual(run(EC.tab, "    a|", true, O("python")), "a|"));
ok("shift-tab outdents selection", () => assert.strictEqual(run(EC.tab, "[    a\n      b]", true, O("python")), "[a\n  b]"));
ok("shift-tab nothing to do", () => assert.strictEqual(run(EC.tab, "a|", true, O("python")), null));
ok("indent ignores line after trailing newline in selection", () =>
  assert.strictEqual(run(EC.tab, "[a\nb\n]c", false, O("python")), "[    a\n    b\n]c"));

ok("comment line", () => assert.strictEqual(run(EC.toggleComment, "x = 1|", O("python")), "# x = 1|"));
ok("uncomment line", () => assert.strictEqual(run(EC.toggleComment, "# x = 1|", O("python")), "x = 1|"));
ok("comment multi keeps min indent", () => assert.strictEqual(run(EC.toggleComment, "[  a\n    b]", O("javascript")), "[  // a\n  //   b]"));
ok("uncomment multi", () => assert.strictEqual(run(EC.toggleComment, "[// a\n// b]", O("javascript")), "[a\nb]"));
ok("comment css block", () => assert.strictEqual(run(EC.toggleComment, "a { }|", O("css")), "/* a { } */|"));
ok("uncomment html block", () => { const r = run(EC.toggleComment, "<!-- <b>x</b> -->|", O("html")); assert.ok(r.startsWith("<b>x</b>")); });
ok("comment unsupported lang", () => assert.strictEqual(run(EC.toggleComment, "x|", O("json")), null));

ok("duplicate down", () => assert.strictEqual(run(EC.duplicateLines, "a|\nb", 1), "a\na|\nb"));
ok("duplicate up keeps caret on top copy", () => assert.strictEqual(run(EC.duplicateLines, "a|\nb", -1), "a|\na\nb"));
ok("delete line", () => assert.strictEqual(run(EC.deleteLines, "a\nb|\nc"), "a\n|c"));
ok("delete last line", () => assert.strictEqual(run(EC.deleteLines, "a\nb|"), "a|"));
ok("move up", () => assert.strictEqual(run(EC.moveLines, "a\nb|\nc", -1), "b|\na\nc"));
ok("move down", () => assert.strictEqual(run(EC.moveLines, "a|\nb\nc", 1), "b\na|\nc"));
ok("move up at top", () => assert.strictEqual(run(EC.moveLines, "a|\nb", -1), null));
ok("move down at bottom", () => assert.strictEqual(run(EC.moveLines, "a\nb|", 1), null));
ok("insert line below", () => assert.strictEqual(run(EC.insertLine, "  a|b\nc", true, O("javascript")), "  ab\n  |\nc"));
ok("insert line above", () => assert.strictEqual(run(EC.insertLine, "  a|b\nc", false, O("javascript")), "  |\n  ab\nc"));
ok("join lines", () => assert.strictEqual(run(EC.joinLines, "a|\n   b", O("javascript")), "a |b"));
ok("select line", () => assert.strictEqual(run(EC.selectLine, "a\nb|c\nd"), "a\n[bc\n]d"));
ok("home target toggles", () => {
  assert.strictEqual(EC.homeTarget("    abc", 7), 4);
  assert.strictEqual(EC.homeTarget("    abc", 4), 0);
  assert.strictEqual(EC.homeTarget("    abc", 0), 4);
});

ok("lineCol / lineStartIndex", () => {
  assert.deepStrictEqual(EC.lineCol("ab\ncd", 4), { line: 2, col: 2 });
  assert.deepStrictEqual(EC.lineCol("ab\ncd", 0), { line: 1, col: 1 });
  assert.strictEqual(EC.lineStartIndex("ab\ncd\nef", 3), 6);
  assert.strictEqual(EC.lineStartIndex("ab\ncd", 99), 3);
});

ok("find basics", () => {
  const r = EC.findAll("Foo foo FOO", "foo", {});
  assert.strictEqual(r.matches.length, 3);
  assert.strictEqual(EC.findAll("Foo foo FOO", "foo", { caseSensitive: true }).matches.length, 1);
  assert.strictEqual(EC.findAll("foobar foo", "foo", { wholeWord: true }).matches.length, 1);
  assert.strictEqual(EC.findAll("a1 b22", "\\d+", { regex: true }).matches.length, 2);
  assert.strictEqual(EC.findAll("a.b", ".", {}).matches.length, 1);
  assert.ok(EC.findAll("x", "(", { regex: true }).error);
  assert.strictEqual(EC.findAll("x", "", {}).matches.length, 0);
});
ok("find zero-length regex terminates", () => assert.ok(EC.findAll("abc", "x*", { regex: true }).matches.length >= 3));
ok("next/prev index wrap", () => {
  const m = EC.findAll("a a a", "a", {}).matches;
  assert.strictEqual(EC.nextIndex(m, 1, false), 1);
  assert.strictEqual(EC.nextIndex(m, 5, false), 0);
  assert.strictEqual(EC.nextIndex(m, 2, true), 0);
  assert.strictEqual(EC.nextIndex(m, 0, true), 2);
});
ok("replace all", () => {
  assert.deepStrictEqual(EC.replaceAll("a b a", "a", "x", {}), { text: "x b x", count: 2 });
  assert.deepStrictEqual(EC.replaceAll("a1 b2", "(\\w)(\\d)", "$2$1", { regex: true }), { text: "1a 2b", count: 2 });
  assert.deepStrictEqual(EC.replaceAll("a$1", "a", "$1", {}), { text: "$1$1", count: 1 });
});

console.log(process.exitCode ? "editor_check: FAILURES" : "editor_check: " + n + " checks passed");
