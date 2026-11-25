# The VM (`vm`)

Every user can have private Linux environments — **several, subject to the host's disk and RAM**: just one,
two, or all four distributions at once. Each is a PRoot-based environment (not a virtual machine) with its **own
root filesystem and its own `/root`**. Inside one you are `root`
and can install anything. It needs **no host privileges** — that is the point: on
Render (or any container that gives you no `sudo`) the VM still gives you a fully
permissive machine.

```text
vm                    your systems and the active one     vm use <name>         make one the default
vm list               systems you can install             vm mode [host|vm [n]] where plain commands run
vm install <name>...  e.g. debian ubuntu:24.04 | all      vm reset [name] --yes recreate system files (keeps /root)
vm shell [name]       interactive shell in a VM           vm remove <name|all> --yes   delete it and its /root
vm run [--vm n] <cmd> run one command in a VM             vm doctor [name]      diagnose a VM
vm update [name]      update a VM to the latest version, safely (locks it; rolls back on any problem)
vm ps / vm kill <pid> processes of a VM (own PID space)    vm deps [name]       what a restore reinstalls
vm setup <profile>    install Python / build tools / Node / … in a VM (see below)
```

## Pick your systems

`vm list` shows everything available; `vm install <name>[:<release>]` installs it.
Install several in one go: `vm install debian ubuntu kali arch`, or `vm install all`.

| Name | System | Packages | Download | Notes |
|------|--------|----------|----------|-------|
| `debian` (**default**) | Debian 13 "trixie" | `apt install` | ~90 MB | The universal choice. `debian:bookworm`, `debian:sid`, … |
| `ubuntu` | Ubuntu (newest LTS, 26.04) | `apt install` | ~35 MB | `ubuntu:24.04`, `ubuntu:22.04`, … |
| `kali` | Kali Linux (minimal) | `apt install` | ~140 MB | Security-testing distro — authorized use only. Add tools with `apt install kali-linux-headless` etc. |
| `alpine` | Alpine Linux | `apk add` | ~3 MB | Tiny and the fastest to set up. |
| `arch` (also `archlinux`) | Arch Linux (rolling) | `pacman -S` | ~150 MB | Always the newest packages; rolling, so there is only one release (`arch`). The first `pacman -S` creates the signing keys by itself. |

Plain `vm install` (no name) installs **Debian**.

Every download is verified against the checksum the distribution itself publishes.

## Install Python and build tools in one command

`vm setup <profile>` installs a ready-made toolchain **inside a VM** as a background
job with live progress (it keeps going if you close the tab; `jobs`, `fg %n`):

| Profile | Installs |
|---------|----------|
| `python` | Python 3, pip, venv, a compiler **and the development headers** that packages like **psycopg2**, mysqlclient, Pillow, lxml and cryptography need to build (PostgreSQL, MySQL/MariaDB, SQLite, libffi, OpenSSL, XML, JPEG/PNG/WebP, LDAP, YAML, …) |
| `build` | gcc, g++, make, cmake, autotools, pkg-config |
| `node` | Node.js and npm |
| `rust` | Rust and cargo (for wheels that compile from source) |
| `db` | `psql`, `mysql`, `redis-cli`, `sqlite3` |
| `tools` | nano, vim, wget, curl, git, ps, htop, zip, jq, ssh, … |
| `docker` | `docker run ...` **without a daemon** (udocker + a `docker` command, also `drun`), plus the real client for `DOCKER_HOST`. In a VM static images such as hello-world can't run (use the host shell); no `-d`, `-p`, exec, build or compose |
| `udocker` | the same as `docker`, without the real Docker client |
| `kali` | **Kali VMs only**: the command-line Kali toolset (`kali-linux-headless`) and the basics it needs. Several GB, long install |
| `all` | `tools`, `build`, `python`, `node`, `rust`, `db` (not `docker`/`kali`) |

```text
$ vm setup python            # (add a name if you have several: vm setup python kali)
$ pip install psycopg2 mysqlclient Pillow lxml      # builds from source, no flags needed
$ vm setup docker            # Docker client + compose
$ vm setup kali              # in a Kali VM (vm install kali first): the Kali tools
```

