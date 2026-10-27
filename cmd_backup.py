"""``backup`` — keep your data when a free host wipes its disk on every deploy.

::

    sudo backup                 status: where, when, how big, what is wrong
    sudo backup now             take a backup right now
    sudo backup restore --yes   replace local data with the latest backup
    sudo backup help            how to set it up

Backups are encrypted and go to a private git repo (or a folder). On a fresh
instance rconsole restores them automatically at boot, then rebuilds your VMs
and restarts your services. Set it up once with environment variables:

    RCONSOLE_BACKUP_URL=https://github.com/you/rconsole-data.git   (private repo!)
    RCONSOLE_BACKUP_TOKEN=<token with write access to that repo>
    RCONSOLE_BACKUP_PASSPHRASE=<a long passphrase — keep a copy elsewhere>
"""
import time

import backup
import commands

HELP = """\
Backups keep your work when a free host (Render, ...) wipes its disk on each deploy.

Set up (once), as environment variables on your host:
  RCONSOLE_BACKUP_URL         a PRIVATE git repo, e.g. https://github.com/you/rconsole-data.git
  RCONSOLE_BACKUP_TOKEN       a token with write access to only that repo
                              (GitHub: Settings > Developer settings > Fine-grained tokens >
                               Contents: Read and write)
  RCONSOLE_BACKUP_PASSPHRASE  long random passphrase; encrypts everything. Keep a copy somewhere
                              else — without it the backup cannot be read.
  (or RCONSOLE_BACKUP_DIR=/some/folder to back up to a folder instead)

What is saved: users and secrets, settings, the workspace, exposures, services, each VM's /root
and the list of VMs and `vm setup` profiles. NOT saved: VM system files (re-downloaded), caches,
node_modules/venvs, files over 20 MB, running processes.

Then: backups run every 15 minutes when something changed (and once more at shutdown); a fresh
instance restores itself at boot, rebuilds your VMs and restarts your services."""


def _ago(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


def _status():
    s = backup.status()
    lines = ["Backup", "------", f"  target    : {s['target']}"]
    if s["problems"]:
        lines.append("  PROBLEMS  :")
        lines += [f"    - {p}" for p in s["problems"]]
        lines += ["", "Run `sudo backup help` for the setup steps."]
        return "\n".join(lines)
    lines += [f"  encrypted : {'yes' if s['encrypted'] else 'NO'}",
              f"  schedule  : every {s['interval'] // 60} min when something changed"
              + ("" if s["running"] else "  (scheduler not running)"),
              f"  last      : {_ago(s['last_time'])}"
              + (f" — {'ok' if s['last_ok'] else 'FAILED'}" if s["last_time"] else ""),
              f"  size      : {s['last_size'] // 1024} KB, {s['last_files']} files, {s['count']} backups this run"]
    if s["restored"]:
        lines.append(f"  restored  : {_ago(s['restored'])} (this instance started from a backup)")
    if s["last_error"]:
        lines.append(f"  error     : {s['last_error']}")
    return "\n".join(lines)


@commands.command("backup")
def cmd_backup(ctx, args):
    sub = args[0].lower() if args else "status"
    if sub in ("status", "info"):
        return _status()
    if sub in ("help", "-h", "--help", "setup"):
        return HELP
    if sub in ("now", "run", "save"):
        ok, msg = backup.backup_now(force=True, reason="manual")
        return ("✓ " if ok else "✗ ") + msg
    if sub == "restore":
        if "--yes" not in args:
            return ("This replaces local users, settings, workspace and VM home folders with the latest "
                    "backup.\nRe-run with --yes to confirm:  sudo backup restore --yes")
        ok, msg = backup.restore(force=True)
        return ("✓ " if ok else "✗ ") + msg + (
            "\nRestart (`sudo reboot`) to apply restored users and settings." if ok else "")
    return "usage: backup [status | now | restore --yes | help]"
