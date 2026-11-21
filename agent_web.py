"""HTTP side of the agent: the page, the JSON API and the server-sent event stream.

Admins only (the agent can run commands on the host and in VMs, exactly like ``sudo``). A turn runs in a thread
(see :mod:`agent_core`); the browser follows its event log with ``GET .../stream?after=<seq>``, so a dropped
connection (phones do this constantly) loses nothing: it simply reconnects from the last number it saw.
"""
import json
import os
import threading
import time

from flask import Blueprint, Response, jsonify, redirect, render_template, request, session, url_for

import agent_core as core
import agent_providers as providers
import agent_store as store
import agent_tools as tools
import auth
import sandbox
import state
from version import __version__

bp = Blueprint("agent_ui", __name__)


def _who():
    """``(username, error response)``: only a logged-in admin gets through."""
    u = session.get("username")
    if not u:
        return None, (jsonify(error="unauthorized"), 401)
    if not auth.is_admin(u):
        return None, (jsonify(error="the agent is for admins only"), 403)
    return u, None


def _ctx(user, sess):
    c = tools.Ctx(user, sess, sess.meta.get("cwd") or "", threading.Event(), opts=providers.options())
    c.cwd = c.cwd or c.view.default_cwd()
    return c


def _session_or_404(user, sid):
    s = store.get(user, sid)
    if s is None:
        return None, (jsonify(error="no such chat"), 404)
    return s, None


def _theme(user):
    s = state.get_session(user)
    return (s or {}).get("theme", state.DEFAULT_THEME)


@bp.get("/agent")
def page():
    u = session.get("username")
    if not u:
        return redirect(url_for("login"))
    if not auth.is_admin(u):
        return Response("The agent is for admins only. Ask an admin for access to a shell.", 403, mimetype="text/plain")
    try:
        v = int(max(os.path.getmtime(os.path.join(os.path.dirname(__file__), "static", f)) for f in ("agent.js", "agent.css")))
    except OSError:
        v = 1
    resp = Response(render_template("agent.html", username=u, theme=_theme(u), cache_v=v, version=__version__))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _provider_rows(user):
    rows = providers.public_table(user)
    return [{"id": r["id"], "name": r["name"], "model": r["model"], "usable": r["usable"], "kind": r["kind"],
             "custom": r["custom"], "context": r["context"]} for r in rows if r["usable"]]


@bp.get("/api/agent/state")
def api_state():
    u, e = _who()
    if e:
        return e
    opts = providers.options()
    cfg = providers.resolve(u)
    try:
        tg = sandbox.targets(u)
        target = {"label": tg.get("label"), "current": tg.get("current")}
    except Exception:                                                  # noqa: BLE001
        target = {"label": "", "current": None}
    return jsonify(version=__version__, providers=_provider_rows(u), default=cfg["id"] if cfg else "",
                   default_model=cfg["model"] if cfg else "", modes=[{"id": m, "help": core.MODE_HELP[m]} for m in core.MODES],
                   default_mode=opts["mode"], target=target, configured=bool(cfg))


@bp.get("/api/agent/sessions")
def api_sessions():
    u, e = _who()
    if e:
        return e
    rows = store.list_sessions(u, request.args.get("q", ""))
    for r in rows:
        run = core.get_run(u, r["id"])
        r["running"] = bool(run and not run.done)
    return jsonify(sessions=rows[:200])


@bp.post("/api/agent/session")
def api_session_new():
    u, e = _who()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    try:
        view = sandbox.fs(u)
    except sandbox.SandboxError as ex:
        return jsonify(error=str(ex)), 400
    cwd = view.default_cwd()
    want = str(d.get("cwd") or "")
    if want:
        try:
            hp = view.host(want, cwd)
            if os.path.isdir(hp):
                cwd = view.virt(hp)
        except (OSError, sandbox.SandboxError):
            pass
    cfg = providers.resolve(u)
    s = store.create(u, cwd, provider=cfg["id"] if cfg else "", model=cfg["model"] if cfg else "",
                     mode=providers.options()["mode"], target=sandbox.current_target(u) or "")
    return jsonify(id=s.id)


