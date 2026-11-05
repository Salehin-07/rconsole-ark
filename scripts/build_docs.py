#!/usr/bin/env python3
"""Generate the generated parts of the docs and build the documentation website.

    python scripts/build_docs.py --write            regenerate the tables between GENERATED markers
    python scripts/build_docs.py --check            exit 1 if any generated block is out of date
    python scripts/build_docs.py --site site        build the static site (GitHub Pages) into ./site
    python scripts/build_docs.py --site site --strict   ... and fail on any broken link

The site has no second copy of the documentation: every page is one of the repository's Markdown files
(or one ``##`` section of README.md) rendered as it is, so the repo and the website cannot disagree.
Needs the ``markdown`` package (a dev dependency) for ``--site``.
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import envvars  # noqa: E402

REPO = "Salehin-07/rconsole-ark"
GH = f"https://github.com/{REPO}"
SITE_URL = "https://salehin-07.github.io/rconsole-ark/"

# ------------------------------------------------------------------ generated blocks
BEGIN = "<!-- BEGIN GENERATED: {} -->"
END = "<!-- END GENERATED: {} -->"


def _names_table():
    lines = ["| Area | Variables |", "|---|---|"]
    for _key, title, rows in envvars.by_area():
        lines.append(f"| {title} | " + ", ".join(f"`{r[1]}`" for r in rows) + " |")
    return "\n".join(lines) + "\n"


def blocks():
    return {"env-reference": envvars.full_reference(), "env-names": _names_table()}


TARGETS = {
    "README.md": ["env-reference"],
    "docs/configuration.md": ["env-reference"],
    "FEATURES.md": ["env-names"],
}


def render_file(rel):
    """The text of ``rel`` with its generated blocks regenerated (and the list of missing markers)."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    missing = []
    for key in TARGETS[rel]:
        b, e = BEGIN.format(key), END.format(key)
        if b not in text or e not in text:
            missing.append(key)
            continue
        head, rest = text.split(b, 1)
        _old, tail = rest.split(e, 1)
        text = f"{head}{b}\n\n{blocks()[key].strip()}\n\n{e}{tail}"
    return text, missing


def stale_files():
    out = []
    for rel in TARGETS:
        new, missing = render_file(rel)
        if missing:
            out.append((rel, f"missing markers: {missing}"))
        elif new != (ROOT / rel).read_text(encoding="utf-8"):
            out.append((rel, "generated block is out of date"))
    return out


# ------------------------------------------------------------------ site structure
# (slug, title, group, source, summary)   source: "FILE.md" or ("README.md", "## heading prefix")
PAGES = [
    ("index", "Overview", "Start here", "docs/index.md", "What Rconsole is, and what it is not."),
    ("quick-start", "Quick start", "Start here", "docs/quick-start.md", "Running in five minutes."),
    ("free-hosting", "Free hosting (Render)", "Start here", "FREE_HOSTING.md",
     "Bots, sites and workers on a free tier."),
    ("deploy", "Deploy anywhere", "Guides", "DEPLOY.md", "Render, Railway, Fly.io, VPS, Kubernetes and more."),
    ("vm", "Linux systems (VM)", "Guides", ("README.md", "## The VM"), "Debian, Ubuntu, Kali and Alpine per user."),
    ("hosting-apps", "Hosting apps", "Guides", ("README.md", "## Hosting web apps"), "expose and service."),
    ("console", "Using the console", "Guides", ("README.md", "## Using the console"), "Line mode and real programs."),
    ("ai-agent", "AI agent", "Guides", ("README.md", "## AI agent"), "sudo agent."),
    ("backup", "Backups and restore", "Guides", "docs/backup.md", "What is saved, what is not, and the limits."),
    ("upgrading", "Updating and rollback", "Guides", "docs/upgrading.md", "rconsole update, vm update, redeploys."),
    ("configuration", "Configuration", "Reference", "docs/configuration.md", "Every environment variable."),
    ("commands", "Command reference", "Reference", "COMMANDS.md", "Every command, option and example."),
    ("features", "Feature catalogue", "Reference", "FEATURES.md", "Every feature by area, with limits."),
    ("security", "Security model", "Reference", "docs/security.md", "Trust model, isolation and what is not covered."),
    ("architecture", "Architecture", "Reference", "docs/architecture.md", "Processes, state and the VM lifecycle."),
    ("compatibility", "Compatibility", "Reference", "docs/compatibility.md", "Python, OS, CPUs and PRoot."),
    ("troubleshooting", "Troubleshooting", "Reference", ("README.md", "## Troubleshooting"), "Common problems."),
    ("file-reference", "File reference", "Reference", "structure.md", "What each source file does."),
    ("changelog", "Changelog", "Reference", "CHANGELOG.md", "Release history."),
]
SOURCE_FILES = {"README.md": "index", "FREE_HOSTING.md": "free-hosting", "DEPLOY.md": "deploy",
                "COMMANDS.md": "commands", "FEATURES.md": "features", "CHANGELOG.md": "changelog",
                "structure.md": "file-reference", "SECURITY.md": "security",
                "docs/index.md": "index", "docs/quick-start.md": "quick-start", "docs/backup.md": "backup",
                "docs/upgrading.md": "upgrading", "docs/configuration.md": "configuration",
                "docs/security.md": "security", "docs/architecture.md": "architecture",
                "docs/compatibility.md": "compatibility"}


