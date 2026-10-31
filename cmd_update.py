"""``rconsole`` — version information and the self-update.

::

    rconsole                    short help
    rconsole version            the running version, where it is installed, the last update result
    rconsole update --check     is there a newer release?  (everyone)
    rconsole update             check, ask, then update to the latest release   (admins)
    rconsole update --yes       the same without asking
    rconsole update --rollback  go back to the version that was installed before the last update
    rconsole update --force     allow updating a git checkout / reinstalling the same version

While an update runs the whole console is locked: no command, terminal, file edit or hosted app can be
used until it is done. See :mod:`updater` for exactly what happens and why your data is safe.
"""
import time

import commands
import config
import updater
from version import __version__

HELP = """rconsole — version and updates

  rconsole version             show the running version and where it came from
  rconsole update --check      is a newer release available?
  rconsole update              update to the latest release (asks first; admins only)
  rconsole update --yes        update without asking
  rconsole update --rollback   go back to the version installed before the last update
  rconsole update --force      also update a git checkout, or reinstall the same version

Releases come from GitHub ({repo}). While an update runs the console is locked and nothing else
can be done; your data (users, VMs, workspace, services, settings) is never touched."""

FLAGS = {"--check": "check", "-c": "check", "--yes": "yes", "-y": "yes", "--force": "force",
         "--rollback": "rollback"}


def _ago(ts):
    secs = max(0, int(time.time() - float(ts)))
    for n, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= n:
            k = secs // n
            return f"{k} {unit}{'s' if k != 1 else ''} ago"
    return "just now"


def _version_text():
    out = [f"rconsole {__version__}"]
    disk = updater.installed_version()
    if disk != __version__:
        out.append(f"  files on disk: {disk} (restart pending: sudo reboot --yes)")
    out.append(f"  installed in:  {updater.APP_DIR}")
    out.append(f"  updates from:  github.com/{config.UPDATE_REPO} (releases)")
    st = updater.read_state()
    last = st.get("last")
    if isinstance(last, dict) and last.get("at"):
        out.append(f"  last update:   {last.get('message')} ({_ago(last['at'])})"
                   f"{'' if last.get('ok') else ' — FAILED'}")
    if st.get("pending"):
        out.append("  note: the new version has not been confirmed healthy yet")
    if (updater.PREVIOUS_DIR / "meta.json").is_file():
        out.append("  rollback:      available (rconsole update --rollback)")
    return "\n".join(out)


def _notes(text, limit=14):
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("---")]
    shown = lines[:limit]
    more = len(lines) - len(shown)
    return "\n".join("  " + ln for ln in shown) + (f"\n  … and {more} more lines" if more > 0 else "")


def _update_flow(ctx, flags):
    """Generator: checks, shows what is new, asks (unless --yes), then starts the update."""
    if updater.is_locked():
        return "rconsole update: an update is already running."
    if "rollback" in flags:
        if not ctx["is_admin"]:
            return "rconsole update: only administrators can roll back."
        meta = updater._read_json(updater.PREVIOUS_DIR / "meta.json", None)
        if not isinstance(meta, dict) or not meta.get("version"):
            return "rconsole update --rollback: nothing to go back to (no update has been installed yet)."
        if "yes" not in flags:
            answer = yield {"output": f"This restores rconsole {meta['version']} (you run {updater.installed_version()}) "
                                      "and restarts. Your data is not touched; services and terminals restart.",
                            "prompt": "Roll back now? [y/N] "}
            if answer.strip().lower() not in ("y", "yes"):
                return "Rollback cancelled."
        try:
            updater.start_rollback()
        except updater.UpdateError as e:
            return f"rconsole update --rollback: {e}"
        return {"output": f"Rolling back to {meta['version']} … the console is locked until it is done.",
                "updating": True}

    try:
        info = updater.check()
    except updater.UpdateError as e:
        return f"rconsole update: {e}"
    cur, new = info["current"], info["latest"]
    if not info["newer"] and not ("force" in flags and info["same"]):
        if info["same"]:
            return f"rconsole {cur} is up to date (latest release: {info['tag']})."
        return (f"rconsole {cur} is newer than the latest release ({info['tag']}) — nothing to do.")
    head = f"rconsole {cur} → {new} is available (released {info['published'] or 'recently'})."
    if info["bad"] and "force" not in flags:
        return (f"{head}\nThat version was rolled back on this instance (it failed to start, or you rolled it "
                "back), so it is not installed again by itself. Add --force to install it anyway.")
    if "check" in flags:
        text = [head]
        if info["notes"]:
            text += ["", "What's new:", _notes(info["notes"])]
        text += ["", f"Update with:  rconsole update   (details: {info['url']})"]
        return "\n".join(text)
    if not ctx["is_admin"]:
        return f"{head}\nrconsole update: only administrators can install it — ask one to run `rconsole update`."
    problems = updater.blockers("force" in flags)
    if problems:
        return f"{head}\nrconsole update: {problems[0]}"
    if "yes" not in flags:
        text = [head, ""]
        if info["notes"]:
            text += ["What's new:", _notes(info["notes"]), ""]
        text += ["The update will:",
                 "  • lock the console — nothing else can be done until it finishes",
                 "  • download the release and verify its SHA-256 checksum",
                 "  • test the new version in a separate process before touching anything",
                 "  • stop services, jobs and terminals, take a final backup, replace the program files",
                 "    (a rollback copy is kept) and restart — services start again by themselves",
                 "  • leave your data alone: users, VMs, workspace, services, settings and backups"]
        answer = yield {"output": "\n".join(text), "prompt": f"Update to {new} now? [y/N] "}
        if answer.strip().lower() not in ("y", "yes"):
            return "Update cancelled."
    try:
        updater.start(info, force="force" in flags)
    except updater.UpdateError as e:
        return f"rconsole update: {e}"
    return {"output": f"Updating {cur} → {new} … the console is locked until it is done "
                      "(this page shows the progress and reloads itself).", "updating": True}


@commands.command("rconsole")
def cmd_rconsole(ctx, args):
    sub = args[0].lower() if args else "help"
    if sub in ("help", "-h", "--help"):
        return HELP.format(repo=config.UPDATE_REPO)
    if sub in ("version", "-v", "--version"):
        return _version_text()
    if sub == "update":
        flags = set()
        for a in args[1:]:
            if a not in FLAGS:
                return f"rconsole update: unknown option {a!r}\n\n" + HELP.format(repo=config.UPDATE_REPO)
            flags.add(FLAGS[a])
        return _update_flow(ctx, flags)
    return f"rconsole: unknown command {sub!r}\n\n" + HELP.format(repo=config.UPDATE_REPO)
