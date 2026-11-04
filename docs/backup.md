# Backups and restore

Free instances start blank after every restart or redeploy. Rconsole cannot change that, but it can make the loss small:
it saves a snapshot of the things that matter to a place that persists, and restores it when it boots on a blank
instance. This page is the authoritative description of how that works; other pages only summarise it.

> **Limits at a glance.** Snapshots are taken every **15 minutes** when something changed, so after a hard stop you can
> lose up to 15 minutes of work. Files over **20 MB** are never saved. A snapshot over **40 MB** (compressed) is
> refused. Running processes are never saved. A restore needs a short rebuild (minutes) before services are back.

## Two ways to keep data

| Setup | Where data lives | When to use it |
|---|---|---|
| **Persistent volume** at `RCONSOLE_DATA_DIR` | A disk the host keeps across restarts | VPS, Railway/Fly.io volumes, Render paid plans (`render.paid.yaml`, 5 GB disk at `/data`). Backups are still recommended as a safety net. |
| **Backup repository** (no volume) | An encrypted snapshot in a private git repository or folder; the instance's own disk is disposable | Render free plan (`render.yaml` has no disk), or any host without a volume. |

Both can be combined. Without either, every restart resets everything.

## Setting it up

Set these variables (full list in [Configuration](configuration.md)):

| Variable | Purpose |
|---|---|
| `RCONSOLE_BACKUP_URL` | A **private** git repository (GitHub, GitLab, Codeberg, ...), or use `RCONSOLE_BACKUP_DIR` for a folder or mount |
| `RCONSOLE_BACKUP_TOKEN` | Token with write access to that repository only (`RCONSOLE_BACKUP_USER` defaults to `x-access-token`) |
| `RCONSOLE_BACKUP_PASSPHRASE` | Required. Encrypts the snapshot. **Keep a copy elsewhere**: without it nobody can read a backup |

Then in the console: `sudo backup` shows the target, whether it is encrypted, the last result and any problem;
`sudo backup now` takes one immediately. The first scheduled backup runs about two minutes after start.

## What is saved

| Saved | Not saved |
|---|---|
| Users, password hashes, the session secret and pepper | VM system files (the root filesystem is downloaded again) |
| Each user's saved state: tabs, history, aliases, settings, environment variables | Running processes, terminals, jobs |
| The **workspace** (the host shell's persistent folder, `/home/rconsole` in Docker) | `node_modules`, Python virtualenvs, `.cache`, `.npm`, `site-packages`, Cargo and Go caches |
| Each Linux system's **`/root`** (its home) | Files over 20 MB, symbolic links |
| Which systems exist, their setup profiles and a dependency list (`deps.json`) | Files outside `/root` in a VM (`/etc`, `/opt`, `/usr/local`) and custom apt repositories |
| Service definitions (`services.json`) and exposures (`exposed.json`) | Logs and the updater's scratch and rollback copies |
| The verified release installed by `rconsole update` | |

Treat `/root` of a Linux system and the workspace as the places to keep anything you cannot lose.

## What the restore rebuilds

After a restore on a blank instance, in this order:

1. **Secrets and users** come back first, so sessions and passwords stay valid.
2. Each Linux system is **downloaded again** and set up, and its `vm setup` profiles are re-applied
   (`RCONSOLE_RESTORE_SETUP=0` skips this).
3. The packages recorded in its dependency list are **reinstalled** (`RCONSOLE_RESTORE_DEPS=0` skips this): manually
   installed system packages, `pip` packages of the system Python, global `npm` packages, each Python virtualenv under
   `/root` (recreated, with its packages), and each Node project that had a `node_modules` (`npm ci`, `yarn` or
   `pnpm install` according to its lockfile). Failures are per item, logged, and shown by `vm deps`.
4. While that runs the system is **locked** in a `restoring` state; its services wait and then start by themselves.

Things that are *not* reconstructed: packages installed outside the recorded categories, anything outside `/root`, and
host-shell installs. If you need them, put the commands in a script under `/root` or the workspace and run it from a
service or by hand.

## What happens at shutdown

When the platform stops the instance (it sends `SIGTERM`), Rconsole stops services, jobs and terminals and then tries
one **final backup**, waiting at most about 22 seconds. It is best effort: a forced kill, power loss or a provider that
does not wait can skip it. The worst case is the loss of everything since the last successful snapshot.

## Behaviour you should know

* **One snapshot only.** The git backend replaces the backup branch (`RCONSOLE_BACKUP_BRANCH`, default
  `rconsole-backup`) with a single new commit on each push, so the repository never grows. If a push fails, the branch
  on the server is not changed and the previous snapshot stays.
* **Unchanged data is skipped.** A scheduled backup does nothing if nothing changed since the last one.
* **Restore at boot never overwrites.** On a blank instance (no `users.json`), files that already exist are kept. A
  manual `sudo backup restore --yes` *does* overwrite. To restore cleanly, start from an empty data directory.
* **Only expected folders are restored.** Snapshot entries outside the known layout, with absolute paths or `..`, are
  ignored.
* **Encryption.** Snapshots are encrypted with a key derived from the passphrase (PBKDF2, 200,000 rounds, Fernet) unless
  you set `RCONSOLE_BACKUP_ENCRYPT=0`. A wrong passphrase gives "cannot decrypt the backup".
* **Not a download.** There is no command that hands you the snapshot as a file; it lives in your repository or folder,
  and `sudo backup restore --yes` reads it from there.
* **Updates.** The snapshot carries the installed release, so after a restore on an older image the update is
  re-applied and Rconsole restarts once.

## Test it once

Do this before you depend on it:

1. `sudo backup now` and check the repository received a branch with a single `backup.rcbk` file.
2. Create a marker file in `/root` of a Linux system, run `sudo backup now` again.
3. Deploy a second throwaway instance with the **same** backup variables, or run `sudo backup restore --yes` on a spare
   one. Confirm the marker file, your users, and that `vm list` shows your systems being rebuilt.
4. Confirm a service starts after the rebuild finishes (`service list`, `service logs <name>`).

## Volume or provider snapshots

For a disk-based install you can also copy the data directory with your own tools (a Docker volume tarball, provider
disk snapshots). That copy includes the VM root filesystems and exact file states, which Rconsole's application-level
backup deliberately does not. Stop Rconsole (or accept a crash-consistent copy) while you copy. The
[deploy guide](../DEPLOY.md#14-after-you-deploy) has the commands.