def slugify(value, _sep="-"):
    """GitHub-style heading slug (what the Markdown files' own anchors use)."""
    value = re.sub(r"<[^>]+>", "", value).strip().lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.U)
    return re.sub(r"\s", "-", value)


def split_sections(text):
    """[(heading_line, body_lines)] for the ``## `` sections of a Markdown file (fence-aware)."""
    sections, cur, fence = [], None, False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and line.startswith("## "):
            cur = [line]
            sections.append(cur)
        elif cur is not None:
            cur.append(line)
    return sections


def promote(lines):
    """Raise every heading of a README section by one level (``##`` becomes ``#``)."""
    out, fence = [], False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        out.append(line[1:] if not fence and re.match(r"^#{2,6} ", line) else line)
    return out


def load_source(src):
    if isinstance(src, tuple):
        fname, prefix = src
        text = (ROOT / fname).read_text(encoding="utf-8")
        for sec in split_sections(text):
            if sec[0].startswith(prefix):
                return "\n".join(promote(sec)).strip() + "\n"
        raise SystemExit(f"section {prefix!r} not found in {fname}")
    return (ROOT / src).read_text(encoding="utf-8")


def source_label(src):
    return src[0] if isinstance(src, tuple) else src


# ------------------------------------------------------------------ rendering
def md_to_html(md_text):
    import markdown
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "md_in_html", "sane_lists", "attr_list"],
        extension_configs={"toc": {"slugify": slugify, "toc_depth": "2-3"}},
        output_format="html",
    )
    body = md.convert(md_text)
    return body, getattr(md, "toc_tokens", [])


def _strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def build_pages():
    """Render every page; returns {slug: dict(title, group, body, toc, ids, source)}."""
    pages = {}
    for slug, title, group, src, summary in PAGES:
        body, toc = md_to_html(load_source(src))
        ids = set(re.findall(r'\sid="([^"]+)"', body))
        pages[slug] = {"slug": slug, "title": title, "group": group, "src": src, "summary": summary,
                       "body": body, "toc": toc, "ids": ids}
    return pages


def link_rewriter(pages):
    import posixpath
    anchor_page = {}
    for slug, p in pages.items():
        for i in p["ids"]:
            anchor_page.setdefault(i, slug)
    broken = []

    def fix(href, cur):
        if re.match(r"^(https?:|mailto:|tel:|data:)", href) or href.startswith("//"):
            return href
        path, _, frag = href.partition("#")
        suffix = f"#{frag}" if frag else ""
        if not path:                                    # "#anchor" in the same source file
            if frag in pages[cur]["ids"]:
                return f"#{frag}"
            tgt = anchor_page.get(frag)
            if tgt:
                return f"{tgt}.html#{frag}"
            broken.append((cur, href))
            return href
        src = pages[cur]["src"]
        srcdir = posixpath.dirname(source_label(src))
        if path.startswith("../../releases") or path.startswith("../../issues"):
            return f"{GH}/{path[6:]}{suffix}"          # README's GitHub-relative links
        norm = posixpath.normpath(posixpath.join(srcdir, path))
        if norm in SOURCE_FILES:
            target = SOURCE_FILES[norm]
            if frag:
                tgt = target if frag in pages[target]["ids"] else anchor_page.get(frag)
                if not tgt:
                    broken.append((cur, href))
                    return f"{target}.html"
                target = tgt
            elif norm == "README.md":
                target = "index"
            return f"{target}.html{suffix}"
        if (ROOT / norm).exists():                      # deploy/, render.yaml, examples/ ...
            kind = "tree" if (ROOT / norm).is_dir() else "blob"
            return f"{GH}/{kind}/main/{norm.rstrip('/')}{suffix}"
        broken.append((cur, href))
        return href
    return fix, broken