def _view(u, s):
    run = core.get_run(u, s.id)
    cfg = providers.resolve(u, s.meta.get("provider"), s.meta.get("model"))
    try:
        cmds = core.command_list(_ctx(u, s))
    except Exception:                                                  # noqa: BLE001
        cmds = []
    meta = dict(s.meta)
    meta["window"] = providers.context_window(cfg) if cfg else providers.DEFAULT_CTX
    meta["provider_name"] = cfg["name"] if cfg else ""
    meta["model"] = cfg["model"] if cfg else meta.get("model", "")
    meta["provider"] = cfg["id"] if cfg else meta.get("provider", "")
    meta["mode"] = meta.get("mode") or providers.options()["mode"]
    meta["undo"] = store.cp_count(u, s.id)
    return {"id": s.id, "meta": meta, "messages": s.transcript(), "compact": s.compact["upto"] if s.compact else 0,
            "run": {"active": bool(run and not run.done), "last": (run.next - 1) if run else 0},
            "commands": cmds, "target": sandbox.target_label(u)}


@bp.get("/api/agent/session/<sid>")
def api_session(sid):
    u, e = _who()
    if e:
        return e
    s, e = _session_or_404(u, sid)
    if e:
        return e
    v = _view(u, s)
    if request.args.get("light"):
        v.pop("messages", None)                  # refreshing the header must not re-send a long transcript
    return jsonify(v)


@bp.post("/api/agent/session/<sid>/send")
def api_send(sid):
    u, e = _who()
    if e:
        return e
    s, e = _session_or_404(u, sid)
    if e:
        return e
    d = request.get_json(silent=True) or {}
    text = str(d.get("text") or "").strip()
    if not text or len(text) > 60000:
        return jsonify(error="write a message first (up to 60,000 characters)"), 400
    cur = core.get_run(u, sid)
    if cur and not cur.done:
        return jsonify(error="the agent is still working on this chat"), 409
    cmd = core.handle_command(u, s, text)
    try:
        if cmd is None:
            run = core.start(u, s, text)
            return jsonify(started=True, last=run.next - 1)
        if cmd.get("start") == "compact":
            run = core.start(u, s, text, job="compact", prompt=cmd.get("prompt"))
            return jsonify(started=True, last=run.next - 1)
        if cmd.get("prompt"):
            run = core.start(u, s, text, prompt=cmd["prompt"])
            return jsonify(started=True, last=run.next - 1)
    except core.Busy as ex:
        return jsonify(error=str(ex)), 429
    return jsonify(started=False, **cmd)


@bp.get("/api/agent/session/<sid>/stream")
def api_stream(sid):
    u, e = _who()
    if e:
        return e
    run = core.get_run(u, sid)
    try:
        after = int(request.headers.get("Last-Event-ID") or request.args.get("after") or 0)
    except ValueError:
        after = 0

    def gen():
        yield "retry: 2500\n\n"
        if run is None:
            yield "event: end\ndata: {}\n\n"
            return
        seq = after
        while not core.SHUTTING.is_set():
            evs, done, reset = run.follow(seq, 12.0)
            if reset:
                yield "event: reset\ndata: {}\n\n"
                seq = run.first - 1
                continue
            for ev in evs:
                seq = ev["seq"]
                yield f"id: {seq}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if done:
                yield "event: end\ndata: {}\n\n"
                return
            if not evs:
                yield ": ping\n\n"

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@bp.post("/api/agent/session/<sid>/cancel")
def api_cancel(sid):
    u, e = _who()
    if e:
        return e
    run = core.get_run(u, sid)
    if run and not run.done:
        run.cancel.set()
        for p in list(run.pending.values()):
            p["ev"].set()
    return jsonify(ok=True)


@bp.post("/api/agent/session/<sid>/answer")
def api_answer(sid):
    u, e = _who()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    ok = core.answer(u, sid, str(d.get("id") or ""), d.get("value"))
    return jsonify(ok=ok)


