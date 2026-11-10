# Changelog

All notable changes. The format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## 1.4.3 — 2026-11-10

### Added
- **Music in the 📁 file browser.** Clicking an audio file (`mp3 wav ogg oga opus m4a aac flac weba`) opens it in the
  same viewer as photos and videos, with the browser's audio player (play/pause, seeking, volume) under the song's
  name. ‹ › (or ←/→, swipe) now move through the folder's photos, videos **and** songs; the arrow keys are left to the
  player so they seek. Closing the viewer stops the sound. Nothing autoplays. A format the browser can't play shows a
  message and a download button.
- `/api/media` serves these audio types too: Range requests (so songs seek), `Content-Disposition: inline`, and the
  same sandboxing `Content-Security-Policy`. Other types are still refused with 415.

### Changed
- Tests: the audio endpoint (types, Range, headers), the client/server extension lists staying in sync, and the viewer
  playing and skipping songs (`tests/test_media_shell.py`, `tests/js/ui_check.js`).

## 1.4.2 — 2026-11-10

### Added
- **`shell`** (admins only): opens an interactive window on the **host** shell, like `vm shell` does for a VM. It always
  targets the host, ignores the tab's current system, needs no `sudo`, and refuses normal users (also when
  `RCONSOLE_ALLOW_USER_HOST` is on). The launch is authorized again when the terminal starts.
- **Photos and videos in the 📁 file browser.** Clicking an image (`png jpg jpeg gif webp bmp ico avif svg`) shows it;
  clicking a video (`mp4 m4v webm ogv mov`) plays it in a player. Previous / next (buttons, ←/→, swipe) move through the
  folder's media, ⬇ downloads, Esc closes. New `/api/media` endpoint: login required, same path confinement as the rest
  of the file browser, whitelisted types only (others get 415), Range requests so videos seek, and a sandboxing
  `Content-Security-Policy` so an SVG can never run scripts. Unplayable codecs get a message and a download button.
- `tests/test_media_shell.py`.

### Changed
- `help` lists `shell`; COMMANDS.md, FEATURES.md, README.md and structure.md describe the new command and the viewer.

### Security
- `/api/media` serves only a fixed list of image and video types with `Content-Security-Policy: sandbox` and no
  script source, so even an SVG opened directly cannot run code with the console's session.

## 1.4.1 — 2026-11-08

### Added
- **Arch Linux as a system** (`vm install arch`, alias `archlinux`; also in `vm install all` and `vm list`). It is a
  rolling release, so there is exactly one release (`arch`). The root filesystem comes from the linuxcontainers mirrors
  (amd64 and arm64) and is verified against the published SHA-256, like Debian.
- Everything the other systems have works on Arch: `vm shell/run/use/status/doctor/reset/remove`, per-user `/root`,
  `neofetch`/`fetch` (with an Arch logo and the `pacman` package count), `vm setup <profile>` for `tools`, `build`,
  `python`, `node`, `rust`, `db`, `all`, `docker` and `udocker` (Arch package names; the real Docker client comes
  from Docker's static build because Arch has no client-only package), `vm update` (a full `pacman -Syu` with the
  usual lock, snapshot, verify and rollback) and the dependency backup/restore (`pacman -Qqen` is recorded and
  reinstalled after a restore).
- pacman works under proot out of the box: the space check, the download user and the sandbox are switched off (a
  fake root has no real mounts or users), signature checking stays **on**, systemd/udev hooks that cannot run in a VM
  are turned into no-ops, a mirror is written if none is active, and the first `pacman -S` creates the signing keys
  by itself (`/usr/local/bin/pacman` wrapper, the real one stays at `/usr/bin/pacman`).
- `tests/test_arch.py`.

### Fixed
- Arch: `pacman-key` failed with `/dev/fd/63: No such file or directory` on hosts where a VM has no PID namespace of
  its own (bash process substitution needs `/dev/fd` -> `/proc/self/fd`, which the restricted `/proc` lacks). The host
  now creates the signing keys itself, with a fixed command, at `vm install arch` and on `vm update arch` (which also
  repairs a half-made keyring). Inside the VM, `pacman` says to run `vm update arch` instead of printing errors.

### Changed
- `vm list` wraps its heading lines to the screen width (the longer Arch line overflowed a 34-column phone).

### Removed
- A Bengali-script alias for Kali (code, docs, tests and wiki). `kali`, `kali-linux` and `kalilinux` still work.

### Notes
- Arch is not tested in a real VM yet: the rootfs download is not reachable from the build sandbox. Everything else is
  covered by tests with faked mirrors.

## 1.4.0 — 2026-11-06

### Added
- **Admin dashboard** at `/dashboard` (admins only). One page to watch the whole instance, live (refreshes every 5 s):
  - *Overview*: CPU, memory, disk and network (container-aware: cgroup limits, so it shows what your plan allows),
    load, process count, users online, running terminals / jobs / services, uptime, **alerts** (CPU or memory
    pegged, disk nearly full, a service stuck in crash backoff, a failed system, backups unconfigured or failing, the
    factory `ark` password still in use) and one hour of history as graphs (5 / 15 / 60 min).
  - *Processes*: every service (restart, stop, start, remove, logs), every job (output, kill), every open terminal
    (close) and the busiest host processes with CPU %, memory and which user / job / terminal they belong to.
  - *Users*: who is online and when they were last seen, per-user terminals, jobs, services, apps and systems;
    **create users, promote and demote admins, reset passwords, stop everything a user runs, delete users** — the web
    counterpart of `sudo muser`, `auser`, `nauser`, `cpass` and `ruser` (they share the same code).
  - *Systems*: every user's Linux systems with state and per-system process list (stop a process), and disk usage by
    area (each system, backups, logs ...), measured in the background.
  - *Apps*: exposed ports with a live "is anything listening?" check, and hosted apps.
  - *Activity*: an **audit trail** of logins (also failed and locked-out ones), logouts, and every account or process
    change made from the dashboard or with `sudo` commands. Kept in memory and in `logs/audit.jsonl` (rotated at 1 MB).
  - *Health*: server facts, backups (with a "Back up now" button), keep-alive and update status.
- **Logout button** next to the theme and system buttons (in the ⚙ menu on phones). It does exactly what the
  `logout` command does (`POST /api/logout`): ends the login, keeps your terminals and data.
