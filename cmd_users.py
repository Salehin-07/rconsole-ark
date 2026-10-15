"""User administration commands (auth, roles, factory reset)."""
import os
from pathlib import Path

import auth
import commands
import jobs
import state
import termsess


@commands.command("logout")
def cmd_logout(ctx, args):
    # Public: anyone (admin or not) may log themselves out.
    return {"action": "logout", "output": "Logging out. Goodbye."}


@commands.command("suser")
def cmd_suser(ctx, args):
    """List all users and their role (admin/user)."""
    users = auth.list_users()
    if not users:
        return "No users."
    out = ["Users:", ""]
    for name in sorted(users):
        u = auth.get_user(name) or {}
        role = "admin" if u.get("is_admin") else "user"
        me = " (you)" if name == ctx["username"] else ""
        out.append(f"  {name:<20}{role}{me}")
    return "\n".join(out)


@commands.command("auser")
def cmd_auser(ctx, args):
    """Grant admin rights to a user: sudo auser <username>."""
    if not args:
        return "Usage: sudo auser <username>"
    ok, err = auth.set_admin(args[0], True)
    if not ok:
        return f"auser: {err}"
    return f"User '{args[0]}' is now an admin."


@commands.command("nauser")
def cmd_nauser(ctx, args):
    """Revoke admin rights from a user: sudo nauser <username>."""
    if not args:
        return "Usage: sudo nauser <username>"
    ok, err = auth.set_admin(args[0], False)
    if not ok:
        return f"nauser: {err}"
    return f"Admin rights removed from '{args[0]}'."


@commands.command("ruser")
def cmd_ruser(ctx, args):
    """Delete a user: sudo ruser <username>."""
    if not args:
        return "Usage: sudo ruser <username>"
    ok, err = auth.delete_user(args[0])
    if not ok:
        return f"ruser: {err}"
    return f"User '{args[0]}' deleted."


@commands.command("rboot")
def cmd_rboot(ctx, args):
    # Hard reset: kill hosted apps + terminal sessions, wipe every user's
    # persisted state, reset in-memory state, and reset users to defaults.
    state.kill_all_hosts_all()
    try:
        for sid in list(state.STATE.keys()):
            try:
                jobs.kill_all_jobs(sid)
            except Exception:
                pass
    except Exception:
        pass
    try:
        with termsess.SESSIONS_LOCK:
            sessions = list(termsess.SESSIONS.values())
            termsess.SESSIONS.clear()
        for s in sessions:
            try:
                s.kill()
            except Exception:
                pass
    except Exception:
        pass

    # Remove all persisted per-user state files so nothing respawns later.
    try:
        d = Path(__file__).resolve().parent / "data" / "state"
        if d.exists():
            for f in d.glob("*.json"):
                try:
                    os.remove(f)
                except Exception:
                    pass
    except Exception:
        pass

    # Wipe in-memory state for every user.
    try:
        with state.STATE_LOCK:
            state.STATE.clear()
    except Exception:
        pass

    auth.reset_to_defaults()
    return {"output": "*** HARD REBOOT ***\nSystem reinitialized to factory state.",
            "clear": True,
            "update_session": {"username": "ark"}}


@commands.command("muser")
def cmd_muser(ctx, args):
    def routine():
        username = (yield "New username: ").strip()
        if not username:
            return "muser: empty username"
        password = yield "New password: "
        confirm = yield "Confirm password: "
        if password != confirm:
            return "muser: passwords do not match"
        ok, err = auth.create_user(username, password)
        if not ok:
            return f"muser: {err}"
        return f"User '{username}' created."
    return routine()


@commands.command("cpass")
def cmd_cpass(ctx, args):
    def routine():
        target = (yield f"Change password for [{ctx['username']}]: ").strip()
        if not target:
            target = ctx["username"]
        new = yield "New password: "
        confirm = yield "Confirm password: "
        if new != confirm:
            return "cpass: passwords do not match"
        ok, err = auth.set_password(target, new)
        if not ok:
            return f"cpass: {err}"
        return f"Password updated for '{target}'."
    return routine()


@commands.command("cuser")
def cmd_cuser(ctx, args):
    def routine():
        new = (yield "New username: ").strip()
        if not new:
            return "cuser: empty username"
        ok, err = auth.set_username(ctx["username"], new)
        if not ok:
            return f"cuser: {err}"
        return {"output": f"Username changed to '{new}'.",
                "update_session": {"username": new}}
    return routine()
