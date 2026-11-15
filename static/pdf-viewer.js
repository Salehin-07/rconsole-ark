"use strict";
// PDF viewer for the file browser (uses the bundled pdf.js in static/vendor/pdfjs).
//
// Why not the browser's own viewer: a big PDF showed a blank page until it had been downloaded completely. This one
//  - shows "Opening ... / Loading page N" and a download bar from the first moment,
//  - asks the server for byte ranges only (/api/media supports Range), so it loads the START of the file first and
//    then only what the pages being looked at need - jump to page 800 and it fetches page 800 first, not 1..799,
//  - draws only the pages on (and next to) the screen, nearest to the middle first, and frees the ones far away.
(function () {
  const VENDOR = "/static/vendor/pdfjs/";
  let libPromise = null;

  function loadLib(v) {
    if (window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
    if (!libPromise) {
      libPromise = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = VENDOR + "pdf.min.js" + (v ? "?v=" + v : "");
        s.onload = () => {
          if (!window.pdfjsLib) { libPromise = null; reject(new Error("pdf.js did not start")); return; }
          window.pdfjsLib.GlobalWorkerOptions.workerSrc = VENDOR + "pdf.worker.min.js" + (v ? "?v=" + v : "");
          resolve(window.pdfjsLib);
        };
        s.onerror = () => { libPromise = null; reject(new Error("could not load the PDF engine")); };
        document.head.appendChild(s);
      });
    }
    return libPromise;
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function mb(n) { return (n / 1048576).toFixed(n >= 10485760 ? 0 : 1) + " MB"; }

  // open(stage, url, {version, onFallback(message)}) -> {destroy()}
  function open(stage, url, opts) {
    opts = opts || {};
    const root = el("div", "pdfv");
    const bar = el("div", "pdfv-bar");
    const prev = el("button", "", "\u2039"); prev.type = "button"; prev.title = "Previous page"; prev.setAttribute("aria-label", "Previous page");
    const next = el("button", "", "\u203A"); next.type = "button"; next.title = "Next page"; next.setAttribute("aria-label", "Next page");
    const pageIn = el("input", "pdfv-page"); pageIn.type = "text"; pageIn.inputMode = "numeric"; pageIn.value = "1"; pageIn.setAttribute("aria-label", "Page number");
    const total = el("span", "pdfv-total", "/ \u2026");
    const zout = el("button", "", "\u2212"); zout.type = "button"; zout.title = "Zoom out"; zout.setAttribute("aria-label", "Zoom out");
    const zlabel = el("span", "pdfv-zoom", "100%");
    const zin = el("button", "", "+"); zin.type = "button"; zin.title = "Zoom in"; zin.setAttribute("aria-label", "Zoom in");
    const spacer = el("span", "pdfv-spacer");
    const newtab = el("button", "pdfv-link", "Open in new tab"); newtab.type = "button";
    newtab.onclick = () => window.open(url, "_blank", "noopener");
    bar.append(prev, pageIn, total, next, spacer, zout, zlabel, zin, newtab);
    const progress = el("div", "pdfv-progress");
    const fill = el("div", "pdfv-progress-fill");
    progress.appendChild(fill);
    const scroll = el("div", "pdfv-scroll");
    scroll.tabIndex = 0;
    const status = el("div", "pdfv-status");
    const spin = el("span", "pdfv-spin");
    const statusText = el("span", "", "Opening PDF\u2026");
    status.append(spin, statusText);
    const pagesBox = el("div", "pdfv-pages");
    scroll.append(status, pagesBox);
    root.append(bar, progress, scroll);
    stage.appendChild(root);

    let dead = false, task = null, doc = null, io = null, ro = null, resizeT = 0, raf = 0;
    const pages = [];                 // {num, wrap, w, h, state, token, rt}
    const wanted = new Set();
    let zoom = 1, fitW = 0, w1 = 612, h1 = 792, current = 1, running = 0;

    function fail(msg) {
      if (dead) return;
      destroy();
      if (opts.onFallback) opts.onFallback(msg);
    }
    function setStatus(text) { statusText.textContent = text; status.hidden = !text; }

    function cssScale() { return Math.max(0.1, ((fitW || scroll.clientWidth) - 20) / w1 * zoom); }
    function sizePage(p) {
      const s = cssScale();
      p.wrap.style.width = Math.floor(p.w * s) + "px";
      p.wrap.style.height = Math.floor(p.h * s) + "px";
    }
    function layoutAll() {
      fitW = scroll.clientWidth;
      for (const p of pages) sizePage(p);
      zlabel.textContent = Math.round(zoom * 100) + "%";
    }
    function release(p) {
      p.token++;
      if (p.rt) { try { p.rt.cancel(); } catch (_) {} p.rt = null; }
      p.state = "empty";
      p.wrap.textContent = "";
      p.wrap.classList.remove("done");
    }

    async function render(p) {
      p.state = "rendering"; running++;
      const token = ++p.token;
      const spinner = el("div", "pdfv-pagewait");
      spinner.append(el("span", "pdfv-spin"), el("span", "", "Loading page " + p.num + "\u2026"));
      p.wrap.textContent = "";
      p.wrap.appendChild(spinner);
      try {
        const page = await doc.getPage(p.num);
        if (dead || token !== p.token) return;
        const base = page.getViewport({ scale: 1 });
        if (Math.abs(base.width - p.w) > 1 || Math.abs(base.height - p.h) > 1) { p.w = base.width; p.h = base.height; }
        const s = cssScale();
        p.wrap.style.width = Math.floor(p.w * s) + "px";
        p.wrap.style.height = Math.floor(p.h * s) + "px";
        let dpr = Math.min(window.devicePixelRatio || 1, 2);
        const px = p.w * s * dpr * p.h * s * dpr;
        if (px > 14e6) dpr *= Math.sqrt(14e6 / px);            // keep the canvas inside what phones can allocate
        const vp = page.getViewport({ scale: s * dpr });
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.floor(vp.width));
        canvas.height = Math.max(1, Math.floor(vp.height));
        canvas.style.width = "100%";
        canvas.style.height = "100%";
        p.rt = page.render({ canvasContext: canvas.getContext("2d"), viewport: vp });
        await p.rt.promise;
        if (dead || token !== p.token) return;
        p.rt = null;
        p.wrap.textContent = "";
        p.wrap.appendChild(canvas);
        p.wrap.classList.add("done");
        p.state = "done";
      } catch (e) {
        if (dead || token !== p.token) return;
        if (e && e.name === "RenderingCancelledException") return;
        p.state = "empty";
        p.wrap.textContent = "";
        const msg = el("div", "pdfv-pagewait", "Page " + p.num + " could not be drawn. ");
        const again = el("button", "", "Retry"); again.type = "button";
        again.onclick = () => { release(p); wanted.add(p.num); pump(); };
        msg.appendChild(again);
        p.wrap.appendChild(msg);
        p.state = "failed";
      } finally {
        running = Math.max(0, running - 1);            // one slot per drawing in flight, even a cancelled one
        pump();
      }
    }

    function pump() {
      if (dead || !doc) return;
      while (running < 2) {
        const mid = scroll.scrollTop + scroll.clientHeight / 2;
        let best = null, bestD = Infinity;
        for (const n of wanted) {
          const p = pages[n - 1];
          if (!p || p.state !== "empty") continue;
          const d = Math.abs(p.wrap.offsetTop + p.wrap.offsetHeight / 2 - mid);   // the page nearest the middle first
          if (d < bestD) { best = p; bestD = d; }
        }
        if (!best) return;
        render(best);
      }
    }

    function updateCurrent() {
      raf = 0;
      if (!pages.length) return;
      const y = scroll.scrollTop + scroll.clientHeight / 3;
      let lo = 0, hi = pages.length - 1;
      while (lo < hi) {                                           // binary search: pages are stacked in order
        const m = (lo + hi + 1) >> 1;
        if (pages[m].wrap.offsetTop <= y) lo = m; else hi = m - 1;
      }
      current = lo + 1;
      if (document.activeElement !== pageIn) pageIn.value = String(current);
    }
    scroll.addEventListener("scroll", () => { if (!raf) raf = requestAnimationFrame(updateCurrent); }, { passive: true });

    function goTo(n) {
      n = Math.min(Math.max(1, n | 0), pages.length || 1);
      const p = pages[n - 1];
      if (p) scroll.scrollTop = p.wrap.offsetTop - 8;
      current = n;
      pageIn.value = String(n);
    }
    prev.onclick = () => goTo(current - 1);
    next.onclick = () => goTo(current + 1);
    pageIn.addEventListener("keydown", (ev) => {
      ev.stopPropagation();
      if (ev.key === "Enter") { goTo(parseInt(pageIn.value, 10) || current); scroll.focus({ preventScroll: true }); }
      if (ev.key === "Escape") { pageIn.blur(); }
    });
    pageIn.addEventListener("blur", () => { pageIn.value = String(current); });

    function setZoom(z) {
      z = Math.min(4, Math.max(0.5, Math.round(z * 4) / 4));
      if (z === zoom) return;
      const ratio = scroll.scrollHeight ? scroll.scrollTop / scroll.scrollHeight : 0;
      zoom = z;
      for (const p of pages) if (p.state !== "empty") release(p);
      layoutAll();
      scroll.scrollTop = ratio * scroll.scrollHeight;
      updateCurrent();
      pump();                                          // the pages still in view are in `wanted`: they draw again
    }
    zin.onclick = () => setZoom(zoom + 0.25);
    zout.onclick = () => setZoom(zoom - 0.25);

    function destroy() {
      if (dead) return;
      dead = true;
      if (io) io.disconnect();
      if (ro) ro.disconnect();
      clearTimeout(resizeT);
      if (raf) cancelAnimationFrame(raf);
      for (const p of pages) if (p.rt) { try { p.rt.cancel(); } catch (_) {} }
      if (task) { try { task.destroy(); } catch (_) {} }
      wanted.clear();
    }

    function build(numPages) {
      total.textContent = "/ " + numPages;
      pagesBox.textContent = "";
      for (let i = 1; i <= numPages; i++) {
        const wrap = el("div", "pdfv-pagebox");
        wrap.dataset.page = String(i);
        pagesBox.appendChild(wrap);
        pages.push({ num: i, wrap, w: w1, h: h1, state: "empty", token: 0, rt: null });
      }
      layoutAll();
      io = new IntersectionObserver((entries) => {
        for (const en of entries) {
          const n = parseInt(en.target.dataset.page, 10);
          const p = pages[n - 1];
          if (en.isIntersecting) { wanted.add(n); }
          else { wanted.delete(n); if (p.state !== "empty") release(p); }   // far away: free the memory
        }
        pump();
      }, { root: scroll, rootMargin: "100% 0px 100% 0px" });
      for (const p of pages) io.observe(p.wrap);
      ro = new ResizeObserver(() => {
        clearTimeout(resizeT);
        resizeT = setTimeout(() => {
          if (dead || Math.abs(scroll.clientWidth - fitW) < 4) return;
          for (const p of pages) if (p.state !== "empty") release(p);
          layoutAll(); pump();
        }, 150);
      });
      ro.observe(scroll);
    }

    setStatus("Opening PDF\u2026");
    loadLib(opts.version).then((lib) => {
      if (dead) return;
      task = lib.getDocument({
        url, withCredentials: true,
        rangeChunkSize: 256 * 1024,
        disableAutoFetch: true,          // only fetch what the pages being looked at need
        disableStream: false,
        isEvalSupported: false,
        cMapUrl: VENDOR + "cmaps/", cMapPacked: true,
        standardFontDataUrl: VENDOR + "standard_fonts/",
      });
      task.onProgress = (pr) => {
        if (dead) return;
        if (pr && pr.total > 0) {
          const f = Math.min(1, pr.loaded / pr.total);
          fill.style.width = (f * 100).toFixed(1) + "%";
          if (!doc) setStatus("Opening PDF\u2026 " + mb(pr.loaded) + " of " + mb(pr.total));
        } else if (!doc && pr) {
          setStatus("Opening PDF\u2026 " + mb(pr.loaded || 0));
        }
      };
      task.onPassword = () => fail("This PDF is password-protected. Open it in a new tab to enter the password.");
      return task.promise;
    }).then(async (d) => {
      if (dead || !d) return;
      doc = d;
      const first = await d.getPage(1);                // page 1 first: its size lays out every placeholder
      if (dead) return;
      const vp = first.getViewport({ scale: 1 });
      w1 = vp.width; h1 = vp.height;
      setStatus("");
      build(d.numPages);
      updateCurrent();
    }).catch((e) => {
      if (dead) return;
      const n = e && e.name;
      if (n === "PasswordException") return;
      if (n === "InvalidPDFException") fail("This file doesn't look like a valid PDF.");
      else if (n === "MissingPDFException" || n === "UnexpectedResponseException") fail("The PDF could not be loaded from the server.");
      else fail((e && e.message) ? "The PDF viewer could not start: " + e.message : "The PDF viewer could not start.");
    });

    return { destroy };
  }

  window.RCPdf = { open };
})();