- **Dashboard link in the ⚙ menu**, rendered only for admins. The ⚙ button now also shows on wide screens for admins.
- `RCONSOLE_METRICS_INTERVAL` (default 5): seconds between dashboard samples.
- `tests/test_dashboard.py` (access control, every section, every action, audit trail, logout) and jsdom checks
  `tests/js/dashboard_check.js` plus new menu / logout checks in `tests/js/ui_check.js`.

### Design
- Dashboard: cards with a status-coloured top edge and a **trend line** under each meter, graphs with dashed
  gridlines and a **hover / touch readout** of the nearest sample, status pills, alert banners with icons, a pulsing
  live indicator, a pill-style tab bar, focus rings and reduced-motion support. Colours come from the console theme
  variables, so all 17 themes work.
- Dashboard on phones: a **bottom tab bar** with icons, two-column meter cards, every table turns into one labelled
  card per row, dialogs open as bottom sheets, and tap targets are at least 36 px.
- Logout button with a power icon that turns red on hover; in the ⚙ menu it is a full-width outlined button. The
  Dashboard link in the menu is now a highlighted row with an icon, a one-line description and an arrow.

### Changed
- `sudo auser`, `nauser`, `muser`, `cpass` and `ruser` now also write to the audit trail. `ruser` shares its
  implementation with the dashboard (`cmd_users.remove_user`).
- Non-admins get a plain 404 for `/dashboard` and 403 for its API; the page link is never sent to them.
- The dashboard refuses to demote, delete or stop *yourself* (use `sudo nauser` / `ruser`); the last admin was
  already protected.

## 1.3.11 — 2026-11-05

### Added
- Tests for the `journalctl`, `systemd-hwdb` and `udevadm` shims (`JournalctlShimTests`, `HwdbShimTests`).

### Fixed
- **`Setting up systemd` printed `/proc/ is not mounted` and `Failed to update catalog` in Debian, Ubuntu and Kali
  VMs.** systemd's install script runs `journalctl --update-catalog`, which has nothing to do in a VM (no journal, no
  real `/proc` mount). Debian-family VMs now get a `journalctl` shim in `/usr/local/bin` that skips just that call;
  every other `journalctl` command runs the real binary. Existing VMs get it on their next launch.
- **`Setting up udev` printed `Failed to write database /usr/lib/udev/hwdb.bin: Function not implemented`.** The
  hardware database cannot be written under proot and a VM has no hardware to describe. Debian-family VMs now get
  `systemd-hwdb` and `udevadm` shims that skip only `update` / `hwdb --update`; every other call runs the real tool.

## 1.3.10 — 2026-11-05

### Added
- Tests for the `ln` shim, the `/proc` whitelist and the games `PATH` (`KaliLoginNoiseTests`).

### Changed
- **VMs start in `/home`** (new tabs, the interactive window, `vm run`, services without `--cwd`, and the fallback when a
  directory is missing). The host shell is unchanged (`/home/rconsole`), and `$HOME` and `cd ~` inside a VM are still
  `/root`. `/home` is created in VMs that lack it.

### Fixed
- **Kali VM printed errors on start and after every command** (`ln: failed to create symbolic link '/dev/fd': Permission
  denied`, same for `/dev/stdin`, `/dev/stdout`, `/dev/stderr`, and `bash: /proc/sys/kernel/random/uuid: No such file
  or directory`, same for `boot_id`). Kali's login scripts try to re-create the standard `/dev` links (the host's
  `/dev` is read-only here and already has them) and read two kernel files that the restricted `/proc` did not expose.
  VMs now get a small `ln` shim in `/usr/local/bin` that skips those four links when they already exist, and the
  restricted `/proc` now includes `sys/kernel/random/uuid` and `boot_id` (harmless, random values). Existing VMs get
  the shim on their next launch.
- **Programs in `/usr/games` were "command not found" on Debian, Ubuntu and Kali** (`apt install sl` then `sl`).
  Debian's `/etc/profile` resets root's `PATH` without `/usr/games` and `/usr/local/games`; Alpine installs to
  `/usr/bin`, which is why it was unaffected. Both directories are now always on `PATH` (line mode, login shells and
  scripts).

## 1.3.9 — 2026-11-05

### Fixed
- **`RCONSOLE_BACKUP_TOKEN` and `RCONSOLE_BACKUP_PASSPHRASE` reached host child processes.** The docs said server
  secrets never reach user commands, hosted apps or services, but only four secrets were stripped. Both are now removed
  from the environment of every command, terminal, job, service and hosted app on the host shell (VMs never inherited
  the server environment). Backups are unaffected: the backup engine reads them itself.
- **Documentation contradicted the code and itself.** README still described the shared PID namespace removed in
  1.3.8; DEPLOY said the free Render blueprint has a 5 GB disk (only `render.paid.yaml` does); "never sleeps",
  "as many VMs as you like", "services survive redeploys" and "open terminals reconnect" promised more than the
  implementation does; the architecture diagram said Alpine only; `RCONSOLE_VM_ROOTFS_URL` was documented as an
  Alpine-only default; the README listed Python 3.12 as passing though 3.14 is required; the shutdown backup was
  documented as 25 s (it waits about 22 s); Koyeb was listed as a free option for new users; the settings tables
  omitted `RCONSOLE_VM_PIDNS`, `RCONSOLE_RESTORE_DEPS`, `RCONSOLE_UPDATE_API`, `RCONSOLE_LOG_LEVEL`,
  `RCONSOLE_DOMAIN`, `RCONSOLE_VERSION` and the Docker-shim variables, and gave wrong defaults for
  `RCONSOLE_TRUST_PROXY`, `RCONSOLE_COOKIE_SECURE` and `RCONSOLE_USER_VM_PROC`.

