"""Agent sessions on disk: cheap to write, cheap to list, cheap to keep in memory.

Layout (per user, under ``<data dir>/agent/users/<user>/``)::

    sessions/<id>.jsonl      one JSON object per line, append-only
    index.json               {id: summary} so the session list never reads transcripts
    checkpoints/<id>/<n>.json  file contents before the agent changed them in turn n (``/undo``)

A transcript line is ``{"k": "meta", "v": {...}}`` (latest values win), ``{"k": "m", "v": <message>}``,
``{"k": "compact", "v": {"upto": n, "summary": "..."}}``. Appending one short line per message means a long
session costs the same to save as a short one, and a crash can lose at most the line being written. The model
context is *derived* from the transcript (see ``agent_core.build_context``); the transcript itself is never
rewritten, so the history shown to the person stays complete after the context was compacted.
"""
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections import OrderedDict

import config

_ID = re.compile(r"^[a-z0-9]{6,20}$")
_lock = threading.RLock()
_cache = OrderedDict()            # (user, id) -> Session; a handful at a time, evicted least recently used
CACHE_MAX = 4
KEEP_CHECKPOINTS = 15
CP_FILE_MAX = 400_000             # a file larger than this cannot be undone
CP_TURN_MAX = 2_000_000

META_DEFAULTS = {"title": "New chat", "cwd": "", "provider": "", "model": "", "mode": "", "created": 0, "updated": 0,
                 "turns": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "ctx_tokens": 0, "allow": [],
                 "prune_upto": 0, "todos": [], "target": ""}


def _safe(name):
    s = re.sub(r"[^A-Za-z0-9._-]", "_", str(name))
    return "_" + s if s.startswith(".") or not s else s


def user_dir(user):
    return config.DATA_DIR / "agent" / "users" / _safe(user)


def valid_id(sid):
    return isinstance(sid, str) and bool(_ID.match(sid))


def _spath(user, sid):
    return user_dir(user) / "sessions" / f"{sid}.jsonl"


class Session:
    def __init__(self, user, sid):
        self.user, self.id = user, sid
        self.meta = {k: (list(v) if isinstance(v, list) else v) for k, v in META_DEFAULTS.items()}
        self.messages = []
        self.compact = None               # {"upto": n, "summary": "..."} - messages[:upto] are summarised in the context
        self.lock = threading.RLock()
        self.runtime = {}                 # not saved: files read this run, etc.

    # ---- persistence
    def _append(self, obj):
        p = _spath(self.user, self.id)
        config.ensure_private_dir(p.parent)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    def add(self, msg):
        with self.lock:
            self.messages.append(msg)
            self._append({"k": "m", "v": msg})
            self.meta["updated"] = time.time()
        return len(self.messages) - 1

    def set_meta(self, **kv):
        with self.lock:
            changed = {}
            for k, v in kv.items():
                if k in META_DEFAULTS and self.meta.get(k) != v:
                    self.meta[k] = v
                    changed[k] = v
            if changed:
                self.meta["updated"] = time.time()
                changed["updated"] = self.meta["updated"]
                self._append({"k": "meta", "v": changed})
        index_put(self)

    def set_compact(self, upto, summary):
        with self.lock:
            self.compact = {"upto": int(upto), "summary": summary}
            self._append({"k": "compact", "v": self.compact})

    def touch(self):
        index_put(self)

    # ---- views
    def summary(self):
        m = self.meta
        first = next((x.get("content", "") for x in self.messages if x.get("role") == "user"), "")
        return {"id": self.id, "title": m["title"], "cwd": m["cwd"], "provider": m["provider"], "model": m["model"],
                "mode": m["mode"], "created": m["created"], "updated": m["updated"], "turns": m["turns"],
                "tokens": m["tokens_in"] + m["tokens_out"], "preview": " ".join(first.split())[:140]}

    def transcript(self, start=0):
        """Messages for the browser (tool results already clipped for display when stored)."""
        with self.lock:
            return [dict(m, i=i) for i, m in enumerate(self.messages) if i >= start]


def _load_file(user, sid):
    p = _spath(user, sid)
    if not p.is_file():
        return None
    s = Session(user, sid)
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue                         # a half-written last line
                k = o.get("k")
                if k == "m" and isinstance(o.get("v"), dict):
                    s.messages.append(o["v"])
                elif k == "meta" and isinstance(o.get("v"), dict):
                    s.meta.update({a: b for a, b in o["v"].items() if a in META_DEFAULTS})
                elif k == "compact" and isinstance(o.get("v"), dict):
                    s.compact = o["v"]
    except OSError:
        return None
    return s


def get(user, sid):
    """The session (cached), or ``None``."""
    if not valid_id(sid):
        return None
    key = (user, sid)
    with _lock:
        s = _cache.get(key)
        if s is not None:
            _cache.move_to_end(key)
            return s
    s = _load_file(user, sid)
    if s is None:
        return None
    with _lock:
        _cache[key] = s
        while len(_cache) > CACHE_MAX:
            old = next((k for k, v in _cache.items() if not v.runtime.get("busy")), None)
            if old is None:
                break
            _cache.pop(old, None)
    return s