The right package names are used for each system (`libpq-dev` on Debian-family,
`postgresql-dev` on Alpine), and if one package is missing on a given release only
that one is skipped. Inside a VM `pip install` works without
`--break-system-packages`, and installs never stop to ask questions (no more
"Please select the geographic area" from tzdata) — `DEBIAN_FRONTEND=noninteractive`
is always set.

## Working with several systems

- **One system installed:** everything just works; you never type its name.
- **Several installed:** pick where things run — whichever is easiest:
  - the **dropdown in the tab bar** (🖥 Host · 🐧 Debian · 🐧 Kali · *Install another…*)
    — on a **phone** it lives in the **⚙ settings menu** together with the theme
    button (same controls, just tidier; wider screens keep them in the bar);
  - `vm use debian` / `vm mode vm debian`;
  - one-offs: `vm shell kali`, `vm run --vm ubuntu apt update`, or prefix any
    command with `@vm:kali` (e.g. `@vm:kali nmap -V`).
- If a command needs a system and you have not picked one, a **dropdown appears
  right in the output** listing your systems *and* the ones you can still install;
  choose one and the command runs. Your choice is remembered.
- The **prompt shows where you are**: `ark@rconsole:/srv$` on the host,
  `ark@debian:/root$` in a VM.
- **Admins** can use both `host` and any VM (`vm mode host`). **Normal users** go
  straight to their VMs and never see the host.

```text
$ vm install debian kali          # two systems, installed one after the other
$ vm                              # shows both and which is active
$ vm shell kali                   # a root shell in Kali
root@kali:~# apt update && apt install -y nmap
$ vm use debian                   # plain commands now run in Debian
ark@debian:/root$ python3 --version
```

## Snapshots, clones and pre-baked templates

Setting up a system takes minutes: download, `apt install`, Python, build tools, Node. Three features exist so you
**do that once** and then go back, branch or repeat in seconds. They are easy to mix up, so here is the whole picture.

| | What it is | Lives where | Use it when |
|---|---|---|---|
| **Snapshot** | A named, compressed picture of **one VM at one moment** (system files **and** `/root`). | A `.tar.gz` on the data disk, belongs to that VM. | You are about to do something risky and want an undo button. |
| **Clone** | A **second, independent VM** copied from a VM (or from one of its snapshots). | A normal VM next to the original. | You want to experiment, test an upgrade, or run two copies of an app. |
| **Template** | A **pre-baked recipe**: a VM prepared once (Python, Node, your tools) and stored so new VMs start from it. | A `.tar.gz`, yours or shared with everyone. | You create the same kind of system again and again. |

### Snapshots: an undo button

```
vm snapshot create before-upgrade --note "debian 13, working app"
apt full-upgrade          # ...and it goes wrong
vm snapshot restore before-upgrade --yes
```

* **What is saved:** the whole system (installed packages, config in `/etc`, everything you added) *and* your `/root`, plus
  the `vm setup` and package records, so the VM comes back exactly as it was.
* **What restore does:** stops what runs in that VM, unpacks the snapshot **next to** the VM, and only then switches the
  files over. If anything fails the VM is left exactly as it was. What you changed since the snapshot is gone, so snapshot first
  if you may want it.