@bp.post("/api/agent/session/<sid>/meta")
def api_meta(sid):
    u, e = _who()
    if e:
        return e
    s, e = _session_or_404(u, sid)
    if e:
        return e
    d = request.get_json(silent=True) or {}
    changes = {}
    if "mode" in d:
        if d["mode"] not in core.MODES:
            return jsonify(error="unknown mode"), 400
        changes["mode"] = d["mode"]
    if "title" in d:
        changes["title"] = str(d["title"]).strip()[:80] or "New chat"
    if "provider" in d or "model" in d:
        cfg = providers.resolve(u, d.get("provider") or s.meta.get("provider"), d.get("model") or None)
        if not cfg or (d.get("provider") and cfg["id"] != d["provider"]):
            return jsonify(error="that provider is not set up (Dashboard > AI agent)"), 400
        changes["provider"] = cfg["id"]
        changes["model"] = (d.get("model") or cfg["model"]).strip()[:200]
    if "cwd" in d:
        try:
            c = _ctx(u, s)
            hp = c.path(str(d["cwd"]))
        except (tools.ToolError, sandbox.SandboxError) as ex:
            return jsonify(error=str(ex)), 400
        if not os.path.isdir(hp):
            return jsonify(error="not a directory"), 400
        changes["cwd"] = c.virt(hp)
    cur = core.get_run(u, sid)
    if cur and not cur.done and ("cwd" in changes or "provider" in changes):
        return jsonify(error="wait for the current turn to finish"), 409
    s.set_meta(**changes)
    return jsonify(_view(u, s)["meta"])


@bp.post("/api/agent/session/<sid>/delete")
def api_delete(sid):
    u, e = _who()
    if e:
        return e
    cur = core.get_run(u, sid)
    if cur and not cur.done:
        cur.cancel.set()
    return jsonify(ok=store.delete(u, sid))


@bp.post("/api/agent/session/<sid>/fork")
def api_fork(sid):
    u, e = _who()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    new = store.fork(u, sid, d.get("upto"))
    if not new:
        return jsonify(error="no such chat"), 404
    return jsonify(id=new.id)


@bp.get("/api/agent/session/<sid>/export")
def api_export(sid):
    u, e = _who()
    if e:
        return e
    s, e = _session_or_404(u, sid)
    if e:
        return e
    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in s.meta["title"])[:40] or "chat"
    resp = Response(store.export_markdown(s), mimetype="text/markdown; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}.md"'
    return resp


@bp.route("/api/agent/models", methods=["GET", "POST"])
def api_models():
    """Model ids of one provider. POST may carry an address / key typed in the dashboard but not saved yet."""
    u, e = _who()
    if e:
        return e
    d = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
    pid = str(d.get("provider") or request.args.get("provider") or "").strip()
    if not pid:
        cfg0 = providers.resolve(u)
        pid = cfg0["id"] if cfg0 else ""
    try:
        cfg = providers.browse_config(u, pid, d.get("base") or "", d.get("api_key") or "")
    except ValueError as ex:
        return jsonify(models=[], error=str(ex), provider=pid), 400
    if not cfg:
        return jsonify(models=[], error="that provider does not exist", provider=pid), 400
    try:
        if d.get("refresh") or request.args.get("refresh"):
            providers.forget_models(cfg)
        ids = providers.list_models(cfg)
    except providers.ProviderError as ex:
        hint = " Add the API key first." if ex.status in (401, 403) and not cfg.get("api_key") else ""
        return jsonify(models=[], error=str(ex) + hint, provider=cfg["id"], current=cfg["model"])
    return jsonify(models=ids[:2000], total=len(ids), provider=cfg["id"], current=cfg["model"])


@bp.get("/api/agent/files")
def api_files():
    """File name suggestions for @-mentions."""
    u, e = _who()
    if e:
        return e
    s, e = _session_or_404(u, request.args.get("s", ""))
    if e:
        return e
    q = (request.args.get("q") or "").strip().lower()
    try:
        c = _ctx(u, s)
        root = c.path(".")
    except (tools.ToolError, sandbox.SandboxError):
        return jsonify(files=[])
    out, end = [], time.time() + 1.5
    for full in tools._walk_files(root, end):
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        if q in rel.lower():
            out.append(rel)
            if len(out) >= 40:
                break
    out.sort(key=lambda r: (r.count("/"), len(r)))
    return jsonify(files=out[:12])