def create(user, cwd="", **meta):
    sid = secrets.token_hex(5)
    s = Session(user, sid)
    now = time.time()
    s.meta.update(created=now, updated=now, cwd=cwd)
    s.meta.update({k: v for k, v in meta.items() if k in META_DEFAULTS and v is not None})
    config.ensure_private_dir(_spath(user, sid).parent)
    s._append({"k": "meta", "v": {k: s.meta[k] for k in ("created", "updated", "cwd", "provider", "model", "mode", "target", "title")}})
    with _lock:
        _cache[(user, sid)] = s
        while len(_cache) > CACHE_MAX:
            old = next((k for k, v in _cache.items() if not v.runtime.get("busy") and k != (user, sid)), None)
            if old is None:
                break
            _cache.pop(old, None)
    index_put(s)
    return s


# ------------------------------------------------------------------------------------------- index
def _ipath(user):
    return user_dir(user) / "index.json"


def _read_index(user):
    try:
        d = json.loads(_ipath(user).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_index(user, idx):
    p = _ipath(user)
    config.ensure_private_dir(p.parent)
    tmp = p.with_name("index.json.tmp")
    tmp.write_text(json.dumps(idx, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, p)


def index_put(s):
    with _lock:
        idx = _read_index(s.user)
        if idx is None:
            idx = _rebuild(s.user)
        idx[s.id] = s.summary()
        _write_index(s.user, idx)


def _rebuild(user):
    idx = {}
    d = user_dir(user) / "sessions"
    try:
        names = [n[:-6] for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        names = []
    for sid in names:
        s = _load_file(user, sid)
        if s:
            idx[sid] = s.summary()
    return idx


def list_sessions(user, query=""):
    with _lock:
        idx = _read_index(user)
        if idx is None:
            idx = _rebuild(user)
            if idx:
                _write_index(user, idx)
    rows = sorted(idx.values(), key=lambda r: r.get("updated", 0), reverse=True)
    q = (query or "").strip().lower()
    if q:
        rows = [r for r in rows if q in (r.get("title", "") + " " + r.get("preview", "")).lower()]
    return rows


def delete(user, sid):
    if not valid_id(sid):
        return False
    with _lock:
        _cache.pop((user, sid), None)
        ok = False
        try:
            os.remove(_spath(user, sid))
            ok = True
        except OSError:
            pass
        shutil.rmtree(user_dir(user) / "checkpoints" / sid, ignore_errors=True)
        idx = _read_index(user)
        if idx is not None and sid in idx:
            del idx[sid]
            _write_index(user, idx)
    return ok


def fork(user, sid, upto=None):
    """Copy a session (up to message index ``upto``, exclusive) into a new one."""
    src = get(user, sid)
    if src is None:
        return None
    with src.lock:
        msgs = list(src.messages if upto is None else src.messages[:max(0, int(upto))])
        meta = dict(src.meta)
    new = create(user, meta["cwd"], provider=meta["provider"], model=meta["model"], mode=meta["mode"],
                 target=meta["target"], title=(meta["title"] + " (fork)")[:80])
    for m in msgs:
        new.add(m)
    new.set_meta(turns=sum(1 for m in msgs if m.get("role") == "user"), allow=list(meta.get("allow") or []))
    return new


def clear_all(user):
    shutil.rmtree(user_dir(user), ignore_errors=True)
    with _lock:
        for k in [k for k in _cache if k[0] == user]:
            _cache.pop(k, None)


# ------------------------------------------------------------------------------------ checkpoints
def cp_dir(user, sid):
    return user_dir(user) / "checkpoints" / sid


def cp_save(user, sid, turn, files):
    """Remember the files as they were before turn ``turn`` changed them. ``files``: list of dicts."""
    if not files or not valid_id(sid):
        return
    d = cp_dir(user, sid)
    config.ensure_private_dir(d)
    p = d / f"{int(turn):06d}.json"
    existing = []
    try:
        existing = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    seen = {f["host"] for f in existing}
    existing += [f for f in files if f["host"] not in seen]
    p.write_text(json.dumps(existing), encoding="utf-8")
    olds = sorted(os.listdir(d))
    for n in olds[:-KEEP_CHECKPOINTS]:
        try:
            os.remove(d / n)
        except OSError:
            pass


def cp_pop_last(user, sid):
    """Remove and return the newest checkpoint's file list (or ``None``)."""
    d = cp_dir(user, sid)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return None
    if not names:
        return None
    p = d / names[-1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        os.remove(p)
    except (OSError, ValueError):
        return None
    return data


def cp_count(user, sid):
    try:
        return len(os.listdir(cp_dir(user, sid)))
    except OSError:
        return 0


# -------------------------------------------------------------------------------------- export
def export_markdown(s):
    out = [f"# {s.meta['title']}", "",
           f"_{time.strftime('%Y-%m-%d %H:%M', time.localtime(s.meta['created'] or time.time()))} · "
           f"{s.meta['provider']} / {s.meta['model']} · {s.meta['cwd']}_", ""]
    for m in s.messages:
        r = m.get("role")
        if r == "user":
            out += ["## You", "", m.get("content", ""), ""]
        elif r == "assistant":
            if m.get("content"):
                out += ["## Agent", "", m["content"], ""]
            for c in m.get("tool_calls") or []:
                out += [f"- `{c['name']}` {json.dumps(c.get('args') or {}, ensure_ascii=False)[:300]}"]
            if m.get("tool_calls"):
                out.append("")
        elif r == "tool":
            ui = m.get("ui") or {}
            body = (ui.get("out") or m.get("content") or "")[:1500]
            out += ["<details><summary>" + (ui.get("title") or m.get("name", "tool")) + "</summary>", "", "```", body, "```",
                    "</details>", ""]
    return "\n".join(out)