def rewrite_links(pages):
    fix, broken = link_rewriter(pages)
    for slug, p in pages.items():
        def repl(m, slug=slug):
            return f'href="{html.escape(fix(html.unescape(m.group(1)), slug), quote=True)}"'
        p["body"] = re.sub(r'href="([^"]*)"', repl, p["body"])
        p["body"] = re.sub(r'src="(?:\.\./)?static/([^"]+)"', r'src="assets/\1"', p["body"])
    return broken


def search_index(pages):
    entries = []
    for slug, p in pages.items():
        parts = re.split(r'(<h[123][^>]*\sid="[^"]+"[^>]*>.*?</h[123]>)', p["body"], flags=re.S)
        entries.append({"t": p["title"], "u": f"{slug}.html", "p": p["title"],
                        "x": re.sub(r"\s+", " ", _strip_tags(parts[0]))[:600]})
        for i in range(1, len(parts), 2):
            m = re.search(r'id="([^"]+)"', parts[i])
            text = re.sub(r"\s+", " ", _strip_tags(parts[i + 1] if i + 1 < len(parts) else ""))[:600]
            entries.append({"t": re.sub(r"\s+", " ", _strip_tags(parts[i])).strip(), "u": f"{slug}.html#{m.group(1)}",
                            "p": p["title"], "x": text})
    return entries


