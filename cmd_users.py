"""User administration commands (auth, roles, factory reset)."""
import os
import shutil

import auth
import commands
import config
import exposure
import jobs
import services
import sandbox
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
    # Don't leave the deleted user's processes, VM or saved state behind (a
    # later account re-using the name would otherwise inherit them).
    _migrate_cleanup(args[0])
    return f"User '{args[0]}' deleted."


def _migrate_user_data(old, new):
    """Move per-user state (tabs, aliases, env) and the VM to the new name."""
    jobs.kill_all_jobs(old)
    state.kill_all_hosts(old)
    with termsess.SESSIONS_LOCK:
        stale = [k for k in termsess.SESSIONS if k[0] == old]
        dead = [termsess.SESSIONS.pop(k) for k in stale]
    for s in dead:
        try:
            s.kill()
        except Exception:
            pass
    state.rename_user(old, new)
    sandbox.rename_user(old, new)
    exposure.rename_owner(old, new)
    services.rename_owner(old, new)


def _migrate_cleanup(name):
    jobs.kill_all_jobs(name)
    state.kill_all_hosts(name)
    with termsess.SESSIONS_LOCK:
        dead = [termsess.SESSIONS.pop(k) for k in [k for k in termsess.SESSIONS if k[0] == name]]
    for t in dead:
        try:
            t.kill()
        except Exception:
            pass
    sandbox.reset(name, keep_home=False)
    state.drop_user(name)
    exposure.remove_owner(name)
    services.remove_owner(name)


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
        if config.STATE_DIR.exists():
            for f in config.STATE_DIR.glob("*.json"):
                try:
                    os.remove(f)
                except Exception:
                    pass
        # Per-user VMs and logs too (the shared proot binary and rootfs
        # download cache are kept — they are not user data).
        if config.VM_DIR.exists():
            for d in config.VM_DIR.iterdir():
                if d.is_dir() and d.name not in ("bin", "cache", "tmp"):
                    shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(config.LOG_DIR, ignore_errors=True)
        exposure.clear()
        services.clear()
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
            # The seeded admin is flagged for a password change unless the
            # operator chose its password via RCONSOLE_ADMIN_PASSWORD.
            "update_session": {"username": "ark",
                               "must_change": not os.environ.get("RCONSOLE_ADMIN_PASSWORD")}}


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
        out = {"output": f"Password updated for '{target}'."}
        if target == ctx["username"]:
            # Lifts the "default password" lockout for this session.
            out["update_session"] = {"must_change": False}
        return out
    return routine()


@commands.command("cuser")
def cmd_cuser(ctx, args):
    def routine():
        new = (yield "New username: ").strip()
        if not new:
            return "cuser: empty username"
        old = ctx["username"]
        ok, err = auth.set_username(old, new)
        if not ok:
            return f"cuser: {err}"
        # Everything is keyed by username: carry the user's saved state and VM
        # across the rename so nothing is silently orphaned.
        _migrate_user_data(old, new)
        return {"output": f"Username changed to '{new}'.",
                "update_session": {"username": new}}
    return routine()