* **Limits:** up to **5 per VM**, private to you. They are compressed (usually a third of the VM's size) and need free disk
  space; the command tells you when there is not enough. `vm snapshot delete <name>` frees it.
* **While it runs** the VM is **locked** (like during `vm update`); other VMs keep working. One such job per user at a time.
* **Crash-consistent:** files are copied as they are. A database that was mid-write is as a power cut would leave it, so stop the
  app first if you need a clean copy. Processes are not saved, only files.
* `vm snapshot list [vm]` shows them. A snapshot **survives `vm remove`**, so `vm clone debian@before-upgrade debian2` can
  bring the VM back.

### Clones: a copy that lives on its own

```
vm clone debian test            # a copy of debian as it is now -> VM "test"
vm clone debian@before-upgrade test2     # ...or from a snapshot
vm shell test
```

A clone takes seconds and **downloads nothing**. It is a full VM: its own `/root`, its own processes and ports, its own
`vm update`, services and prompt (`test@root`). Changing the clone never changes the original. The name is 1-32 characters
and cannot be the name of an installable system (`debian`, `ubuntu`, ...). After a **fresh deploy** a clone is rebuilt like any VM
(a new system of the same distro, your `/root`, the `vm setup` profiles and recorded packages), not byte for byte; use a
**snapshot or template** for exact copies, and note those live on the data disk.

### Templates: pre-baked systems

A template is a VM **baked** in advance. Starting from one skips the download and every install step.

```
vm template save debian pydev --desc "Python 3 + build tools"   # freeze a VM you prepared
vm template bake nodebox debian node python                      # or build one from scratch
vm template list
vm template use pydev api-project                                # a new VM, ready in seconds
```

* **`save`** freezes a VM you already set up. **`bake`** does everything for you: it installs the distro in a scratch VM, runs the
  `vm setup` profiles you name (`vm setup` lists them), saves the result, and **deletes the scratch VM** whatever happens.
  Baking takes as long as the setups do; it runs in the background.
* **Secrets are never baked in.** Shell history, `~/.ssh`, `~/.gnupg`, `.netrc`, git credentials, `gh`/AWS/kube/docker config,
  `.npmrc`, `.pypirc` and the pip cache are left out, so a template is safe to hand to someone else.
* **Private by default** (up to 8). An admin can add `--shared` (up to 12) so every user can `vm template use` it: a good way for a
  small team to start from the same toolchain. `vm template delete <name> [--shared]` removes one.
* A VM made from a template records its tools, so a backup restore knows what to reinstall.

### Where they are stored (and what a backup does)

Snapshots and templates live under the data directory (`vmstore/`), **not** in your encrypted backup: a system is hundreds of
MB. On a host with a disk that survives restarts they survive too; on a free tier that wipes the disk on each deploy they do
not, but your VMs, `/root` and package lists are still rebuilt as before. With repo storage on, files that are only in the repo
(placeholders) are copied as placeholders, not downloaded.

## Active ports and "Open in Proxy"

`vm ports` (and the **Systems** page of the dashboard) lists **every TCP port a VM is listening on**: the port, the address,
the process, and whether it is already published. Systems share the server's network, so a server you start inside a VM listens
on the host's `localhost`; Rconsole attributes each listening socket to the VM whose processes own it, so you only see your own.

```
python -m http.server 8000 &
vm ports                    # debian: 1 listening port(s)  8000  0.0.0.0  pid 41  python -m http.server
vm ports open 8000 demo     # same as `expose 8000 demo`  ->  https://<your-site>/demo/
vm ports close 8000
```

In the dashboard each port has an **Open in Proxy** switch. It uses the same registry as `expose`, so it shows up in
`expose list` and the Apps page, needs a login to open (making one public is an admin-only `expose --public`), and only works
for a port the VM is **really listening on right now**. Ports bound to a specific non-local address are shown with a reason,
because the proxy reaches `127.0.0.1` only.

## How it works

Each system is a root filesystem run under [proot](https://proot-me.github.io/)
(user-space `chroot` + fake root, no kernel privileges). The file navigator,
editor, `fm`, `cd` and the agent are all confined to the chosen system's
filesystem — `..` and symlinks cannot escape it. Rconsole uses **proot 5.5.0**: on
x86-64 it downloads that exact, checksum-verified build the first time it is
needed, because older proots (such as the one Ubuntu/Debian package) cannot run
Ubuntu 26.04's Rust-based coreutils (`coreutils: unknown program 'prooted-…'`).
Installs run a self-test and fail with a clear message rather than leave a broken VM.

Data lives in `<data>/vm/<user>/<system>/{rootfs,home}`. `vm reset <name> --yes`
recreates a system's files but keeps its `/root`; `vm remove` deletes both.
Existing single-VM installs from earlier versions are moved into this layout
automatically.

Things to know:

- **It is not a hard security boundary.** proot is a compatibility layer: every guest runs under the server's one
  user id. Since 1.3.8 each VM also gets **its own PID namespace** where the host allows user namespaces (`vm doctor`
  shows `pid ns:`): `ps`, `top` and `kill` then see only that VM. Where the host forbids it there is no process
  isolation; everyone sees only the harmless `/proc/{uptime,meminfo,cpuinfo,loadavg,version}` files, and `vm ps` /
  `vm kill` are the scoped view. Docker's default seccomp profile blocks namespaces: see
  [docs/security.md](security.md). Only create accounts for people you trust.
- **Long commands:** a plain command or `vm run` is cut off after 60–90 s (hosting
  platforms drop HTTP requests around 100 s). For `apt upgrade`, builds and the
  like use `vm shell` (no limit, Ctrl+C works) or append `&` to run a background job.
- No `systemd`, no mounting, no raw sockets/kernel features; some syscalls are
  emulated and a few programs misbehave. If something needs a real kernel, use a
  VPS or QEMU — see [DEPLOY.md](../DEPLOY.md).
- **Updating a system: `vm update [name]`.** Brings the VM to the latest version: an **Alpine**
  VM moves to the **newest Alpine release** (3.12 → … → 3.24.3, one release at a time, as Alpine
  recommends) and gets all newer packages; **Debian, Ubuntu and Kali** get all newer packages
  of their release (they keep the release — install a newer one next to it with
  `vm install debian:<codename>`). `--stay` updates packages only, on any system. Your `/root`
  is never touched. It is built to
  be uneventful: the VM is **locked** meanwhile (nothing can start in it), the package
  database is health-checked first, a **snapshot** is taken (hard links, so it costs almost
  no disk), the upgrade runs without questions, and the result is **verified**. If anything
  fails, times out, the server restarts, or even crashes, the snapshot is put back and the VM
  is exactly as it was. Nothing new? It says so and changes nothing. Progress: `vm status <name>`;
  details: `vm update <name> --log`. Who: **admins** must be in the host shell
  (`vm mode host`); a **normal user with one system** loses everything they had running (it all
  lives in it) and gets it back afterwards (services restart by themselves) — the name is
  optional then; with **several systems** only the updating one is locked, and the others keep
  working (name required, a picker offers them). If terminals or jobs would be stopped it asks
  for `--yes` first. If the latest release cannot be looked up
  (no network) it refuses instead of guessing. Custom Alpine repositories (e.g. `@testing`)
  are not rewritten: such a system gets packages only, and it says so.
- **proot needs `ptrace`.** If your host blocks it, `vm install` reports why and
  removes the half-built VM instead of leaving it broken. **`vm doctor`** runs a
  checklist (shell, programs, root, write access, DNS, internet, package manager)
  and shows exactly which step fails.
- Other CPU architectures (arm64, e.g. Raspberry Pi): install `proot` **5.5.0 or newer** yourself (the Dockerfile
  adds the distro one, which may be older; check `proot --version`, set `RCONSOLE_VM_PROOT` to a newer build). All four
  systems publish arm64 images.
- Hosted apps in a VM: run them inside the VM (`vm setup python`, then
  `service add app --vm debian --cwd /root/app --expose 8000:app gunicorn -b 127.0.0.1:8000 app:app`).

## `neofetch` / `fetch`

`neofetch` and `fetch` follow your current target, and adapt to your screen so
they look right on a phone as well as a desktop:

- **In a VM** they show that VM's own system — distro, package count, shell — by
  running a small `rfetch` shim that Rconsole installs inside every VM. The same
  `neofetch` works when you type it inside `vm shell`, whichever system you chose.
- **On the host** they show Rconsole's own summary of the server.
- `rfetch` always shows Rconsole's. Flags: `--no-logo`, `--plain` (VM shim).

Layout is chosen from your screen width: logo beside the details on wide screens,
a short logo above on medium ones, and details only (long values shortened with
`~`) on narrow phones. If you install the real `neofetch`/`fastfetch` in a VM it
lives at `/usr/bin/…`; call it by that path to use it instead of the shim.

## Choosing the target

| | host | vm |
|---|---|---|
| Admin default | ✅ (`RCONSOLE_DEFAULT_MODE=host`) | `vm use <name>` |
| Non-admin | ❌ always refused | ✅ the only option |
| Prefix a single command | `@host cmd` (admins) | `@vm cmd` (active) or `@vm:<name> cmd` |

All of this is enforced in one place (`sandbox.py`), so it cannot be bypassed by
calling the terminal websocket or the file APIs directly.