CSS = r"""
:root{--bg:#ffffff;--fg:#1b2430;--muted:#5b6776;--line:#e2e7ee;--side:#f6f8fb;--accent:#1f6feb;--accent-fg:#fff;
--code:#f1f4f8;--pre:#0f1722;--pre-fg:#e6edf6;--callout:#eef5ff;--callout-line:#1f6feb;--warn:#fff7e6;--warn-line:#d98e04;
--shadow:0 1px 2px rgba(16,24,40,.06),0 4px 14px rgba(16,24,40,.06)}
:root[data-theme=dark]{--bg:#0d1117;--fg:#dfe6ee;--muted:#9aa7b6;--line:#232c38;--side:#10161e;--accent:#58a6ff;--accent-fg:#0d1117;
--code:#18202b;--pre:#0a0e14;--pre-fg:#e6edf6;--callout:#112038;--callout-line:#58a6ff;--warn:#2b2110;--warn-line:#d9a441;--shadow:none}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0d1117;--fg:#dfe6ee;--muted:#9aa7b6;--line:#232c38;--side:#10161e;
--accent:#58a6ff;--accent-fg:#0d1117;--code:#18202b;--pre:#0a0e14;--pre-fg:#e6edf6;--callout:#112038;--callout-line:#58a6ff;--warn:#2b2110;
--warn-line:#d9a441;--shadow:none}}
*{box-sizing:border-box}html{scroll-padding-top:72px;-webkit-text-size-adjust:100%;overflow-x:hidden}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;height:56px;padding:0 max(12px,env(safe-area-inset-right)) 0 max(12px,env(safe-area-inset-left));background:var(--bg);
border-bottom:1px solid var(--line)}
header.top .brand{display:flex;align-items:center;gap:10px;font-weight:700;color:var(--fg);white-space:nowrap;flex:0 0 auto}
header.top .brand img{width:28px;height:28px;border-radius:6px}
header.top .brand small{font-weight:500;color:var(--muted)}
.spacer{flex:1}
#q{flex:0 1 340px;min-width:0;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--side);color:var(--fg);font:inherit;font-size:14px}
#q:focus{outline:2px solid var(--accent);outline-offset:0}
.btn{flex:0 0 auto;white-space:nowrap;border:1px solid var(--line);background:var(--side);color:var(--fg);border-radius:8px;padding:6px 10px;cursor:pointer;font:inherit;font-size:14px}
.btn:hover{border-color:var(--accent)}
#menu{display:none}
.wrap{display:grid;grid-template-columns:260px minmax(0,1fr) 220px;max-width:1380px;margin:0 auto}
nav.side{position:sticky;top:56px;align-self:start;height:calc(100vh - 56px);overflow:auto;padding:20px 12px 40px 16px;border-right:1px solid var(--line);background:var(--side)}
nav.side h4{margin:18px 8px 6px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
nav.side a{display:block;padding:6px 10px;border-radius:7px;color:var(--fg);font-size:14.5px}
nav.side a:hover{background:var(--line);text-decoration:none}
nav.side a.cur{background:var(--accent);color:var(--accent-fg);font-weight:600}
main{padding:32px clamp(18px,4vw,48px) 80px;min-width:0}
main h1{font-size:2rem;line-height:1.25;margin:0 0 .6em}
main h2{margin:2.1em 0 .6em;padding-bottom:.3em;border-bottom:1px solid var(--line);font-size:1.5rem;line-height:1.3}
main h3{margin:1.7em 0 .5em;font-size:1.2rem}main h4{margin:1.4em 0 .4em}
main p,main li{max-width:78ch}
main img{max-width:100%;height:auto}
main{overflow-x:hidden}
.cards{grid-template-columns:repeat(auto-fit,minmax(min(230px,100%),1fr))}
main p,main li,main h1,main h2,main h3,main h4{overflow-wrap:break-word}
main td,main th{overflow-wrap:normal;word-break:normal;min-width:5.5em}
code{background:var(--code);padding:.15em .4em;border-radius:5px;overflow-wrap:anywhere;font:0.88em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre{position:relative;background:var(--pre);color:var(--pre-fg);padding:14px 16px;border-radius:10px;overflow:auto;line-height:1.5;margin:1.1em 0}
pre code{background:none;padding:0;color:inherit;font-size:.86rem;overflow-wrap:normal}
pre{-webkit-overflow-scrolling:touch;max-width:100%}
.copy{position:absolute;top:8px;right:8px;opacity:.0;transition:.15s;font-size:12px;padding:3px 8px;background:#243142;color:#dfe6ee;border:0;border-radius:6px;cursor:pointer}
pre:hover .copy,.copy:focus{opacity:1}
@media(hover:none){.copy{opacity:.85}}
.tablewrap{overflow-x:auto;margin:1.1em 0}
table{border-collapse:collapse;width:100%;font-size:.93rem}
th,td{border:1px solid var(--line);padding:8px 11px;text-align:left;vertical-align:top}
th{background:var(--side)}
blockquote{margin:1.2em 0;padding:.7em 1.1em;background:var(--callout);border-left:4px solid var(--callout-line);border-radius:0 8px 8px 0}
blockquote p{margin:.4em 0}
hr{border:0;border-top:1px solid var(--line);margin:2em 0}
aside.toc{position:sticky;top:56px;align-self:start;max-height:calc(100vh - 56px);overflow:auto;padding:28px 12px 40px 0;font-size:13.5px}
aside.toc b{display:block;margin-bottom:8px;color:var(--muted);font-size:12px;letter-spacing:.06em;text-transform:uppercase}
aside.toc a{display:block;padding:3px 0 3px 10px;color:var(--muted);border-left:2px solid var(--line)}
aside.toc a.l3{padding-left:22px}aside.toc a:hover{color:var(--accent);text-decoration:none}
.pager{display:flex;justify-content:space-between;gap:12px;margin-top:56px;padding-top:20px;border-top:1px solid var(--line)}
.pager a{display:block;padding:10px 14px;border:1px solid var(--line);border-radius:10px;min-width:40%}
.pager small{display:block;color:var(--muted)}
.edit{margin-top:32px;font-size:14px;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin:1.4em 0}
.card{display:block;padding:16px 18px;border:1px solid var(--line);border-radius:12px;background:var(--side);color:var(--fg)}
.card:hover{border-color:var(--accent);text-decoration:none;box-shadow:var(--shadow)}
.card b{display:block;margin-bottom:4px}.card span{color:var(--muted);font-size:.92rem}
#results{position:absolute;top:50px;right:16px;width:min(520px,calc(100vw - 32px));max-height:70vh;overflow:auto;background:var(--bg);
border:1px solid var(--line);border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.25);display:none}
#results a{display:block;padding:10px 14px;color:var(--fg);border-bottom:1px solid var(--line)}
#results a:hover,#results a.sel{background:var(--side);text-decoration:none}
#results small{color:var(--muted);display:block;font-size:12.5px}
footer{padding:24px;text-align:center;color:var(--muted);font-size:13px;border-top:1px solid var(--line)}
@media(max-width:1100px){.wrap{grid-template-columns:240px minmax(0,1fr)}aside.toc{display:none}}
@media(max-width:780px){
#menu{display:inline-block;min-width:42px;min-height:40px}.wrap{display:block}
nav.side{position:fixed;left:0;top:56px;bottom:auto;width:min(310px,88vw);z-index:25;transform:translateX(-102%);transition:transform .18s;height:calc(100vh - 56px);height:calc(100dvh - 56px);align-self:auto;
-webkit-overflow-scrolling:touch;padding-bottom:calc(40px + env(safe-area-inset-bottom))}
nav.side a{padding:11px 12px;font-size:16px}
body.nav-open nav.side{transform:none;box-shadow:0 0 40px rgba(0,0,0,.35)}
body.nav-open{overflow:hidden}
#scrim{display:none;position:fixed;inset:56px 0 0 0;z-index:24;background:rgba(0,0,0,.45)}body.nav-open #scrim{display:block}
header.top .brand small,header.top a.btn{display:none}
header.top .brand span{font-size:15px}
#q{font-size:16px;padding:9px 12px}#theme{min-width:42px;min-height:40px}
main{padding:20px max(16px,env(safe-area-inset-left)) 60px}main h1{font-size:1.55rem}main h2{font-size:1.3rem}main h3{font-size:1.12rem}
body{font-size:16px}pre{padding:12px;margin-left:-4px;margin-right:-4px;border-radius:8px}pre code{font-size:.8rem}
.copy{opacity:.85}table{font-size:.86rem}th,td{padding:6px 8px}
.pager{flex-direction:column}.pager a{min-width:0}.pager a[style]{text-align:left!important}
#results{right:8px;left:8px;width:auto;top:52px;max-height:60vh}
}
@media(max-width:380px){header.top .brand span{display:none}main h1{font-size:1.4rem}}
@media(min-width:781px){#scrim{display:none!important}}
"""