### Added
- **Documentation website** (GitHub Pages, <https://salehin-07.github.io/rconsole-ark/>) built from the repository's
  own Markdown by `scripts/build_docs.py`: quick start, guides, command reference, search, dark mode, mobile layout.
  New pages: backups and restore, updating and rollback, configuration, security model, architecture, compatibility.
- `SECURITY.md` (how to report a vulnerability).
- **One authoritative environment-variable list** (`scripts/envvars.py`); the tables in README, FEATURES and the site
  are generated from it, and tests fail when the code, the list or any document disagree.
- Documentation tests for cross-document facts: version, Python version, Render blueprints, backup limits, release
  asset names, per-user limits, secret stripping, broken links.

### Notes
- No restart-sensitive behaviour changed apart from the secret stripping. Host-shell scripts that read
  `RCONSOLE_BACKUP_TOKEN` or `RCONSOLE_BACKUP_PASSPHRASE` from the environment must now get them another way.
- Enable the site once: repository **Settings → Pages → Source: GitHub Actions**.

## 1.3.8 — 2026-11-04

### Fixed
- **VMs shared one PID space.** Every VM of every user ran in the server's PID namespace: an admin's VM listed
  everyone's processes (the server, other users' VMs) and `kill` (even `kill -9 -1`) reached them all. Each VM now
  gets **its own PID namespace** (`unshare --user --pid`, new `pidns.py`, init in `pidns_init.py`): inside a VM
  `ps`, `top`, `kill` and `/proc` show only that VM's processes, with PIDs starting at 1, and `kill -9 -1` only
  reaches that VM. All commands of one VM (terminals, jobs, services, hosted apps) share its namespace, so a
  service started in one terminal is visible from another. The namespace's init reaps zombies and exits when the
  server dies, so nothing is orphaned.
- On a host that forbids user namespaces rconsole still works, without isolation: it says so in `vm doctor`
  (`pid ns:` line), and **admins' VMs no longer get the host's whole `/proc`** (everyone gets only the harmless
  system files, as normal users already did). Use `vm ps` / `vm kill` there. `RCONSOLE_VM_PIDNS=off` restores the
  old behaviour, `on` is the same as `auto` but documents intent.
