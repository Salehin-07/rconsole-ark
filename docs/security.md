# Security model

What Rconsole enforces, what it deliberately does not, and the assumptions behind both. Read this before you give
anyone an account.

## The trust model

Rconsole is built for **one owner and a few people they trust** (10 accounts by default, `RCONSOLE_MAX_USERS`). It is
not a multi-tenant hosting platform. If you need to host strangers, run one separate container per tenant and treat the
container as the boundary.

| Role | What they can reach |
|---|---|
| **Admin** | The server's host shell and filesystem as the server's user (root if `RCONSOLE_RUN_AS_ROOT=1`): the data directory, every user's files and Linux-system homes, every user's services and saved environment variables. Admins are root-equivalent inside the container by design. |
| **User** | Only their own Linux systems. Commands, the file manager, the editor and the AI agent are confined to them. Host access for non-admins is off unless you set `RCONSOLE_ALLOW_USER_HOST=1` (that is remote code execution: leave it off). |

At least one admin always exists: the last admin cannot be deleted or demoted.

## What a Linux system is, and is not

Each "VM" is a root filesystem run under [PRoot](https://proot-me.github.io/), which translates paths and fakes root in
user space. **It is not a virtual machine and not a hardened sandbox.** There is no separate kernel. Each system has its
own root filesystem and its own `/root`; that is isolation of *files*, not of *privileges*.

| Question | Answer |
|---|---|
| Does each system see other systems' processes? | **No, when PID namespaces are available** (1.3.8+): each VM gets its own PID namespace (`unshare --user --pid`). Inside it `ps`, `top`, `kill` and `/proc` show only that VM's processes, with PIDs from 1, and `kill -9 -1` stays inside. |
| What if the host forbids user namespaces? | Rconsole still works, **without** process isolation, and `vm doctor` says so (`pid ns:` line). Everyone then sees only harmless `/proc` files (uptime, meminfo, cpuinfo, loadavg, version) instead of host processes. `vm ps` and `vm kill` show and signal only that VM's processes (found through PRoot). `RCONSOLE_VM_PIDNS=off` never creates namespaces and restores the pre-1.3.8 behaviour, including the host's full `/proc` for admins' VMs. |
| Is that a strong boundary? | **No.** Even with a PID namespace, every VM still runs under the server's single user id, shares the kernel, the network and the host's resources. A malicious account can still attack the server and other users. Namespaces prevent *accidents and snooping*, not a determined attacker. |
| Does Docker allow it? | Docker's default seccomp profile blocks user namespaces. On plain Docker add `--security-opt seccomp=unconfined` (or a profile allowing `unshare`/`setns`) to get isolation. Not verified on Render. |
| Resource limits? | **None** per VM or user: no memory, CPU or process quotas. One user can use all of the instance's RAM and CPU. |

`RCONSOLE_USER_VM_PROC=1` binds the host's `/proc` into VMs that have **no** PID namespace, which exposes host process
lists and their environments. Leave it off unless you trust every account.

The AI agent's destructive-command guard is switched off for commands run *inside* a user's own system, because those
are the user's own files. That is a convenience, not a statement that PRoot isolates the host.

## Authentication and web

* Passwords: PBKDF2-HMAC-SHA256 (210,000 rounds) with a per-user salt and a persisted random pepper. Old hashes are
  upgraded on login. Minimum length 8 (`RCONSOLE_MIN_PASSWORD`).
* The factory `ark` / `ark` login works only to set a new password.
* Failed logins are throttled per (IP, user) and per IP with exponential lockout (`RCONSOLE_LOGIN_ATTEMPTS`,
  `RCONSOLE_LOGIN_LOCKOUT`, capped at 15 minutes). The response never reveals whether a user exists.
* Cross-site POSTs and WebSocket handshakes are rejected (Origin / `Sec-Fetch-Site`). Cookies are `HttpOnly`,
  `SameSite=Lax` and `Secure` on HTTPS; `X-Frame-Options`, `nosniff` and HSTS are set.
* The session cookie is named `rconsole_session` and is never forwarded to hosted apps, so a Flask app's own `session`
  cookie cannot collide with it.
* The admin dashboard (`/dashboard`) and its API are admin-only and re-check the role on every request, so a demoted
  admin loses access at once; others get a 404 / 403 and never receive the link. Its pages build every value with
  `textContent` (no HTML injection from commands or names), and its changes pass the same Origin check as every POST.
  Logins, logouts and account or process changes are recorded in an audit trail (`logs/audit.jsonl`).
* There is no TLS in the process: terminate HTTPS at the platform or a proxy.
* Every launch and file access passes through one authorization point (`sandbox.py`): the line console, jobs, hosted
  apps, the terminal WebSocket, the file APIs and the AI agent.

## Where secrets live

| Secret | Where | Protection |
|---|---|---|
| Session key, password pepper | `<data>/secret.key`, `<data>/pepper.key` (or the environment) | `0600`; removed from user processes |
| Password hashes | `<data>/users.json` | `0600`, atomic writes |
| Provider and deployment secrets (`RCONSOLE_ADMIN_PASSWORD`, `RCONSOLE_BACKUP_TOKEN`, `RCONSOLE_BACKUP_PASSPHRASE`, `RCONSOLE_UPDATE_TOKEN`) | The environment | Removed from user processes. Held by your platform's secret store. |
| API keys saved with `sudo env`, tokens given to `service add --env` | `<data>/state/*.json`, `services.json` | `0600` **plain JSON on disk**; encrypted only inside backups. Use disk encryption if that matters. |
| Backup snapshots | Your backup repository or folder | Encrypted with a passphrase-derived key |

Anyone who can read the data directory (admins, or whoever controls the host) can read the plain files.

## What is not covered

* Hostile accounts. Treat every account as trusted.
* Hosted apps run as the server's user and are not isolated from each other.
* A compromised admin account is a compromised container.
* Denial of service by a user who exhausts RAM, CPU or disk.
* Anything that needs a real kernel boundary: use a VPS or a real VM for that.

## Reporting a vulnerability

Please do not open a public issue. Report it privately through GitHub:
[Security → Report a vulnerability](https://github.com/Salehin-07/rconsole-ark/security/advisories/new) on the
repository. Include the version (`rconsole version`), what you did, and what you expected.

## Hardening checklist

The [deploy guide](../DEPLOY.md#16-security-hardening-checklist) has the step-by-step list for your platform.