JS = r"""
(function(){
var root=document.documentElement;
try{var t=localStorage.getItem('rc-theme');if(t)root.setAttribute('data-theme',t);}catch(e){}
document.getElementById('theme').onclick=function(){
  var dark=root.getAttribute('data-theme')==='dark'||(!root.getAttribute('data-theme')&&matchMedia('(prefers-color-scheme: dark)').matches);
  var n=dark?'light':'dark';root.setAttribute('data-theme',n);try{localStorage.setItem('rc-theme',n);}catch(e){}
};
document.getElementById('menu').onclick=function(){document.body.classList.toggle('nav-open');};
function closeNav(){document.body.classList.remove('nav-open');}
document.querySelector('main').addEventListener('click',closeNav);
document.getElementById('scrim').addEventListener('click',closeNav);
document.querySelectorAll('nav.side a').forEach(function(a){a.addEventListener('click',closeNav);});
addEventListener('keydown',function(e){if(e.key==='Escape')closeNav();});
addEventListener('resize',function(){if(innerWidth>780)closeNav();});
document.querySelectorAll('main table').forEach(function(tb){var w=document.createElement('div');w.className='tablewrap';tb.parentNode.insertBefore(w,tb);w.appendChild(tb);});
document.querySelectorAll('main pre').forEach(function(pre){
  var b=document.createElement('button');b.className='copy';b.type='button';b.textContent='Copy';
  b.onclick=function(){var txt=pre.querySelector('code')?pre.querySelector('code').innerText:pre.innerText;
    (navigator.clipboard?navigator.clipboard.writeText(txt):Promise.reject()).then(function(){b.textContent='Copied';setTimeout(function(){b.textContent='Copy';},1400);},
    function(){var r=document.createRange();r.selectNodeContents(pre);var s=getSelection();s.removeAllRanges();s.addRange(r);});};
  pre.appendChild(b);});
var idx=null,q=document.getElementById('q'),res=document.getElementById('results'),sel=-1;
function load(cb){if(idx)return cb();fetch('search.json').then(function(r){return r.json();}).then(function(j){idx=j;cb();}).catch(function(){idx=[];cb();});}
function esc(s){return s.replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function run(){var v=q.value.trim().toLowerCase();if(!v){res.style.display='none';return;}
  var words=v.split(/\s+/);
  var out=idx.map(function(e){var tl=e.t.toLowerCase(),xl=e.x.toLowerCase(),s=0;
    for(var i=0;i<words.length;i++){var w=words[i],a=tl.indexOf(w)>=0,b=xl.indexOf(w)>=0;if(!a&&!b)return null;s+=(a?5:0)+(b?1:0)+(tl===w?5:0);}
    return{e:e,s:s};}).filter(Boolean).sort(function(a,b){return b.s-a.s;}).slice(0,12);
  res.innerHTML=out.length?out.map(function(o){return '<a href="'+o.e.u+'">'+esc(o.e.t)+'<small>'+esc(o.e.p)+' — '+esc(o.e.x.slice(0,110))+'</small></a>';}).join(''):'<a>No results</a>';
  res.style.display='block';sel=-1;}
q.addEventListener('input',function(){load(run);});q.addEventListener('focus',function(){load(run);});
q.addEventListener('keydown',function(e){var as=res.querySelectorAll('a[href]');
  if(e.key==='ArrowDown'){sel=Math.min(as.length-1,sel+1);}else if(e.key==='ArrowUp'){sel=Math.max(0,sel-1);}
  else if(e.key==='Enter'&&as[Math.max(sel,0)]){location.href=as[Math.max(sel,0)].getAttribute('href');return;}
  else if(e.key==='Escape'){res.style.display='none';q.blur();return;}else return;
  as.forEach(function(a,i){a.classList.toggle('sel',i===sel);});e.preventDefault();});
document.addEventListener('click',function(e){if(!res.contains(e.target)&&e.target!==q)res.style.display='none';});
document.addEventListener('keydown',function(e){if(e.key==='/'&&document.activeElement!==q&&!/input|textarea/i.test(document.activeElement.tagName)){e.preventDefault();q.focus();}});
})();
"""

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · Rconsole docs</title>
<meta name="description" content="{desc}">
<meta name="theme-color" content="#0d1117">
<link rel="icon" href="assets/logo.png">
<link rel="stylesheet" href="assets/style.css">
</head><body>
<header class="top">
<button class="btn" id="menu" aria-label="Menu">☰</button>
<a class="brand" href="index.html"><img src="assets/logo.png" alt=""><span>Rconsole <small>docs · v{version}</small></span></a>
<span class="spacer"></span>
<input id="q" type="search" placeholder="Search docs  ( / )" autocomplete="off" aria-label="Search the documentation">
<button class="btn" id="theme" aria-label="Toggle theme" title="Toggle light/dark"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor"/></svg></button>
<a class="btn" href="{gh}">GitHub</a>
<div id="results"></div>
</header>
<div id="scrim"></div>
<div class="wrap">
<nav class="side" aria-label="Documentation">{nav}</nav>
<main>
{body}
<p class="edit"><a href="{gh}/blob/main/{srcfile}">Edit this page on GitHub</a></p>
<div class="pager">{prev}{nxt}</div>
</main>
<aside class="toc">{toc}</aside>
</div>
<footer>Rconsole {version} · MIT licensed · <a href="{gh}/releases">Releases</a> · <a href="changelog.html">Changelog</a></footer>
<script src="assets/app.js"></script>
</body></html>
"""


def _flatten_toc(tokens, out=None):
    out = [] if out is None else out
    for t in tokens:
        out.append(t)
        _flatten_toc(t.get("children", []), out)
    return out


def build_site(outdir, strict=False):
    import shutil
    from datetime import date  # noqa: F401  (kept for future "last built" use)
    version = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "version.py").read_text(encoding="utf-8")).group(1)
    pages = build_pages()
    broken = rewrite_links(pages)
    out = Path(outdir)
    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
    (out / "assets" / "style.css").write_text(CSS.strip() + "\n", encoding="utf-8")
    (out / "assets" / "app.js").write_text(JS.strip() + "\n", encoding="utf-8")
    shutil.copy(ROOT / "static" / "logo.png", out / "assets" / "logo.png")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    order = [p[0] for p in PAGES]
    groups = []
    for slug in order:
        g = pages[slug]["group"]
        if not groups or groups[-1][0] != g:
            groups.append((g, []))
        groups[-1][1].append(slug)

    def nav(cur):
        parts = []
        for g, slugs in groups:
            parts.append(f"<h4>{html.escape(g)}</h4>")
            for s in slugs:
                cls = ' class="cur"' if s == cur else ""
                parts.append(f'<a{cls} href="{s}.html">{html.escape(pages[s]["title"])}</a>')
        return "\n".join(parts)

    for i, slug in enumerate(order):
        p = pages[slug]
        toc = _flatten_toc(p["toc"])
        toc_html = ""
        if len(toc) > 2:
            toc_html = "<b>On this page</b>" + "".join(
                f'<a class="l{t["level"]}" href="#{t["id"]}">{html.escape(_strip_tags(t["name"]))}</a>'
                for t in toc if t["level"] in (2, 3))[:60000]
        prev = nxt = "<span></span>"
        if i > 0:
            q = pages[order[i - 1]]
            prev = f'<a href="{q["slug"]}.html"><small>← Previous</small>{html.escape(q["title"])}</a>'
        if i < len(order) - 1:
            q = pages[order[i + 1]]
            nxt = f'<a href="{q["slug"]}.html" style="text-align:right"><small>Next →</small>{html.escape(q["title"])}</a>'
        page = TEMPLATE.format(title=html.escape(p["title"]), desc=html.escape(p["summary"], quote=True),
                               version=version, gh=GH, nav=nav(slug), body=p["body"], toc=toc_html,
                               srcfile=source_label(p["src"]), prev=prev, nxt=nxt)
        (out / f"{slug}.html").write_text(page, encoding="utf-8")

    missing = TEMPLATE.format(title="Page not found", desc="Page not found", version=version, gh=GH, nav=nav(""),
                              body="<h1>Page not found</h1><p>That page does not exist. Try the search box, or "
                                   '<a href="index.html">start from the overview</a>.</p>',
                              toc="", srcfile="docs/index.md", prev="<span></span>", nxt="<span></span>")
    (out / "404.html").write_text(missing, encoding="utf-8")
    (out / "search.json").write_text(json.dumps(search_index(pages), ensure_ascii=False), encoding="utf-8")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}sitemap.xml\n", encoding="utf-8")
    urls = "".join(f"<url><loc>{SITE_URL}{s}.html</loc></url>" for s in order)
    (out / "sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        encoding="utf-8")
    return pages, broken


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="regenerate generated blocks in the Markdown files")
    ap.add_argument("--check", action="store_true", help="fail if a generated block is stale")
    ap.add_argument("--site", metavar="DIR", help="build the static site into DIR")
    ap.add_argument("--strict", action="store_true", help="with --site: fail on broken links")
    a = ap.parse_args(argv)
    rc = 0
    if a.write:
        for rel in TARGETS:
            text, missing = render_file(rel)
            if missing:
                print(f"{rel}: missing markers for {missing}", file=sys.stderr)
                rc = 1
                continue
            (ROOT / rel).write_text(text, encoding="utf-8")
            print(f"updated {rel}")
    if a.check:
        bad = stale_files()
        for rel, why in bad:
            print(f"{rel}: {why} (run: python scripts/build_docs.py --write)", file=sys.stderr)
        rc = rc or (1 if bad else 0)
    if a.site:
        pages, broken = build_site(a.site, a.strict)
        for cur, href in broken:
            print(f"broken link on {cur}: {href}", file=sys.stderr)
        print(f"built {len(pages)} pages into {a.site}")
        if broken and a.strict:
            rc = 1
    if not (a.write or a.check or a.site):
        ap.print_help()
    return rc


if __name__ == "__main__":
    sys.exit(main())
