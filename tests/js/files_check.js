// Unit tests for static/files-core.js (file manager selection + archive-name logic).
// Run: node tests/js/files_check.js
const assert = require("assert");
const FC = require("../../static/files-core.js");

let n = 0;
const ok = (name, fn) => { try { fn(); n++; } catch (e) { console.error("FAIL: " + name + "\n  " + e.message); process.exitCode = 1; } };
const order = ["/a", "/b", "/c", "/d", "/e"];
const EXTS = [".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".7z", ".rar"];

ok("archiveExt picks the longest match and ignores bare extensions", () => {
  assert.strictEqual(FC.archiveExt("x.tar.gz", EXTS), ".tar.gz");
  assert.strictEqual(FC.archiveExt("X.TGZ", EXTS), ".tgz");
  assert.strictEqual(FC.archiveExt("notes.txt.gz", EXTS), ".gz");
  assert.strictEqual(FC.archiveExt(".zip", EXTS), "");
  assert.strictEqual(FC.archiveExt("readme.md", EXTS), "");
  assert.strictEqual(FC.archiveExt("a.rar", []), "");
  assert.strictEqual(FC.isArchive("a.7z", EXTS), true);
});

ok("name helpers", () => {
  assert.strictEqual(FC.stripLastExt("report.final.docx"), "report.final");
  assert.strictEqual(FC.stripLastExt(".bashrc"), ".bashrc");
  assert.strictEqual(FC.stripLastExt("Makefile"), "Makefile");
  assert.strictEqual(FC.baseName("/home/ark/proj/"), "proj");
  assert.strictEqual(FC.baseName("C:\\Users\\a"), "a");
  assert.strictEqual(FC.baseName("/"), "");
});

ok("default archive names", () => {
  const f = { name: "notes.txt", is_dir: false }, d = { name: "src", is_dir: true };
  assert.strictEqual(FC.suggestArchiveName([f], "/h/p", false), "notes");
  assert.strictEqual(FC.suggestArchiveName([f], "/h/p", true), "notes.txt");      // gzip keeps the full name
  assert.strictEqual(FC.suggestArchiveName([d], "/h/p", false), "src");
  assert.strictEqual(FC.suggestArchiveName([f, d], "/h/p", false), "p");
  assert.strictEqual(FC.suggestArchiveName([f, d], "/", false), "archive");
});

ok("which formats fit a selection", () => {
  const zip = { single: false }, gz = { single: true };
  const f = { is_dir: false }, d = { is_dir: true };
  assert.strictEqual(FC.formatFits(zip, [f, d]), true);
  assert.strictEqual(FC.formatFits(zip, []), false);
  assert.strictEqual(FC.formatFits(gz, [f]), true);
  assert.strictEqual(FC.formatFits(gz, [d]), false);
  assert.strictEqual(FC.formatFits(gz, [f, f]), false);
});

ok("describeSelection", () => {
  assert.deepStrictEqual(FC.describeSelection([{ is_dir: true }, { is_dir: false, size: 10 }, { is_dir: false, size: 5 }]),
    { count: 3, files: 2, folders: 1, bytes: 15 });
});

ok("plain click selects just that row", () => {
  assert.deepStrictEqual(FC.applyClick(order, ["/a", "/b"], "/a", "/c", {}), { selected: ["/c"], anchor: "/c" });
});
ok("ctrl / touch click toggles", () => {
  assert.deepStrictEqual(FC.applyClick(order, ["/c"], "/c", "/a", { mod: true }), { selected: ["/a", "/c"], anchor: "/a" });
  assert.deepStrictEqual(FC.applyClick(order, ["/a", "/c"], "/a", "/c", { mod: true }), { selected: ["/a"], anchor: "/c" });
  assert.deepStrictEqual(FC.applyClick(order, ["/b"], "/b", "/d", { touch: true }).selected, ["/b", "/d"]);
  assert.deepStrictEqual(FC.applyClick(order, ["/b"], "/b", "/b", { touch: true }).selected, []);
});
ok("shift click selects the range from the anchor (either direction)", () => {
  assert.deepStrictEqual(FC.applyClick(order, ["/b"], "/b", "/d", { shift: true }), { selected: ["/b", "/c", "/d"], anchor: "/b" });
  assert.deepStrictEqual(FC.applyClick(order, ["/d"], "/d", "/a", { shift: true }).selected, ["/a", "/b", "/c", "/d"]);
  // no anchor yet: behaves like a plain click
  assert.deepStrictEqual(FC.applyClick(order, [], null, "/c", { shift: true }), { selected: ["/c"], anchor: "/c" });
});
ok("selection stays in list order", () => {
  assert.deepStrictEqual(FC.applyClick(order, ["/e", "/a"], "/a", "/c", { mod: true }).selected, ["/a", "/c", "/e"]);
});

ok("rubber band hits rows by vertical overlap", () => {
  const rows = [0, 1, 2, 3, 4].map((i) => ({ top: i * 30, height: 30 }));
  assert.deepStrictEqual(FC.bandHits(rows, 35, 70), [1, 2]);
  assert.deepStrictEqual(FC.bandHits(rows, 70, 35), [1, 2]);          // dragged upwards
  assert.deepStrictEqual(FC.bandHits(rows, 30, 60), [1]);             // touching an edge is not overlap
  assert.deepStrictEqual(FC.bandHits(rows, -20, 5), [0]);
  assert.deepStrictEqual(FC.bandHits(rows, 500, 600), []);
  assert.deepStrictEqual(FC.bandHits([], 0, 10), []);
});
ok("band selection replaces, or adds when ctrl/shift is held", () => {
  assert.deepStrictEqual(FC.bandSelection(order, new Set(["/e"]), [1, 2], false), ["/b", "/c"]);
  assert.deepStrictEqual(FC.bandSelection(order, new Set(["/e"]), [1, 2], true), ["/b", "/c", "/e"]);
  assert.deepStrictEqual(FC.bandSelection(order, new Set(["/e"]), [], false), []);
});
ok("long-press sweep adds the swept range to what was selected", () => {
  assert.deepStrictEqual(FC.sweepSelection(order, new Set(["/e"]), 1, 3), ["/b", "/c", "/d", "/e"]);
  assert.deepStrictEqual(FC.sweepSelection(order, new Set(), 3, 1), ["/b", "/c", "/d"]);   // swept upwards
  assert.deepStrictEqual(FC.sweepSelection(order, new Set(["/a"]), 2, 2), ["/a", "/c"]);   // back at the start row
  assert.deepStrictEqual(FC.sweepSelection(order, new Set(["/a"]), -1, 2), ["/a"]);
});
ok("pruneSelection drops vanished paths", () => {
  assert.deepStrictEqual(FC.pruneSelection(["/a", "/c"], ["/a", "/b", "/c"]), ["/a", "/c"].filter((p) => ["/a", "/b", "/c"].includes(p)));
  assert.deepStrictEqual(FC.pruneSelection(["/a"], ["/a", "/gone"]), ["/a"]);
});

ok("auto-scroll speed near the edges", () => {
  assert.strictEqual(FC.autoScrollSpeed(300, 100, 500, 32, 18), 0);
  assert.ok(FC.autoScrollSpeed(105, 100, 500, 32, 18) < 0);
  assert.ok(FC.autoScrollSpeed(495, 100, 500, 32, 18) > 0);
  assert.strictEqual(FC.autoScrollSpeed(95, 100, 500, 32, 18), -18);      // beyond the edge: full speed
  assert.strictEqual(FC.autoScrollSpeed(10, 0, 20, 32, 18), 0);           // list too short to scroll
});

ok("fmtBytes", () => {
  assert.strictEqual(FC.fmtBytes(0), "0 B");
  assert.strictEqual(FC.fmtBytes(1023), "1023 B");
  assert.strictEqual(FC.fmtBytes(1536), "1.50 KB");
  assert.strictEqual(FC.fmtBytes(50 * 1024 * 1024), "50.0 MB");
  assert.strictEqual(FC.fmtBytes(null), "");
});

console.log(process.exitCode ? "files_check: FAILURES" : "files_check: " + n + " checks passed");
