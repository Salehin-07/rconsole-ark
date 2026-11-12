"""``storage`` - files kept in your GitHub repo instead of on the host's disk.

::

    storage                     show where your files live and how full the local cache is
    storage get <path>...       bring placeholder files (or whole folders) back to this disk
    storage pin <path>...       keep files on this disk for good (never turned into placeholders)
    storage unpin <path>...
    storage sync                upload changes now                                   (admin)
    storage mode host|github    choose where files live                              (admin)
    storage encrypt on|off      encrypt files in the repo (new files; see --rewrite)  (admin)

Only your workspace and each system's /home, /srv and /data are kept in the repo; /root, /etc, /proc, /app
and the other system folders always stay on the host. A big file that is not on this disk shows up in its
folder as a small placeholder; opening it in the file manager, or naming it in a command, fetches it back
automatically. The dashboard (admins) has a page with every option.
"""
import os

import commands
import gitstore
import sandbox


def _size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _summary():
    s = gitstore.status()
    lines = ["Repo storage", "------------",
             f"  mode      : {s['mode']}" + ("" if s["active"] or s["mode"] == "host" else "  (NOT active: files stay on the host)"),
             f"  encrypted : {'yes' if s['encrypted'] else 'no'}",
             f"  stored    : {s['files']} file(s), {_size(s['bytes'])} in {s['pieces']} piece(s)",
             f"  this disk : {_size(s['cache_bytes'])} of {_size(s['cache_limit'])} cache, {s['placeholders']} placeholder(s)",
             f"  piece size: {s['chunk_mb']} MB"]
    if s["state"] != "idle":
        lines.append(f"  working   : {s['state']} {s['done']}/{s['total']} {s['message']}")
    if s["last_error"]:
        lines.append(f"  last error: {s['last_error']}")
    if s["blocked"]:
        lines.append(f"  paused    : {s['blocked']}")
    for p in s["problems"]:
        lines.append(f"  problem   : {p}")
    return "\n".join(lines)


def _paths(ctx, args):
    sid = ctx["sid"]
    view = sandbox.fs(sid)
    cwd = ctx["tab"]["cwd"]
    return [view.host(a, cwd, follow_last=False) for a in args]


@commands.command("storage")
def cmd_storage(ctx, args):
    sub = (args[0].lower() if args else "status")
    rest = args[1:]
    admin = ctx["is_admin"]
    if sub in ("status", "info"):
        return _summary()
    if sub in ("get", "pin", "unpin"):
        if not rest:
            return f"usage: storage {sub} <path>..."
        if not gitstore.active():
            return "storage: repo storage is off, so every file is already on this disk."
        out = []
        try:
            for p in _paths(ctx, rest):
                if not os.path.lexists(p):
                    out.append(f"{p}: no such file")
                    continue
                if sub == "get":
                    n = gitstore.ensure_local(p, recursive=True)
                    out.append(f"{p}: {n} file(s) brought back" if n else f"{p}: already on this disk")
                else:
                    gitstore.pin(p, on=(sub == "pin"))
                    out.append(f"{p}: {'pinned (stays on this disk)' if sub == 'pin' else 'unpinned'}")
        except (gitstore.StoreError, sandbox.SandboxError, OSError) as e:
            out.append(f"storage: {e}")
        return "\n".join(out)
    if sub in ("sync", "mode", "encrypt"):
        if not admin:
            return f"storage {sub}: only admins can do that."
        import settings
        if sub == "sync":
            if not gitstore.active():
                return "storage: repo storage is off or not set up.\n" + _summary()
            ok, msg = gitstore.sync_once("command", force=True)
            return ("synced: " if ok else "sync failed: ") + msg
        if sub == "mode":
            if not rest or rest[0] not in ("host", "github"):
                return "usage: storage mode host|github"
            ok, msg, _ = settings.set_values({"RCONSOLE_STORAGE": rest[0]}, ctx["username"])
            note = "" if rest[0] == "host" or not gitstore.problems() else "\n  but: " + "; ".join(gitstore.problems())
            return (msg if not ok else f"storage mode: {rest[0]} (saved; also changeable in the dashboard)") + note
        if not rest or rest[0] not in ("on", "off"):
            return "usage: storage encrypt on|off [--rewrite]"
        ok, msg, _ = settings.set_values({"RCONSOLE_STORAGE_ENCRYPT": "1" if rest[0] == "on" else "0"}, ctx["username"])
        if ok and "--rewrite" in rest and gitstore.active():
            ok2, m2 = gitstore.reencode()
            return f"encryption {rest[0]}: {m2}"
        return msg if not ok else f"encryption {rest[0]} for new files (add --rewrite to convert the stored ones)"
    return "usage: storage [status | get <path> | pin <path> | unpin <path> | sync | mode host|github | encrypt on|off]"
