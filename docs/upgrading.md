# Updating and rollback

There are four different things you can update. They are independent and use different tools.

| What | Tool | Touches | Your data |
|---|---|---|---|
| **Rconsole itself** (program files) | `rconsole update` | The installed program files, Python dependencies the release needs | Never touched |
| **A Linux system** (Alpine, Debian, Ubuntu, Kali) | `vm update [name]` | That system's root filesystem | Its `/root` is never touched |
| **The container image** (Dockerfile, system packages, Python base image) | Redeploy / rebuild | The whole image | Kept if `/data` is a volume or backed up |
| **Your own apps** | Whatever they use (`git pull`, `pip install -U`, ...) | Their folders under `/root` or the workspace | Yours |

## `rconsole update`

```text
rconsole update --check     # anyone: is a newer release published?
rconsole update            # admins: check, show what is new, confirm, update, restart in place
rconsole update --rollback  # back to the version that was installed before the last update
```

What it does, in order: it **locks the console** (every request except the health check gets a `503`), downloads the
release and **verifies its SHA-256** against the release's `SHA256SUMS`, **tests the new version in a separate process**
before touching anything, stops services, jobs and terminals, takes a final backup, replaces the program files
atomically while keeping a rollback copy, and restarts in place. A failure before the swap changes nothing.

* **Rollback scope.** Rollback restores the *program files* from the rollback
  copy. It does not undo changes to your data. A new version that never stays up for about 25 seconds and keeps
  failing to start is rolled back automatically at the next container start, and is not offered again until you use
  `--force`. `python updater.py rollback` works even when the server will not start.
* **Containers.** A redeploy rebuilds the image from your repository, which would undo an in-place update, so the
  verified release is kept in `<data>/update/releases` and in the encrypted backups, and re-applied at boot. That works
  on a persistent volume, and on a disk-less host only if backups are configured.
* **What it cannot change.** Anything in the image: the `Dockerfile`, system packages, the Python base image. When a
  release changes those, the update tells you to redeploy.
* **Git checkouts and read-only installs** are refused. Use `git pull`, or add `--force`.
* **Forks and rate limits.** Set `RCONSOLE_UPDATE_REPO` to your fork and `RCONSOLE_UPDATE_TOKEN` for private forks.

## Updating by redeploy

Use this when the release changes the image, or you prefer it:

| Hosting | Command |
|---|---|
| PaaS with auto-deploy | Push to `main`, or point the service at the new release and redeploy |
| Docker Compose | `git pull && docker compose up -d --build` (or change `RCONSOLE_VERSION` and `docker compose pull` for the image compose file) |
| systemd | `git pull && uv sync --frozen && sudo systemctl restart rconsole` |

On a persistent volume your data stays. On a disk-less host the instance restores from its backup afterwards, so check
`sudo backup` first and take a fresh `sudo backup now` before you update.

## What survives an update or restart

| Item | After `rconsole update` or `sudo reboot` | After a redeploy |
|---|---|---|
| Users, files, Linux systems, `/root` | Kept | Kept on a volume; restored from backup otherwise |
| Terminal tabs, history, aliases | Kept | Kept (saved state) |
| Programs running in terminals, jobs, `&` tasks, hand-started apps | **Stopped** | **Stopped** |
| `service` definitions | Kept | Kept; the supervisor starts them again once their system is ready |
| Browser page | Reconnects by itself | Reconnects by itself |

Open terminal tabs come back as fresh terminals in the same folder; the programs that ran in them do not.

## `vm update`

`vm update [name]` brings a Linux system to the newest version of its release line safely: an **Alpine** system moves to
the newest Alpine release one step at a time, **Debian, Ubuntu and Kali** get all newer packages of their release.
It locks the system, health-checks the package database, takes a hard-linked snapshot, upgrades without prompts, and
verifies the result. On any failure, timeout, server restart or crash the snapshot is put back. Details, options and
who may run it: [the VM guide](../README.md#the-vm-vm) and [COMMANDS.md](../COMMANDS.md).

## Release notes

Every release lists what changed in the [changelog](../CHANGELOG.md). A release that needs a restart or redeploy,
a new environment variable or a Dockerfile change says so under **Notes**. Before any update on a disk-less host, run
`sudo backup now`.
