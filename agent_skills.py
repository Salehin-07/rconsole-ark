"""Agent skills: folders with a ``SKILL.md`` that teach the agent a task, in the same format OpenCode and Claude Code use.

A skill is ``<dir>/<name>/SKILL.md`` with a small front matter (``name``, ``description``) followed by instructions;
the folder may also hold scripts, templates and references the instructions point to. Only the name and description
go into the prompt; the agent loads the full text with the ``skill`` tool when a task matches (or the person types
``/<name>``). Skills shipped with Rconsole (``bundled_skills/``, e.g. ``find-skills``) are always available and are
looked up last, so your own skill of the same name wins. Looked up (nearest first): ``.rconsole/skills``, ``.claude/skills``, ``.agents/skills`` and
``.opencode/skills`` in the working directory and its parents, then the same folders in the home directory.

Installing (``/skills add``) only downloads from GitHub; a skill is text the agent will follow, so install only
skills you trust. The model can never install one on its own.
"""
import os
import re
import shutil
import tempfile
import zipfile
from urllib.parse import urlsplit

import requests

import config

SKILL_DIRS = (".rconsole/skills", ".claude/skills", ".agents/skills", ".opencode/skills")
HOME_EXTRA = (".config/opencode/skills",)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SKILLS = 60
MAX_BODY = 40_000
CATALOG_CAP = 6000
ZIP_CAP = 60 * 1024 * 1024
FILE_CAP = 1024 * 1024
TREE_CAP = 8 * 1024 * 1024
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bundled_skills")
HOSTS = {"github.com", "www.github.com", "codeload.github.com", "raw.githubusercontent.com"}


class SkillError(Exception):
    pass


def parse(text):
    """``(meta, body)`` of a SKILL.md: a deliberately small front-matter reader (key: value, quotes, > and | blocks)."""
    text = text.lstrip("\ufeff")
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        key, lines = None, []
        for raw in m.group(1).split("\n") + ["x: x"]:
            kv = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", raw)
            if kv and not raw.startswith(" "):
                if key:
                    meta[key] = " ".join(x.strip() for x in lines if x.strip()).strip()
                key, val = kv.group(1).lower(), kv.group(2).strip()
                lines = [] if val in (">", "|", ">-", "|-") else [val.strip("\"'")]
            elif key:
                lines.append(raw)
    return meta, body.strip()


def _walk_dirs(ctx):
    """(host dir, scope) pairs to search, nearest first."""
    out, seen = [], set()
    cwd, n = ctx.cwd or "", 0
    while cwd and n < 6:
        for d in SKILL_DIRS:
            try:
                hp = ctx.path(f"{cwd.rstrip('/')}/{d}")
            except Exception:                                          # noqa: BLE001 - protected or unreachable
                continue
            if hp not in seen and os.path.isdir(hp):
                seen.add(hp)
                out.append((hp, "project"))
        parent = os.path.dirname(cwd.rstrip("/"))
        if parent == cwd or not parent or (not ctx.vm and parent == "/"):
            break
        cwd, n = parent, n + 1
    for d in SKILL_DIRS + HOME_EXTRA:
        try:
            hp = ctx.path("~/" + d)
        except Exception:                                              # noqa: BLE001
            continue
        if hp not in seen and os.path.isdir(hp):
            seen.add(hp)
            out.append((hp, "global"))
    if os.path.isdir(BUNDLED_DIR):                                     # shipped with Rconsole, lowest priority, read-only
        out.append((BUNDLED_DIR, "built-in"))
    return out


def discover(ctx):
    """``{name: {name, description, dir, file, scope}}`` of every skill visible from the working directory."""
    found = {}
    for base, scope in _walk_dirs(ctx):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for nm in names:
            d = os.path.join(base, nm)
            f = os.path.join(d, "SKILL.md")
            if not os.path.isfile(f):
                continue
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    meta, _b = parse(fh.read(6000))
            except OSError:
                continue
            name = (meta.get("name") or nm).strip().lower()
            if not NAME_RE.match(name):
                name = nm.lower()
                if not NAME_RE.match(name):
                    continue
            if name not in found and len(found) < MAX_SKILLS:
                found[name] = {"name": name, "description": (meta.get("description") or "").strip()[:300],
                               "dir": d, "file": f, "scope": scope}
    return found


