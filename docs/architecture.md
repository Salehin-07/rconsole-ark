# Architecture

How Rconsole is put together. For what each source file does, see the [file reference](../structure.md).

```text
Browser (xterm.js)
   │  HTTP(S)            REST /api/run  (line-mode commands)
   ▼
Flask app (app.py)   ── before_request: Origin/CSRF check → auth guard
   ├── /api/run  → interp.py ──┬─ commands.py (built-ins: cmd_*.py, editor, agent, fun)
   │                           ├─ jobs.py      (background `cmd &`)
   │                           └─ shell passthrough
   ├── /ws       → ptyhost.py → termsess.py (persistent PTY)
   ├── /api/file|dir|fs  (editor + file manager)
   ├── /api/update/status (progress of `rconsole update`; the console is locked while it runs)
   └── /<name>/…  → reverse proxy → hosted app process
                │
   every launch and every file access goes through
                ▼
        sandbox.py  ── authorizes (admin? mode?) and picks the target
            ├── host: the server's shell and filesystem (admins)
            └── vm:   PRoot + per-user, per-system root filesystem, path-confined FsView,
                      own PID namespace where the host allows it
```

## Principles

* **One authorization point.** `sandbox.spec()` turns a command into argv, working directory and environment for the
  right target *and* enforces permissions; `sandbox.fs()` returns the path namespace for file access. The interpreter,
  jobs, terminals, hosted apps, editor, file APIs and the AI agent all use them.
* **One process, many threads.** Sessions, terminals, jobs and hosted-app records live in the memory of one process.
  Run exactly **one gunicorn worker** with many threads (`RCONSOLE_THREADS`). Two instances would split the state and
  lose terminals. Sticky sessions do not fix this, because the state is process memory, not a shared store.
* **Environment-driven.** Everything is configured by [environment variables](configuration.md).

## What lives where

| Thing | Location | Survives a restart? |
|---|---|---|
| Users, secrets | `<data>/users.json`, `secret.key`, `pepper.key` | Yes, on a volume or via [backup](backup.md) |
| Saved tabs, history, aliases, user environment | `<data>/state/` | Yes (same rule) |
| Host shell's persistent folder | `<data>/workspace`, linked to `/home/rconsole` in the Docker image | Yes (same rule) |
| Linux systems | `<data>/vm/<user>/<system>/{rootfs,home}`; `home` is that system's `/root` | `home` is backed up; `rootfs` is downloaded again |
| Services and exposures | `<data>/services.json`, `exposed.json` | Definitions yes; processes no |
| Update files | `<data>/update/` | Verified releases yes; scratch no |
| Terminals, jobs, running apps | Process memory and child processes | **No** |

`/data/workspace` (host), `/home/rconsole` (the same folder in Docker) and a VM's `/root` are three places: the first two
are the host shell's persistent workspace, the last is inside one Linux system.

## The Linux-system lifecycle

1. `vm install <name>` downloads a checksum-verified root filesystem, unpacks it and runs a self-test.
2. Commands run through `proot -0 -r <rootfs>` with the system's home bound at `/root`.
3. If the host allows it, a small init process holds a private PID namespace per system; every command of that system
   joins it, so a service started in one terminal is visible from another.
4. `vm update` locks the system, snapshots it, upgrades and verifies, or puts the snapshot back.
5. After a restore on a blank instance the system is rebuilt in a locked `restoring` state, then released.

## The service supervisor

`service add` stores a command, working directory, environment and optional exposure. The supervisor starts each
service **when Rconsole starts and the target system is ready**, restarts it when it exits with a back-off of 2 s doubling
up to 60 s (reset after a minute of stable running), and never gives up on its own: stop a failing service with
`service stop <name>`. Limits: 10 services and 20 exposures per user.

## The reverse proxy

`expose` publishes a local port at `https://<domain>/<name>/` for logged-in users. The proxy is HTTP only: requests are
buffered with a **30 second timeout**, **WebSocket upgrades are not proxied**, and the proxy sends `X-Forwarded-Prefix`,
`-Host` and `-Proto`. See the [hosting guide](../README.md#hosting-web-apps) for framework notes.

## Shutdown

On `SIGTERM` (a deploy or restart) gunicorn calls `lifecycle.begin_shutdown`: pause the supervisor, kill children,
stop VM namespaces' leftovers, and make a final backup within about 22 seconds, inside gunicorn's 30 second graceful
period. `gunicorn.conf.py` keeps `keepalive = 0` so idle connections cannot hold the shutdown.