- **A restore could start services on a half-built VM.** After a redeploy a VM was marked ready as soon as its system
  was reinstalled, so a service could start before its `vm setup` profiles were back and crash-loop. A VM is now
  **locked in a `restoring` state** (set before the supervisor's first pass) until its system, setups **and
  packages** are back; its services wait, then start by themselves.

### Added
- **Dependency manifest per VM** (`deps.json`, part of every backup, new `vmdeps.py`): before each backup rconsole
  records what is installed in each VM — manually installed system packages (`apk` world / `apt-mark showmanual`),
  `pip` packages of the system Python, global `npm` packages, **every Python virtualenv under `/root` with its
  packages**, and every **Node project** that had a `node_modules` (with its lockfile type). `node_modules` and
  virtualenvs are not in backups, so this list is what brings them back. It is refreshed only when something
  changed (directory mtimes), never while a VM is being restored, and a partial restore never makes it forget
  what could not be reinstalled.
- **Full restore:** after a redeploy each VM is rebuilt, its profiles re-applied, then everything in its manifest
  reinstalled (`apk add`/`apt-get install`, `pip install -r`, `npm install -g`, venvs recreated, `npm ci` / `yarn` /
  `pnpm install` in projects). Failures are per item, logged, and shown by `vm deps`. `RCONSOLE_RESTORE_DEPS=0` skips it.
- `vm ps [name]`, `vm kill <pid> [name] [--9]`, `vm deps [name]` (`capture` / `restore` run it now).
- `vm doctor` and `vm status` show the PID-namespace mode and the `restoring` state.
- Tests: PID isolation between VMs and users, `kill -1` containment, the no-namespace fallback, manifest
  parsing/merge/restore script, and "services wait for a full restore".

### Notes
- The Docker image now installs `util-linux` explicitly (`unshare`, `nsenter`). Docker's default seccomp profile
  blocks user namespaces: on plain Docker add `--security-opt seccomp=unconfined` (or a profile allowing
  `unshare`/`setns`) to get isolation; `vm doctor` tells you which mode you are in. Not verified on Render.
- Not captured: files outside `/root` (`/etc`, `/opt`, `/usr/local`), custom apt repositories, and **host-shell**
  packages (admin host mode). Keep projects under `/root`.

## 1.3.7 — 2026-11-03

### Added
- `RCONSOLE_HOST_HOME` (default `/home/rconsole`): where the host shell starts.
- `tests/test_cwd.py`: default directories, ending a terminal keeps the tab, and a real host shell's directory
  carried back on `exit`.

### Changed
- **Default directories:** the host shell now starts in `/home/rconsole`, a VM in `/root`. In the Docker image
  `/home/rconsole` is linked to the persistent workspace (`<data>/workspace`) by the entrypoint, so what you keep
  there is still part of every backup; where `/home/rconsole` does not exist or is not writable (a plain
  install), the host falls back to `<data>/workspace` as before. `RCONSOLE_HOST_HOME` overrides the path.
  **This needs a redeploy / container restart** (the entrypoint runs at start).
- **File manager:** the always-visible ☑ button next to the new file / folder buttons and the **All** button in the
  selection bar are gone. A **select-all checkbox** now appears at the start of the selection bar once at least one
  item is selected (empty, or ticked when everything is selected; clicking it selects all / none), like other file managers. Ctrl/Cmd+A still works.
- The login logo is smaller (96 px, was 132 px).
- The app is called **Rconsole** in the PWA manifest (installed app name), the home-screen title, the page titles
  and the README.

### Fixed
- **The prompt in a VM's interactive window** no longer shows the server's long host id and a bare `#`. It reads
  `<system>@root:<dir>#` (`ubuntu@root:~#`, `alpine@root:/etc#`), coloured in bash. It comes from a managed
  `~/.rconsole_prompt` that `.bashrc` / `.profile` load (existing VMs get it on their next launch; the old
  per-VM `PS1=` line is retired).
- **Returning from an interactive window no longer resets the directory.** Ending a terminal session used to delete
  the tab on the server, so the next command ran in a new tab in the default directory (and the tab's history was
  lost). The tab is kept now. On the host the directory the shell ended in is carried back too (`cd` inside
  `bash`, then `exit`); inside a VM the console returns to the directory the window was opened from.

## 1.3.6 — 2026-11-02

### Added
- **Select, compress, extract and Properties in the 📁 file manager**, working like a desktop file manager:
  - **Selecting.** Click the blank space of a row to select it (the name still opens the file or folder);
    Ctrl/Cmd+click toggles, Shift+click selects a range, **dragging a rectangle** selects every row it touches
    (Ctrl/Shift+drag adds; it auto-scrolls near the edges), double-click opens. On **phones and tablets** a
    **long-press** selects a row and dragging without lifting sweeps over more rows; after that a tap toggles rows
    and tapping a name selects instead of opening, so nothing opens by accident. A plain tap on blank space does
    nothing while nothing is selected, so scrolling never selects. **Select all** (the ☑ button, or Ctrl/Cmd+A),
    Esc clears the selection (a second Esc closes the browser), and the Delete key deletes it.
  - **A selection bar** with the item count and total size and: **All**, **Compress**, **Extract** (only when
    every selected item is an archive), **Properties** (**only when exactly one item is selected**), **Delete**
    (one confirmation for the whole selection) and ✕.
  - **Compress** to **zip, tar, tar.gz, tar.bz2, tar.xz**, tar.zst (Python 3.14+), **7z**, RAR and the one-file
    **gz, bz2, xz, zst**. Only formats that work on the server are offered; one-file formats are greyed out unless
    exactly one plain file is selected; the default name follows the selection; an existing archive is never
    overwritten (`name (2).zip`). Symlinks are stored as links, never followed.
  - **Extract** zip (also jar, war, ear, apk, whl, egg), tar, tar.gz/bz2/xz, gz, bz2, xz, zst and, through `bsdtar`,
    **7z, RAR (RAR3 and RAR5), CAB, ISO, CPIO, DEB, RPM, LZH, XAR**. *Extract to a new folder* (an archive with
    exactly one top folder becomes that folder, otherwise a folder named after the archive) or *Extract here*
    (never overwrites; skipped entries are counted).
  - **Properties**: type, location, size (folders counted without following links), size on disk, modified /
    accessed / changed, permissions (`-rw-r--r--` and octal), owner and group, link target, MIME type and, for zip
    and tar archives, the number of entries and their unpacked size.
  - Compress and extract run as **background jobs** with a progress bar and a **Cancel** button (temp files are
    removed); one job per user at a time. New endpoints: `GET /api/props`, `POST /api/archive`,
    `GET /api/archive/<id>`, `POST /api/archive/<id>/cancel`, and the `delete_many` action of `/api/fs`.
- `RCONSOLE_MAX_EXTRACT_MB` (default 2048): the most an archive may expand to when extracted (zip-bomb guard).
- `fileops.py` (the engine, standard library only) and `static/files-core.js` (the selection logic, DOM-free), with
  tests: `tests/test_fileops.py` (46), `tests/test_fileman_api.py`, `tests/js/files_check.js` (15) and a larger
  `tests/js/ui_check.js` that drives the real handlers (click, ctrl/shift, rectangle, long-press, dialogs).
- The Docker image now installs `libarchive-tools` (`bsdtar`), which gives 7z creation and 7z / RAR / CAB / ISO
  extraction. **This is a Dockerfile change, so it needs a redeploy**; the self-updater does not install packages.
  Without `bsdtar` those formats are simply not offered (zip, tar and gz/bz2/xz always work).

### Changed
- **New logo everywhere**: favicon, home-screen / PWA icons (including an Android *maskable* icon), the iOS
  touch icon, the login page (it showed ASCII art) and the README. `static/icon.svg` is gone.
- The editor's font-size buttons moved to the **top bar next to Save and Close** and are now just **−** and **+**
  (the size is in their tooltip; it is still remembered and still 16 px minimum on iOS).
- In a file row, the name now only takes the room it needs; the free space to its right is the click target for
  selecting.

### Safety
- **Extraction cannot escape the folder it is given.** Every member is created relative to a directory file
  descriptor with `O_NOFOLLOW`, so the classic *"symlink, then a file below the symlink"* archive writes nothing
  outside. `..`, absolute and Windows drive-letter names are refused; devices, FIFOs and hard links are skipped;
  setuid bits are dropped; files are never overwritten; the expanded size, the entry count (200,000) and the free
  disk space are all checked while writing (zip and gzip bombs stop early). All of it is tested with crafted archives.
- **Confined (VM) users only get `bsdtar` for the external formats**, because it refuses to write through symlinks
  by default; 7-Zip, `unrar`, `unar` and `rar` are used only for the host admin. 7z / RAR creation is refused for
  trees that contain symlinks, since those tools may follow them out of the folder.
- Archives are written to a hidden temp file and renamed into place, so an interrupted job never leaves a
  half-written archive.

### Fixed
- `test_download_and_upload_are_confined_to_the_vm` sat inside the `if __name__ == "__main__":` block of
  `tests/test_app.py`, so it **never ran** under test discovery. It now does (it needs proot, so it is skipped
  where proot is missing, exactly like its neighbours).

### Known limits
- **RAR files cannot be created without WinRAR's proprietary `rar` tool**, so *RAR* only appears in the Compress
  list for an admin who installed `rar`. Extraction works (via `bsdtar`).
- Password-protected archives are not supported (the job says so).
- There is no right-click menu, copy / cut / paste or drag-and-drop of files between folders yet.
- Folder sizes in Properties stop counting after 3 seconds or 500,000 entries and say so.
- Zstandard (`tar.zst`, `.zst`) needs Python 3.14 (the Docker image and CI have it).
- Touch behaviour was tested with simulated touch events (jsdom), not on a real phone. The long-press drag relies on
  the browser letting the page cancel scrolling once the long-press fired, which Chrome and Safari allow.

## 1.3.5 — 2026-11-01

### Added
- **A much smarter file editor** (`edit`, and files opened from the 📁 browser). It now behaves like a small code
  editor instead of a plain text box:
  - **Auto-indent**: Enter keeps the current indentation and adds one level after `{ [ (`, after `:` (Python,
    YAML, Makefile), after `then` / `do` / `else` (shell) and after an opening HTML/XML tag. Enter between a pair
    (`{|}`, `(|)`, `<div>|</div>`) opens an indented block with the closer on its own line. Python dedents after
    `return` / `pass` / `break` / `continue` / `raise`. Markdown lists continue (`-`, `*`, `1.`, `- [ ]`) and an
    empty item ends the list; `/** … */` comments continue with ` * `.
  - **Tabs or spaces, detected per file**: the file's own indentation is detected when it opens (tabs, 2 or 4
    spaces; otherwise a language default: tabs for Go and Makefiles, 2 spaces for JS/TS/JSON/YAML/HTML/CSS and
    friends, 4 for the rest). Tab inserts up to the next tab stop, Shift+Tab outdents, Tab/Shift+Tab on a selection
    (de)indents every line, and Backspace inside the indentation removes a whole level. The status bar shows the
    current style; tap it to cycle 2 spaces → 4 spaces → tabs.
  - **Auto-closing brackets and quotes**: `(` `[` `{` `"` `'` and the backtick are closed as you type, typing the
    closer steps over it, Backspace between a pair removes both, and typing one around a selection wraps it (`*`,
    `_`, `~` too in Markdown). Quotes are not paired after a word character (so `don't` works), Python `f"…"` /
    `r'…'` prefixes and `"""docstrings"""` are understood, apostrophes are not paired in prose/Rust/Lisp-style
    languages, a closer typed on a blank line steps back one level, and `<div>` in HTML/XML adds `</div>` (not for
    void tags such as `<br>`).
  - **Editor shortcuts**: Ctrl/Cmd+`/` toggle comment (~60 languages, block comments for HTML/CSS/Markdown),
    Ctrl/Cmd+`]` / `[` indent / outdent, Alt+↑/↓ move line(s), Alt+Shift+↑/↓ copy line(s), Ctrl/Cmd+Shift+K delete
    line, Ctrl/Cmd+Enter (+Shift) insert line below (above), Ctrl/Cmd+L select line, Ctrl/Cmd+J join lines, smart
    Home (first non-blank ↔ column 0), Ctrl/Cmd+G go to line.
  - **Find and replace** (Ctrl/Cmd+F, Ctrl/Cmd+H, the *Find* button, F3): match case, whole word, regular
    expressions (with `$1` in the replacement), next/previous with wrap-around, a "3 of 12" counter, Replace and
    Replace All. Esc closes the find bar first.
  - **Font size**: **A−** / **A+** in the editor's status bar (9–32 px, 16–32 px on iPhone/iPad where smaller text
    would make iOS zoom the page). The choice is remembered in the browser.
  - **Status bar**: line and column (tap to go to a line), selection size, line count, indentation style. The
    current line is highlighted, and closing the tab with unsaved changes now asks first.
  - **Symbol bar on touch devices**: Tab, outdent, undo, redo, Find, comment toggle and the symbols that are hard to
    reach on a phone keyboard (brackets, quotes, `;` `:` `=` `/` `\` `|` `&` `_` `-` `#` `$` `%` and more). They go
    through the same auto-close logic as typing.
- `static/editor-core.js`: the editor's text logic as a small DOM-free module, with its own tests
  (`tests/js/editor_check.js`, 76 checks) plus end-to-end checks of the real handlers in `tests/js/ui_check.js`.

### Changed
- Edits made by the editor go through the browser's own editing command, so **Ctrl+Z / Ctrl+Y keep working**
  after auto-indent, auto-close, comment toggles and line moves (the old Tab handler overwrote the text and
  wiped the undo history).
- Opening a file puts the cursor at the top-left (it used to be at the end while the view showed the top).
- Editor text, line numbers and highlighting share one font-size setting instead of three fixed values.

### Known limits
- Typing a closer always steps over an identical closer that is already next to the cursor (it does not track
  which ones it inserted itself), there is no multi-cursor and no word wrap, and tab characters are drawn 4 wide.
- Auto-close and auto-indent hook the browser's `beforeinput` event. Browsers do not allow cancelling it during
  IME composition, so on keyboards that compose words (some Android predictive keyboards) a few of the smart
  behaviours may not trigger; plain typing, Tab, the symbol bar and all shortcuts are unaffected.
- A few browsers reserve some shortcuts (e.g. Ctrl+L, Ctrl+J, Ctrl+Shift+K); the same actions are reachable
  with the other keys or, on touch devices, the symbol bar.

## 1.3.4 — 2026-11-01

### Added
- After an Alpine release walk the VM now **records which release it is** (`/etc/alpine-release`,
  `/etc/os-release`), so `vm status`, neofetch and `cat /etc/os-release` show the new release.
- An extra `apk upgrade` pass and a "nothing left to upgrade" check at the end of the walk.

### Fixed
- **`vm update` on Alpine rolled a *successful* release walk back.** All steps ran and the packages were upgraded
  (3.13 → 3.24 in a real VM), but the final check read `/etc/alpine-release`, which in an Alpine rootfs belongs
  to no package, so `apk` never changes it and it still said 3.13.12: "expected Alpine 3.24 but found 3.13.12",
  and the safety net restored the old system. The check is now based on facts that are really true afterwards:
  (1) every repository line points at the target release, (2) `apk upgrade` has nothing left to do (one extra
  pass is tried first; leftovers fail the update and roll it back), and only then (3) the VM is told which
  release it is: `/etc/alpine-release` and `/etc/os-release` (so `vm status`, neofetch and `cat /etc/os-release`
  show the new release) are written inside the VM, following symlinks there. A final "programs still start"
  check closes the update.
- The test double for `apk` now behaves like the real one (it no longer rewrites `/etc/alpine-release` itself),
  which is why the old tests could not see this. New tests: repositories left on another release, packages left
  behind, the identify script run for real against a regular file and a symlinked `os-release`, and the real-proot
  walk using a fake `apk` that never touches the release file.

## 1.3.3 — 2026-11-01

### Added
- **`vm update --stay`**: update the packages only and keep the current release (on any system).
- `vm install alpine:<major.minor.patch>`: exact Alpine releases such as `alpine:3.23.5` (see Fixed).

### Fixed
- **`vm install alpine:3.23.5` / `alpine:3.24.1` failed.** An exact Alpine point release was turned into a branch
  called `v3.23.5`, which does not exist. Exact releases now resolve to their branch's folder and are verified
  against the published `.sha256`. A release that really does not exist says so and suggests `alpine:3.23`.
- **Wrong Alpine numbers now get a useful answer.** Alpine releases are `3.x`; typing `alpine:1.24.1` or `1.22`
  used to say "try `alpine:1.24`" or a raw "not found: <url>". It now says there is no Alpine 1.x, shows the
  valid forms (`alpine:3.24`, `alpine:3.23.5`, `alpine`, `alpine:edge`) and suggests the likely intent (`3.24.1`).
- **`vm install alpine 3.23.5`** (a space instead of a colon) now means Alpine 3.23.5 instead of "unknown system
  '3.23.5'".

### Changed
- **`vm update` now moves an Alpine VM to the latest Alpine release** (e.g. 3.12 → 3.24.3), not just to the newest
  packages of its current release. It walks one release at a time (each release ships the signing keys of the next),
  inside the same lock + snapshot + verify + rollback protection: every step is checked (package database, programs
  still start) and the final release is confirmed. Any failing step, timeout or shutdown restores the original
  system, repositories included. The VM's metadata (`release`, label) follows; a VM installed as plain `alpine`
  keeps following "latest".
- Edge systems and customised repository lists (e.g. `@testing`) are never rewritten: they get packages only and the message says why. If the latest
  release cannot be looked up (no network) the update is refused rather than guessed.
- The start message now says exactly what will happen ("from Alpine 3.12.12 to 3.24.3: 12 release steps"), and for
  Debian, Ubuntu and Kali that the release is kept.
- Tests: the planner, the hop script, the whole walk, a failing/never-starting/wrong-version step, `--stay`,
  customised repositories, and (real proot) a complete walk and a rollback with a fake `apk`.

### Not included
- Release upgrades for Debian, Ubuntu and Kali are not supported in place (Debian needs `full-upgrade` and
  source rewrites, Ubuntu needs `do-release-upgrade`, Kali is rolling). Install the newer release next to the old
  one: `vm install debian:<codename>`, `vm install ubuntu:26.04`.

## 1.3.2 — 2026-11-01

### Added
- **`vm update [name]`: update a VM to the latest packages of its release, safely.** The new module `vmupdate.py`:
  1. **locks** the VM (nothing can start in it: commands, terminals, files, services, `vm run/shell/setup/doctor`
     and `vm remove/reset/install` all answer "being updated") and stops what ran in it;
  2. health-checks the package database (a VM that is already broken is not touched);
  3. refreshes the package lists and counts what is new (nothing new: says so, changes nothing);
  4. takes a **snapshot** of the system files: hard links for the big files (almost no extra disk), real copies
     of `/etc` and the dpkg/apk databases that packages edit in place; needs ~200 MB free and refuses otherwise;
  5. upgrades with no questions and existing config files kept (`apt-get upgrade`, `full-upgrade` on Kali,
     `apk upgrade`; it never removes packages);
  6. **verifies**: programs still start and the package database is consistent;
  7. on success drops the snapshot; on *any* problem (failed command, 1 h timeout, the server shutting down,
     even a crash) **puts the snapshot back**, so the VM is exactly as before. A crash is rolled back by itself at
     the next start, and the VM stays locked until then.
  `/root` is never touched and the update runs without the user's own environment variables.
- **Who and what:** admins must be in the host shell (`vm mode host`). A normal user with a single system has
  everything they run stopped meanwhile (it all lives in it; services restart by themselves afterwards) and may
  omit the name. With several systems only the updating one is locked and stopped, the others keep working, and
  the name is required (a picker offers them). Running terminals/jobs ask for `--yes` first. `vm status`
  shows progress and the last result, `vm update <name> --log` the last run's output.
- Services whose VM is installing or updating now wait quietly (no restart storm) and start when it is usable.
- `tests/test_vmupdate.py` (fake package manager on real files: snapshot, rollback, lock, rules, recovery,
  shutdown) and `tests/test_vmupdate_e2e.py` (a real proot VM with a fake apt-get: success, failure, broken
  database, timeout, lock, environment).

### Changed
- Server shutdown (reboot, self-update, SIGTERM) interrupts a running VM update and rolls it back *before* the
  final backup. Renaming or deleting an account, and `vm remove/reset`, wait while its VM is updating.
- Jobs and terminals remember which VM they run in (needed to stop exactly the right ones).

### Not included
- `vm update` keeps the release (trixie stays trixie). Moving to a newer release is a fresh install next to it,
  e.g. `vm install ubuntu:26.04`.

## 1.3.1 — 2026-10-31

### Added
- **`serve.py`, a launcher with a gunicorn fallback.** `uv run python serve.py` starts gunicorn
  (`gunicorn -c gunicorn.conf.py app:app`) where it works, and otherwise serves the same app with a built-in
  threaded server, so testing on a Windows device no longer fails with a missing gunicorn. The fallback is one
  process, serves the terminal websockets, and on Ctrl+C / Ctrl+Break / SIGTERM runs the same orderly shutdown
  (pause the supervisor, stop children, final backup). `RCONSOLE_SERVER=gunicorn|builtin` forces a server and
  `RCONSOLE_HOST` sets the built-in server's bind address.
- `tests/test_serve.py`: server choice, the gunicorn command, and the fallback (requests answered, shutdown run).

### Changed
- gunicorn is no longer installed on Windows (`sys_platform != 'win32'` in `pyproject.toml`, `uv.lock`,
  `requirements.txt`), because it cannot run there.
- README, FEATURES.md, DEPLOY.md and structure.md describe `serve.py`. Docker, systemd and Render are unchanged:
  they still run gunicorn directly.

## 1.3.0 — 2026-10-31

### Added
- **`rconsole update`**: checks GitHub for a newer release and updates the whole program to it, safely. `--check`
  only reports (everyone), `--yes` skips the question, `--rollback` returns to the previous version, `--force` also
  allows a git checkout or reinstalling the same / a rolled-back version. Admins install; no `sudo` prefix needed.
  - **The console is locked while it runs.** Every request except `/healthz`, static files and the status endpoint
    gets `503` (header `X-Rconsole-Updating`): no command can run, no terminal, file operation, login or hosted app can
    be used, in any tab. Open tabs show a progress screen and reload when it is over (`/api/update/status`).
  - **Safe by construction:** the release's SHA-256 is checked against its `SHA256SUMS` (no checksum, no update); the
    archive is unpacked strictly (no links, `..`, absolute paths; size limits); the new tree must have the right version,
    compile and **import in a separate process** with a throw-away data directory *before* anything is stopped or
    replaced. Failure up to that point changes nothing and stops nothing.
  - **Data stays intact:** only program files are replaced (per file, atomically), after a rollback copy of each is
    saved; `data/`, `.venv`, `.git`, `.env` are never touched, and only files a previous release installed are ever
    removed. Services, jobs and terminals are stopped and a final backup is taken first; then rconsole restarts in
    place and services start again.
  - **Changed Python dependencies** are installed into `<data>/update/site-<version>`, never into the system.
  - **Rollback and boot safety:** `rconsole update --rollback`, `python updater.py rollback` (works with a dead
    server), and an automatic rollback when a new version keeps failing to start (it must stay up ~25 s to count as
    healthy).
  - **Survives container re-creation:** the verified release is kept in `<data>/update/releases`, is part of the
    encrypted backups, and is re-applied at boot (`docker-entrypoint.sh` → `updater.py sync`, or after a restore on a
    blank disk) when the image was rebuilt from older code.
  - `RCONSOLE_UPDATE_REPO` (use your fork) and `RCONSOLE_UPDATE_TOKEN` (private repo / rate limit; never passed to user
    commands) settings.
- **`FEATURES.md`**: documents every feature, area by area.
- `rconsole version`: running version, install directory, last update result, rollback availability.

### Changed
- README, COMMANDS.md, DEPLOY.md, FREE_HOSTING.md and structure.md describe the update; a test now keeps the README
  version, FEATURES.md and the command reference in step with the code.

## 1.2.4 — 2026-10-30

### Added
- **`docker run` now works, without a daemon.** A `docker` command (a shim over udocker) is installed in the image and
  by `vm setup docker` / `vm setup udocker`. It translates `run` (`-e -v -w -u --entrypoint --name --rm --env-file`),
  `pull`, `images`, `ps`, `rm`, `rmi`, `inspect`, `search`, `login`; a run container is removed afterwards unless
  named. On the host shell it uses proot (P1) and runs anything, including static images such as hello-world. Inside a
  VM it uses fakechroot (F1), because proot cannot run inside proot: normal programs work, static binaries cannot,
  and it says so. `-p`, `--restart` and similar are ignored with a warning; `-d`, `exec`, `logs`, `build`, `compose`
  and the like explain that they need a daemon. `DOCKER_HOST` plus a real client forwards everything to that engine.
- `vm setup udocker`: the same as `vm setup docker` without the real Docker client.

### Changed
- `vm setup docker` now installs udocker and the `docker`/`drun` commands as well as the real client (which moved to
  `/usr/local/lib/docker-real` when fetched as a static binary, so it never shadows the shim).
- `drun` is now just `docker run`, always local.
- On Alpine, `vm setup docker`/`udocker` also installs GNU `findutils` (busybox `find` makes udocker print "while
  modifying attributes of image layer" for every layer) and the shim warns if it finds only busybox `find`.

## 1.2.3 — 2026-10-30

### Added
- **`drun`**: run Docker images without a Docker daemon, in the host shell (`drun hello-world`). A Render service
  and every rconsole VM is an unprivileged container, so a daemon can never run there; `drun` wraps udocker
  (proot mode) instead. It handles images that ship no `/etc` or `/root` (hello-world), reuses one container per
  image, and refuses inside a VM (proot cannot nest). Suits command-line images, not `-d`, ports, compose or
  desktops. Installed in the Docker image (`udocker==1.3.17`).

### Changed
- The `vm setup docker` closing note now points at `drun` as the no-engine option.

## 1.2.2 — 2026-10-30

(There is no 1.2.1: its tag was withdrawn before release and cannot be reused; everything from it is here.)

### Added
- **File browser: download.** Every row has a ⬇ button. A file downloads as it is; a **folder downloads as a
  `.zip`** that streams while it is built (constant memory). Symlinks inside a folder are skipped and opened
  with `O_NOFOLLOW`, so a link can never pull in a file from outside the folder or the VM.
- **File browser: upload.** **⬆ Upload files** and **⬆ Upload folder** (keeps the folder's structure). One request
  per file with live progress, three at a time; existing files are replaced only after you confirm once. Names are
  validated (no `..`, absolute or empty parts), writes are atomic, and a symlink at the target is replaced rather
  than written through. Limit per file: `RCONSOLE_MAX_UPLOAD_MB` (default 64).
- **`vm setup docker`** — Docker CLI, compose and buildx, **client only** on every system (never the engine:
  no `dockerd`, containerd, runc or iptables, which cannot run under proot and would waste ~300 MB). Where a
  release has no `docker-cli` package, Docker's static client and the compose plugin are fetched instead. The
  script says why there is no daemon and shows how to use `DOCKER_HOST` with a remote engine.
- **`vm setup kali`** — Kali VMs only (refused anywhere else): the command-line Kali toolset
  (`kali-linux-headless`, no desktop) plus the basics it needs, with a smaller fallback (`kali-tools-top10`).

### Removed
- **`sudo serve`, `sudo gunicorn` and `sudo sprocess`.** `service add … --expose PORT:NAME` (keeps it running) and
  `expose` (publishes something you started) cover them; `service logs <name>` replaces `sprocess`. The unused
  `_static_host.py` and `_wsgi_host.py` helpers went with them. `kprocess` stays.

### Changed
- `vm setup` lists `docker` and `kali`; `vm setup all` still means tools, build, python, node, rust and db; it does not include `docker` or `kali`.

## 1.2.0 — 2026-10-21

rconsole now has a clear purpose: **free hosting for bots, websites and background workers, for one person or a
small team** — on free tiers such as Render's, from a phone. This release adds what that needs. See
[FREE_HOSTING.md](FREE_HOSTING.md).

### Added
- **`service`** — a process supervisor for bots, workers and servers: named commands that **start at boot,
  restart after crashes** (exponential backoff 2 s → 1 min), run on the host or inside a VM, keep logs, and can
  be exposed on the web (`--expose PORT:NAME`). Definitions persist and are backed up.
- **Encrypted backups and automatic restore** (`backup`): snapshots of users, secrets, settings, the workspace,
  services, exposures and every VM's `/root` go — encrypted (Fernet, key from `RCONSOLE_BACKUP_PASSPHRASE`) — to a
  private git repo or a folder, every 15 min when something changed and once at shutdown. A **fresh instance
  restores itself at boot** (before secrets are generated), **rebuilds your VMs** and **re-applies your `vm setup`
  toolchains**, and services start again. The git branch is replaced on each push, so it never grows.
- **Built-in keep-alive** (`keepalive`): pings the site's own public URL so free instances never go to sleep; also
  pings extra URLs. On by default on Render.
- **`reboot`** — restart rconsole in place (final backup, stop everything, reload) without redeploying; `--hard`
  to exit the process.
- **Installable app**: web manifest and icon, so the console can be added to a phone's home screen.
- `RCONSOLE_MAX_USERS` (default 10) keeps it a small-team tool; `RCONSOLE_DEFAULT_SYSTEM` picks the default VM
  system (e.g. `alpine` on 512 MB instances); `RCONSOLE_PUBLIC_URL`.
- `FREE_HOSTING.md` and a free-tier `render.yaml`.

### Changed
- Host-mode commands now start in a persistent **workspace** (`<data>/workspace`), which is part of every backup,
  instead of the application directory.
- `render.yaml` targets Render's **free** plan; the disk-backed variant is `render.paid.yaml`.
- Dependabot updates are grouped into one pull request per ecosystem.

### Fixed
- **Slow shutdown/reload**: an open browser connection held gunicorn's graceful shutdown for the full 30 s timeout
  (and the final backup only ran after that wait). Upstream keep-alive is now off, and shutdown work starts the
  moment SIGTERM arrives. Restarts now take well under a second.
- **Orphaned processes**: stopping or reloading the server left background jobs, services and hosted apps running,
  and an old worker's supervisor could restart them while it was exiting ("address already in use" for the new
  one). Shutdown now pauses the supervisor first, stops everything it started, then takes the final backup.

### Security
- Backups are encrypted by default and refuse to run without a passphrase; tokens, passphrases and URL
  credentials are scrubbed from every message; restore only writes inside the expected folders and never follows
  symlinks or paths outside them.

## 1.1.0 — 2026-10-20

The first release of rconsole as a complete, self-hostable Linux console: private Linux VMs for every user, a
hardened security model, a phone-friendly UI, one-command publishing of your own servers, full documentation and
ready-made deployment files.

### Added
- **Several virtual machines per user** (`vm`): Alpine, Debian (default), Ubuntu and Kali — any number at once, each
  with its own root filesystem and `/root`. Runs under proot, so it needs no host privileges and works on
  locked-down hosts such as Render. `vm list`, `vm install <name>…|all`, `vm shell`, `vm run`, `vm use`, `vm mode`,
  `vm reset`, `vm remove`, `vm doctor`, and `@vm:<name>` one-off prefixes.
- **System picker and target dropdown**: commands that need a system show an inline dropdown (including installing
  another); the tab bar has a permanent dropdown, and the prompt shows where you are.
- **`vm setup <profile>`**: one command to install Python, pip, a compiler and the headers that psycopg2,
  mysqlclient, Pillow, lxml and cryptography need (`python`), plus `build`, `node`, `rust`, `db`, `tools`, `all`.
- **`expose <port> <name>`** (and `unexpose`): publish any server you started — on the host or in a VM — at
  `https://<domain>/<name>/`. Private by default, `--public` for admins, `--rewrite` for apps unaware of the prefix.
- **`neofetch` / `fetch` / `rfetch`** that follow your target (a VM's own system, or rconsole's) and fit phone screens.
- **Phone layout**: the theme button and system dropdown move into a ⚙ settings menu on small screens.
- **✕ close button** on every interactive window.
- `version` command and `/healthz` reports the version.
- **`COMMANDS.md`**: every command documented (a test keeps it complete); `DEPLOY.md` for any platform; MIT license.
- Deployment files: `Dockerfile`, `docker-compose.yml` + Caddy, nginx, Fly.io, Kubernetes, systemd, Render blueprint,
  GitHub Actions CI and a release workflow that publishes archives and a container image.
- Native commands and a native AI agent (`sudo agent`) now work inside the VM you are in.

### Changed
- Debian is the default system for `vm install`.
- Plain commands in a VM always have `~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, `~/go/bin` on `PATH`, and read
  `~/.profile` exports, so tools installed with `curl … | sh` (uv, rustup, bun, …) work outside the interactive shell.
- `pip install` works in a VM without `--break-system-packages`; installs never prompt (`DEBIAN_FRONTEND=noninteractive`).
- Configuration is entirely environment-driven (`config.py`); secrets are generated once and persisted.

### Fixed
- **Inline system picker did nothing in a real browser**: a screen-wide click handler re-focused the command box,
  closing the native dropdown the moment it opened.
- Interactive shells had no controlling terminal: `bash: cannot set terminal process group … no job control`, and
  Ctrl+C did nothing in bash-based VMs (Ubuntu, Debian, Kali).
- Ubuntu 26.04: `coreutils: unknown program 'prooted-…'` — old proot builds cannot run its Rust coreutils. rconsole
  now uses a pinned proot 5.5.0 and the install self-test explains the problem instead of leaving a broken VM.
- tzdata's "Please select the geographic area" prompt; apt's "does not have a stable CLI interface" warning.
- Kali: login script failing on `/dev/net/tun` (each VM gets a private writable `/dev/net`) and its banner.
- Pipes and redirects were quoted away (`echo hi | tr …` printed literally); native `echo`/`wc`/… shadowed pipelines.
- A corrupt user file silently recreated the default `ark`/`ark` admin; it is now quarantined.
- Hosted apps lost multiple `Set-Cookie` headers (Django's `csrftoken` + `sessionid`).

### Security
- One authorization choke point (`sandbox.py`) for every command and file access, including the terminal websocket.
  Non-admins can never reach the host; file APIs are confined to the user's VM (no `..`/symlink escapes).
- PBKDF2 hashes with a persisted random pepper (old hashes upgrade on login); login throttling; the factory
  password must be changed; minimum password length.
- Cross-site request protection (Origin / `Sec-Fetch-Site`), secure cookie flags, a distinct session cookie name,
  security headers, server secrets stripped from user processes and hosted apps.
- The VM is a compatibility layer, **not** a hard security boundary — see the README's security model.

## 0.1.0
Initial version: browser console, real PTY terminals, hosted apps, users and the AI agent.
