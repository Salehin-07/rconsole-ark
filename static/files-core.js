/* rconsole file manager: pure selection / naming logic (no DOM).
 *
 * console.js owns the elements and events; the decisions live here so they can be unit-tested
 * in plain Node (tests/js/files_check.js): what a click, shift/ctrl-click, a drag rectangle or a
 * long-press drag selects, and what an archive should be called.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.FilesCore = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---- archive names -------------------------------------------------------
  /** Longest archive extension from `exts` that `name` ends with ("" if none). */
  function archiveExt(name, exts) {
    const low = String(name || "").toLowerCase();
    let best = "";
    for (const e of exts || []) {
      if (low.endsWith(e) && e.length > best.length && low.length > e.length) best = e;
    }
    return best;
  }

  function isArchive(name, exts) { return archiveExt(name, exts) !== ""; }

  /** Name without its last extension ("report.final.docx" -> "report.final"; dotfiles stay whole). */
  function stripLastExt(name) {
    const i = String(name).lastIndexOf(".");
    return i > 0 ? name.slice(0, i) : name;
  }

  function baseName(path) {
    const p = String(path || "").replace(/[\\/]+$/, "");
    const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
    return i >= 0 ? p.slice(i + 1) : p;
  }

  /**
   * A sensible default archive name (without extension).
   * One item: the item's own name; one-file formats (gz, bz2 ...) keep the full file name so that
   * extracting gives it back (`notes.txt` -> `notes.txt.gz`); several items: the current folder's name.
   */
  function suggestArchiveName(entries, cwd, singleFileFormat) {
    if (entries.length === 1) {
      const e = entries[0];
      if (e.is_dir) return e.name;
      return singleFileFormat ? e.name : stripLastExt(e.name);
    }
    return baseName(cwd) || "archive";
  }

  /** Can `fmt` ({single: bool}) be used for this selection? One-file formats need exactly one plain file. */
  function formatFits(fmt, entries) {
    if (!fmt.single) return entries.length > 0;
    return entries.length === 1 && !entries[0].is_dir;
  }

  function describeSelection(entries) {
    let files = 0, folders = 0, bytes = 0;
    for (const e of entries) {
      if (e.is_dir) folders++;
      else { files++; bytes += e.size || 0; }
    }
    return { count: entries.length, files, folders, bytes };
  }

  // ---- selection semantics -------------------------------------------------
  const inOrder = (order, set) => order.filter((p) => set.has(p));

  /**
   * What a click on row `path` does to the selection.
   *   touch / ctrl|cmd : toggle that row (selection mode, like Android / Explorer)
   *   shift            : select the range from the anchor to the row
   *   plain mouse      : select only that row
   * Returns {selected: [paths in list order], anchor}.
   */
  function applyClick(order, selected, anchor, path, o) {
    o = o || {};
    const sel = new Set(selected);
    if (o.touch || o.mod) {
      if (sel.has(path)) sel.delete(path); else sel.add(path);
      return { selected: inOrder(order, sel), anchor: path };
    }
    if (o.shift && anchor && order.includes(anchor) && order.includes(path)) {
      return { selected: rangeBetween(order, anchor, path), anchor };
    }
    return { selected: [path], anchor: path };
  }

  /** Paths from `a` to `b` inclusive, whichever comes first in `order`. */
  function rangeBetween(order, a, b) {
    const i = order.indexOf(a), j = order.indexOf(b);
    if (i < 0 || j < 0) return [];
    return order.slice(Math.min(i, j), Math.max(i, j) + 1);
  }

  /** Indices of rows ({top, height}) that overlap the vertical span [y1, y2] (either order). */
  function bandHits(rows, y1, y2) {
    const lo = Math.min(y1, y2), hi = Math.max(y1, y2);
    const hits = [];
    rows.forEach((r, i) => { if (r.top < hi && r.top + r.height > lo) hits.push(i); });
    return hits;
  }

  /** Selection while dragging a rectangle: only what is inside, or `base` plus it when additive. */
  function bandSelection(order, base, hitIdx, additive) {
    const sel = new Set(additive ? base : []);
    hitIdx.forEach((i) => { if (order[i] !== undefined) sel.add(order[i]); });
    return inOrder(order, sel);
  }

  /** Selection while long-press-dragging over rows: what was selected before, plus the swept range. */
  function sweepSelection(order, base, anchorIdx, curIdx) {
    const sel = new Set(base);
    if (anchorIdx < 0 || curIdx < 0) return inOrder(order, sel);
    const lo = Math.min(anchorIdx, curIdx), hi = Math.max(anchorIdx, curIdx);
    for (let i = lo; i <= hi; i++) sel.add(order[i]);
    return inOrder(order, sel);
  }

  /** Drop selected paths that are no longer listed (after a refresh). */
  function pruneSelection(order, selected) {
    const have = new Set(order);
    return selected.filter((p) => have.has(p));
  }

  /** Scroll speed (px per frame) when the pointer is near the top / bottom edge of the list. */
  function autoScrollSpeed(y, top, bottom, edge, max) {
    edge = edge || 32;
    max = max || 18;
    if (bottom - top < edge * 2) return 0;
    if (y < top + edge) return -Math.ceil(max * Math.min(1, (top + edge - y) / edge));
    if (y > bottom - edge) return Math.ceil(max * Math.min(1, (y - (bottom - edge)) / edge));
    return 0;
  }

  // ---- display helpers -----------------------------------------------------
  function fmtBytes(n) {
    if (n === undefined || n === null) return "";
    if (n < 1024) return n + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let v = n / 1024, u = 0;
    while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
    return (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)) + " " + units[u];
  }

  return {
    archiveExt, isArchive, stripLastExt, baseName, suggestArchiveName, formatFits, describeSelection,
    applyClick, rangeBetween, bandHits, bandSelection, sweepSelection, pruneSelection, autoScrollSpeed, fmtBytes,
  };
});