def catalog(skills):
    """The prompt section: names and one-line descriptions only."""
    if not skills:
        return ""
    lines = ["# Skills",
             "Skills are saved instructions for particular tasks. When a task matches one, call the `skill` tool with its "
             "name BEFORE you start and follow it. The person can also run one with /<name>. Available:"]
    used = 0
    for s in skills.values():
        ln = f"- {s['name']}: {s['description'] or '(no description)'}"
        used += len(ln)
        if used > CATALOG_CAP:
            lines.append("- (more skills exist: /skills lists them)")
            break
        lines.append(ln)
    return "\n".join(lines)


def load(ctx, name):
    """``(body, base dir shown to the agent, extra files)`` of one skill; raises :class:`SkillError`."""
    skills = discover(ctx)
    s = skills.get((name or "").strip().lower().lstrip("/"))
    if not s:
        raise SkillError("no skill named '%s'. Available: %s" % (name, ", ".join(skills) or "none"))
    with open(s["file"], encoding="utf-8", errors="replace") as fh:
        _meta, body = parse(fh.read(MAX_BODY + 1))
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n[skill text cut here]"
    files = []
    for root, dirs, fs in os.walk(s["dir"]):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(fs):
            rel = os.path.relpath(os.path.join(root, f), s["dir"]).replace(os.sep, "/")
            if rel != "SKILL.md":
                files.append(rel)
        if len(files) >= 60:
            break
    try:
        shown = ctx.virt(s["dir"])
    except Exception:                                                  # noqa: BLE001 - bundled folder is outside the workspace
        shown = s["dir"]
    return body, shown, files[:60]


# ---------------------------------------------------------------------------------------------- install
def _target_root(ctx, project):
    return ctx.path(f"{(ctx.cwd or '.').rstrip('/')}/.rconsole/skills") if project else ctx.path("~/.rconsole/skills")


def _parse_source(src):
    """-> (owner, repo, ref or None, subpath, raw_url or None)."""
    src = src.strip().rstrip("/")
    if re.match(r"^https?://", src):
        u = urlsplit(src)
        if (u.hostname or "").lower() not in HOSTS:
            raise SkillError("only GitHub sources are supported (github.com/owner/repo)")
        parts = [p for p in u.path.split("/") if p]
        if u.hostname == "raw.githubusercontent.com":
            return None, None, None, "", src
        if len(parts) < 2:
            raise SkillError("that does not look like a repository address")
        owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
        if len(parts) >= 5 and parts[2] in ("tree", "blob"):
            ref, sub = parts[3], "/".join(parts[4:])
            if parts[2] == "blob":
                return None, None, None, "", f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{sub}"
            return owner, repo, ref, sub, None
        return owner, repo, None, "", None
    parts = [p for p in src.split("/") if p]
    if len(parts) < 2 or not all(re.match(r"^[\w.@-]+$", p) for p in parts):
        raise SkillError("use owner/repo, owner/repo/path/to/skill or a github.com link")
    return parts[0], parts[1], None, "/".join(parts[2:]), None


def _clean_name(raw):
    n = re.sub(r"[^a-z0-9_-]+", "-", (raw or "").lower()).strip("-_")
    return n if NAME_RE.match(n or "") else ""


