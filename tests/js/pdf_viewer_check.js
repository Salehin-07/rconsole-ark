// Behaviour checks for static/pdf-viewer.js in jsdom with a stub pdf.js engine:
//   cd tests/js && npm install --no-save jsdom && node pdf_viewer_check.js
// Proves: a loading state shows at once, the page list is built from page 1's size, only pages on screen are
// requested/drawn (so a big file loads the part being looked at first), pages that leave the screen are freed,
// and a password-protected / broken PDF falls back instead of staying blank.
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.resolve(__dirname, "..", "..", "static", "pdf-viewer.js"), "utf8");
let failures = 0;
const check = (name, ok, extra = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  (" + extra + ")" : ""}`);
  if (!ok) failures++;
};
const tick = (ms = 10) => new Promise((r) => setTimeout(r, ms));

function setup(makeTask) {
  const dom = new JSDOM("<!doctype html><div id='stage'></div>", { runScripts: "outside-only", pretendToBeVisual: true,
    url: "http://localhost/" });
  const w = dom.window;
  const ios = [];
  w.IntersectionObserver = class { constructor(cb) { this.cb = cb; ios.push(this); } observe() {} disconnect() {} };
  w.ResizeObserver = class { observe() {} disconnect() {} };
  const calls = { getPage: [], destroyed: 0, opts: null };
  w.pdfjsLib = {
    GlobalWorkerOptions: {},
    getDocument(opts) { calls.opts = opts; return makeTask(calls); },
  };
  w.eval(src);
  return { w, ios, calls, stage: w.document.getElementById("stage") };
}

function goodTask(calls, pages) {
  const doc = {
    numPages: pages,
    async getPage(n) {
      calls.getPage.push(n);
      return {
        getViewport: ({ scale }) => ({ width: 612 * scale, height: 792 * scale }),
        render: () => ({ promise: Promise.resolve(), cancel() {} }),
      };
    },
  };
  let resolve;
  const task = { promise: new Promise((r) => { resolve = r; }), destroy() { calls.destroyed++; }, onProgress: null };
  task.resolve = () => resolve(doc);
  return task;
}

(async () => {
  // 1. loading state, progress, page list
  {
    let task;
    const { w, ios, calls, stage } = setup((c) => (task = goodTask(c, 500)));
    let fell = null;
    const v = w.RCPdf.open(stage, "/api/media?path=x", { onFallback: (m) => { fell = m; } });
    check("a loading message is on screen immediately", /Opening PDF/.test(stage.textContent));
    await tick();
    check("opens with range requests only (no auto-fetch of the whole file)", calls.opts.disableAutoFetch === true && calls.opts.rangeChunkSize > 0);
    check("the session cookie is sent with the requests", calls.opts.withCredentials === true);
    task.onProgress({ loaded: 3 * 1048576, total: 80 * 1048576 });
    check("download progress is shown while it opens", /3\.0 MB of 80 MB/.test(stage.textContent), stage.textContent.slice(0, 80));
    task.resolve();
    await tick(30);
    check("one placeholder per page is built from page 1's size", stage.querySelectorAll(".pdfv-pagebox").length === 500);
    check("the page count is shown", /500/.test(stage.querySelector(".pdfv-total").textContent));
    check("the loading message is gone once the document is open", !/Opening PDF/.test(stage.textContent));
    check("only page 1 was read to lay out the list", calls.getPage.join() === "1", calls.getPage.join());

    // 2. the start of the file first: page 1 and 2 visible
    const io = ios[0];
    const box = (n) => stage.querySelector(`.pdfv-pagebox[data-page="${n}"]`);
    io.cb([{ target: box(1), isIntersecting: true }, { target: box(2), isIntersecting: true }]);
    await tick(30);
    check("pages on screen are drawn", box(1).querySelector("canvas") && box(2).querySelector("canvas"));
    check("pages that are not on screen are never requested", calls.getPage.every((n) => n <= 2), calls.getPage.join());

    // 3. jump far into the file: that part is fetched, the middle is not
    io.cb([{ target: box(1), isIntersecting: false }, { target: box(2), isIntersecting: false },
           { target: box(400), isIntersecting: true }, { target: box(401), isIntersecting: true }]);
    await tick(30);
    check("the part being looked at is drawn", box(400).querySelector("canvas") && box(401).querySelector("canvas"));
    check("pages in between were never read", !calls.getPage.some((n) => n > 2 && n < 400), calls.getPage.join());
    check("pages that left the screen are freed", !box(1).querySelector("canvas") && !box(2).querySelector("canvas"));

    // 4. page box and zoom
    const input = stage.querySelector(".pdfv-page");
    check("the page box exists and holds a page number", !!input && /^\d+$/.test(input.value));
    stage.querySelector("button[title='Zoom in']").click();
    check("zoom in changes the zoom label", stage.querySelector(".pdfv-zoom").textContent === "125%");

    v.destroy();
    check("closing the viewer destroys the document (frees the worker)", calls.destroyed === 1);
    check("no fallback was needed", fell === null);
  }

  // 5. password-protected PDF
  {
    let task;
    const { w, calls, stage } = setup((c) => (task = { promise: new Promise(() => {}), destroy() { c.destroyed++; }, onProgress: null }));
    let fell = null;
    w.RCPdf.open(stage, "/api/media?path=x", { onFallback: (m) => { fell = m; } });
    await tick();
    task.onPassword();
    check("a password-protected PDF falls back with an explanation", /password/.test(fell || ""), fell);
    check("... and the engine is released", calls.destroyed === 1);
  }

  // 6. damaged file
  {
    const { w, stage } = setup((c) => ({ promise: Promise.reject(Object.assign(new Error("bad"), { name: "InvalidPDFException" })), destroy() { c.destroyed++; } }));
    let fell = null;
    w.RCPdf.open(stage, "/api/media?path=x", { onFallback: (m) => { fell = m; } });
    await tick(30);
    check("a damaged PDF falls back instead of staying blank", /valid PDF/.test(fell || ""), fell);
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