def _install_tree(files, dest_root, name):
    """Write {relative path: bytes} to dest_root/name, replacing an earlier copy only when everything was written."""
    os.makedirs(dest_root, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".skill-", dir=dest_root)            # same filesystem, so the final rename is atomic
    try:
        for rel, data in files.items():
            parts = [p for p in rel.split("/") if p]
            if not parts or any(p in (".", "..") for p in parts) or rel.startswith("/"):
                raise SkillError("unsafe path in the skill: " + rel)
            out = os.path.join(tmp, *parts)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(data)
        final = os.path.join(dest_root, name)
        if os.path.isdir(final):
            shutil.rmtree(final)
        os.replace(tmp, final)
        tmp = None
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def install(ctx, source, wanted=(), project=False):
    """Download skills from GitHub into ``~/.rconsole/skills`` (or the project). Returns the installed names."""
    owner, repo, ref, sub, raw = _parse_source(source)
    dest = _target_root(ctx, project)
    headers = {"User-Agent": "Rconsole-agent"}
    if raw:                                                            # a single SKILL.md
        r = requests.get(raw, headers=headers, timeout=(8, 25))
        if r.status_code != 200 or len(r.content) > FILE_CAP:
            raise SkillError(f"could not download that file (HTTP {r.status_code})")
        meta, _b = parse(r.text)
        name = _clean_name(meta.get("name") or raw.rstrip("/").split("/")[-2])
        if not name:
            raise SkillError("the file has no usable name; add `name:` to its front matter")
        _install_tree({"SKILL.md": r.content}, dest, name)
        return [name]
    url = f"https://codeload.github.com/{owner}/{repo}/zip/" + (ref or "HEAD")
    fd, zpath = tempfile.mkstemp(suffix=".zip", dir=config.DATA_DIR if os.path.isdir(config.DATA_DIR) else None)
    os.close(fd)
    try:
        with requests.get(url, headers=headers, stream=True, timeout=(8, 40)) as r:
            if r.status_code == 404:
                raise SkillError("repository (or branch) not found, or it is private")
            if r.status_code != 200:
                raise SkillError(f"GitHub answered HTTP {r.status_code}")
            size = 0
            with open(zpath, "wb") as fh:
                for chunk in r.iter_content(262144):
                    size += len(chunk)
                    if size > ZIP_CAP:
                        raise SkillError("that repository is too large to download here")
                    fh.write(chunk)
        try:
            zf = zipfile.ZipFile(zpath)
        except zipfile.BadZipFile:
            raise SkillError("GitHub did not return a valid archive")
        with zf:
            infos = {i.filename.split("/", 1)[1]: i for i in zf.infolist() if "/" in i.filename and not i.is_dir()}
            sub = sub.strip("/")
            cands = sorted({os.path.dirname(p) for p in infos if os.path.basename(p) == "SKILL.md"
                            and (not sub or p == sub + "/SKILL.md" or os.path.dirname(p) == sub or os.path.dirname(p).startswith(sub + "/"))})
            if not cands:
                raise SkillError("no SKILL.md found there")
            want = {w.lower() for w in wanted}
            if want:
                cands = [c for c in cands if os.path.basename(c).lower() in want or _clean_name(os.path.basename(c)) in want]
                if not cands:
                    raise SkillError("none of those skills exist in that repository")
            elif len(cands) > 6:
                raise SkillError(f"{len(cands)} skills found. Pick some: /skills add {source} <name> [<name>...]. "
                                 "Some: " + ", ".join(os.path.basename(c) or repo for c in cands[:14]))
            done = []
            for c in cands:
                prefix = c + "/" if c else ""
                tree, total = {}, 0
                for p, i in infos.items():
                    if not p.startswith(prefix) or (i.external_attr >> 28) == 0xA:        # skip symlinks
                        continue
                    if i.file_size > FILE_CAP:
                        continue
                    total += i.file_size
                    if total > TREE_CAP or len(tree) >= 200:
                        raise SkillError("a skill folder is too large")
                    tree[p[len(prefix):]] = zf.read(i)
                meta, _b = parse(tree.get("SKILL.md", b"").decode("utf-8", "replace"))
                name = _clean_name(meta.get("name") or os.path.basename(c) or repo)
                if not name:
                    continue
                _install_tree(tree, dest, name)
                done.append(name)
            if not done:
                raise SkillError("could not install anything from there")
            return done
    except requests.RequestException as e:
        raise SkillError(f"could not reach GitHub ({e.__class__.__name__})")
    finally:
        try:
            os.remove(zpath)
        except OSError:
            pass


def remove(ctx, name):
    s = discover(ctx).get((name or "").strip().lower())
    if not s:
        raise SkillError(f"no skill named '{name}'")
    if s["scope"] == "built-in":
        raise SkillError(f"'{name}' ships with Rconsole and cannot be removed (a skill of your own with that name overrides it)")
    shutil.rmtree(s["dir"])
    return s["name"]


def create(ctx, name, description="", project=False):
    name = _clean_name(name)
    if not name:
        raise SkillError("use a short name of letters, digits, - and _")
    root = _target_root(ctx, project)
    d = os.path.join(root, name)
    if os.path.exists(d):
        raise SkillError(f"a skill named '{name}' already exists")
    os.makedirs(d)
    desc = (description or "Describe in one sentence what this skill does and when to use it.").replace("\n", " ")
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\nWrite the steps the agent should follow here.\n"
                 "Put helper scripts or reference files next to this file and mention them by relative path.\n")
    return d
