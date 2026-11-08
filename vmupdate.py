"""``vm update`` — bring one installed VM to the latest packages of its release, safely.

What "update" means
-------------------
* **Alpine** moves to the **latest release** (3.12 -> 3.24.3), one release at a time as Alpine
  recommends (each release ships the keys for the next), then has every newer package.
  ``--stay`` only updates packages inside the current release.
* **Arch** is a rolling release: it gets every newer package (a full ``pacman -Syu``) and there is
  no release to move to.
* **Debian, Ubuntu, Kali** get every newer package of their release (security fixes, point
  releases) and keep the release. A newer release is installed next to the old one
  (``vm install debian:<codename>``); upgrading those in place is not supported yet.

Your ``/root`` is never touched (it lives outside the system files).

The steps (every one is checked; nothing is guessed)
----------------------------------------------------
1. **Lock** the VM. While it is locked nothing can start in it (terminals, commands,
   files, services all get "being updated"), and what was running in it is stopped.
2. **Health check** of the package database. A VM that is already broken is not touched.
3. **Refresh** the package lists and **count** what would change. Nothing to do? Done.
4. **Snapshot** the system files. Big files are hard links (free, instant, no extra disk);
   the small databases that packages edit in place (``/etc`` and the dpkg/apk/pacman databases)
   are real copies, so the snapshot cannot change underneath us.
5. **Upgrade**, with no questions asked and existing config files kept.
6. **Verify**: the system still starts programs and the package database is consistent.
7. Everything fine -> drop the snapshot. Anything wrong (a failed command, a timeout, the
   server shutting down, even a crash) -> the snapshot is put back and the VM is exactly as
   it was. A crash mid-update is rolled back automatically on the next start.

Who may update, and what stops
------------------------------
* **Admins** must be in the host shell (``vm mode host``), never inside the VM being replaced.
* **Everyone else**: with exactly one VM everything they run lives in it, so everything
  stops while it updates. With several, only the VM being updated is locked and stopped;
  work in the others carries on.
"""
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path

import config
import distros
import jobs
import sandbox
import services
import termsess

META = "update.json"
LOG = "update.log"
SNAP = "rootfs.snapshot"
FAILED = "rootfs.failed"
# Real copies in the snapshot (small, and edited in place by package scripts);
# everything else is a hard link.
COPY_DIRS = ("etc", "var/lib/dpkg", "var/lib/apk", "lib/apk", "var/lib/pacman")
MIN_FREE = 200 * 1024 * 1024          # bytes that must stay free on the data disk
MAX_PARALLEL = 2                      # updates running at once on one server
TIMEOUTS = {"health": 120, "refresh": 600, "count": 180, "upgrade": 3600, "verify": 120}
MAX_OUTPUT_LINES = 50000

_slots = threading.BoundedSemaphore(MAX_PARALLEL)
_start_lock = threading.Lock()
_threads = {}                         # (sid, vm) -> Thread
_abort = threading.Event()


class _Refused(Exception):
    """Stopped before any system file was changed (nothing to roll back)."""


class _Failed(Exception):
    """Stopped after the upgrade began: the snapshot must be put back."""


# ------------------------------------------------------------------ commands
def commands_for(family, vm_id=""):
    """Shell snippets for one package-manager family (run as root inside the VM)."""
    if family == "alpine":
        check = "apk info >/dev/null 2>&1 || { apk info; exit 1; }"
        return {"health": check, "refresh": "apk update", "count": "apk upgrade --simulate",
                "upgrade": "apk upgrade", "verify": check}
    if family == "arch":                       # rolling: lists are refreshed and upgraded together
        check = ('command -v pacman >/dev/null || { echo "pacman is missing"; exit 1; }; '
                 '[ ! -e /var/lib/pacman/db.lck ] || { echo "the package database is locked '
                 '(/var/lib/pacman/db.lck)"; exit 1; }; '
                 'out=$(pacman -Dk 2>&1) || { echo "$out"; exit 1; }')
        return {"health": check, "refresh": "pacman -Sy --noconfirm",
                "count": 'pacman -Qu; rc=$?; [ "$rc" -le 1 ] || exit "$rc"',
                "upgrade": "pacman -Su --noconfirm --noprogressbar", "verify": check}
    verb = "full-upgrade" if vm_id == "kali" else "upgrade"     # Kali is rolling
    audit = ('command -v apt-get >/dev/null || { echo "apt-get is missing"; exit 1; }; '
             'out=$(dpkg --audit 2>&1); if [ -n "$out" ]; then echo "$out"; exit 1; fi')
    return {"health": audit, "refresh": "apt-get update", "count": f"apt-get -s {verb}",
            "upgrade": f"apt-get -y -o Dpkg::Options::=--force-confold "
                       f"-o Dpkg::Options::=--force-confdef {verb}",
            "verify": audit}


def parse_count(family, text):
    """How many packages the simulated upgrade would change."""
    pat = {"alpine": r"^(\(\d+/\d+\)\s+)?Upgrading\s",
           "arch": r"^\S+\s+\S+\s+->\s+\S+"}.get(family, r"^Inst\s")       # arch: `pacman -Qu` lines
    return sum(1 for line in (text or "").splitlines() if re.match(pat, line.strip()))


# ------------------------------------------------------- Alpine release upgrades
ALPINE_REPO = re.compile(r"^https?://[^\s/]+/alpine/(?:v\d+\.\d+|latest-stable)/(?:main|community)/?$")


def alpine_hop_script(branch):
    """Point the repositories at ``branch`` ("3.13"), refresh and upgrade everything."""
    return ("set -e; sed -i -E 's#(/alpine/)(v[0-9]+\\.[0-9]+|latest-stable)(/)#\\1v" + branch
            + "\\3#' /etc/apk/repositories; apk update; apk upgrade --available")


def alpine_identify_script(version):
    """Make the system say which release it now is.

    ``/etc/alpine-release`` and ``/etc/os-release`` belong to no package in a minirootfs, so
    ``apk`` never updates them; after a release upgrade they still named the old release (and
    the upgrade looked like a failure). Written inside the VM, so symlinks resolve there.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+", version or ""):
        raise ValueError(f"bad Alpine version '{version}'")
    branch = ".".join(version.split(".")[:2])
    return (f"set -e; echo '{version}' > /etc/alpine-release; f=/etc/os-release; "
            f'if [ -L "$f" ]; then f=$(readlink -f "$f"); fi; '
            f"printf 'NAME=\"Alpine Linux\"\\nID=alpine\\nVERSION_ID={version}\\n"
            f"PRETTY_NAME=\"Alpine Linux v{branch}\"\\nHOME_URL=\"https://alpinelinux.org/\"\\n"
            f"BUG_REPORT_URL=\"https://gitlab.alpinelinux.org/alpine/aports/-/issues\"\\n' "
            f'> "$f"')


def alpine_release_plan(release_text, repos_text, latest):
    """Which releases to step through: ``(["3.13", ..., "3.24"], note)``.

    No hops (``[]``) means "stay": already on the latest, an edge/alpha system, or
    repositories the user customised (we do not rewrite what we do not understand).
    """
    first = (release_text or "").strip().splitlines()[0] if (release_text or "").strip() else ""
    cur = re.match(r"(\d+)\.(\d+)", first)
    new = re.match(r"(\d+)\.(\d+)", latest or "")
    if not cur or not new or "_" in first or "edge" in first:
        return [], "this Alpine is not on a numbered release (edge?); only packages are updated"
    cur_t, new_t = (int(cur[1]), int(cur[2])), (int(new[1]), int(new[2]))
    if new_t <= cur_t:
        return [], ""
    lines = [ln.strip() for ln in (repos_text or "").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if not lines or not all(ALPINE_REPO.match(ln) for ln in lines):
        return [], "your repository list is customised, so the release was not changed"
    if cur_t[0] != new_t[0]:
        return [f"{new_t[0]}.{new_t[1]}"], ""
    return [f"{cur_t[0]}.{m}" for m in range(cur_t[1] + 1, new_t[1] + 1)], ""


def plan_release(sid, vm):
    """What ``vm update`` will do about the release of ``vm`` (needs the network).

    Returns ``{"from", "to", "hops", "note"}`` for Alpine, else ``None``. Raises
    :class:`distros.DistroError` when the latest release cannot be looked up.
    """
    info = sandbox.distro_info(sid, vm) or {}
    if (info.get("family") or "") != "alpine":
        return None
    root = sandbox.rootfs_path(sid, vm)
    try:
        release = (root / "etc/alpine-release").read_text()
        repos = (root / "etc/apk/repositories").read_text()
    except OSError:
        return {"from": "?", "to": "?", "hops": [], "note": "could not read this system's release"}
    latest = distros.alpine_latest_version()
    hops, note = alpine_release_plan(release, repos, latest)
    return {"from": release.strip().splitlines()[0] if release.strip() else "?",
            "to": latest, "hops": hops, "note": note}


# ----------------------------------------------------------------- bookkeeping
def _meta_file(base):
    return Path(base) / META


def read_meta(sid, vm):
    try:
        return json.loads(_meta_file(sandbox.vm_dir(sid, vm)).read_text())
    except (OSError, ValueError, sandbox.SandboxError):
        return {}


def _write_meta(base, **fields):
    path = _meta_file(base)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data.update(fields)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def log_tail(sid, vm, lines=40):
    try:
        text = (sandbox.vm_dir(sid, vm) / LOG).read_text(errors="replace")
    except (OSError, sandbox.SandboxError):
        return ""
    return "\n".join(text.splitlines()[-lines:])


def summary(sid, vm):
    """One line about the last update, for ``vm status`` (empty if there was none)."""
    m = read_meta(sid, vm)
    if not m.get("finished"):
        return ""
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(m["finished"]))
    if m.get("ok"):
        return f"last update {when}: {m.get('message') or 'ok'}"
    return f"last update {when} FAILED (system unchanged): {m.get('message') or '?'}"


# -------------------------------------------------------------------- snapshot
def tree_size(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def hardlinks_work(directory):
    """Can this filesystem make hard links? (it decides how much disk a snapshot needs)"""
    a = Path(directory) / ".rc-link-a"
    b = Path(directory) / ".rc-link-b"
    try:
        a.write_text("x")
        os.link(a, b)
        return True
    except OSError:
        return False
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)


def _in_copy_zone(rel):
    return any(rel == d or rel.startswith(d + "/") for d in COPY_DIRS)


def make_snapshot(src, dst):
    """Mirror ``src`` into ``dst``: hard links, except :data:`COPY_DIRS` (real copies).

    Returns ``{"linked": n, "copied": n}``. Raises OSError on a real failure (disk full).
    """
    src, dst = Path(src), Path(dst)
    stats = {"linked": 0, "copied": 0}

    def walk(cur, rel):
        out = dst / rel if rel else dst
        out.mkdir(parents=True, exist_ok=True)
        out.chmod(0o700)                      # writable while we fill it
        with os.scandir(cur) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            erel = f"{rel}/{e.name}" if rel else e.name
            target = dst / erel
            if e.is_symlink():
                os.symlink(os.readlink(e.path), target)
            elif e.is_dir(follow_symlinks=False):
                walk(e.path, erel)
            elif e.is_file(follow_symlinks=False):
                if _in_copy_zone(erel):
                    shutil.copy2(e.path, target, follow_symlinks=False)
                    stats["copied"] += 1
                else:
                    try:
                        os.link(e.path, target)
                        stats["linked"] += 1
                    except OSError:          # no hard links here, or too many of them
                        shutil.copy2(e.path, target, follow_symlinks=False)
                        stats["copied"] += 1
            elif stat.S_ISFIFO(e.stat(follow_symlinks=False).st_mode):
                os.mkfifo(target)
            # sockets and devices are runtime leftovers; they are not part of the system
        shutil.copystat(cur, out, follow_symlinks=False)

    walk(str(src), "")
    return stats


def snapshot_needed_bytes(root, base):
    """Free space a snapshot of ``root`` needs (plus the safety margin)."""
    if hardlinks_work(base):
        need = sum(tree_size(Path(root) / d) for d in COPY_DIRS if (Path(root) / d).is_dir())
    else:
        need = tree_size(root)
    return need + MIN_FREE


def rollback(base):
    """Put the snapshot back as the system. Returns ``(ok, message)``."""
    base = Path(base)
    root, snap, failed = base / sandbox.ROOTFS_DIRNAME, base / SNAP, base / FAILED
    if not snap.is_dir():
        return False, "there is no snapshot to restore"
    shutil.rmtree(failed, ignore_errors=True)
    moved = False
    try:
        if root.exists() or root.is_symlink():
            os.replace(root, failed)
            moved = True
        os.replace(snap, root)
    except OSError as e:
        if moved and not root.exists():
            try:
                os.replace(failed, root)        # put the half-updated one back at least
            except OSError:
                pass
        return False, f"rollback failed: {e}"
    shutil.rmtree(failed, ignore_errors=True)
    return True, "restored"


# --------------------------------------------------------------- what is running
def _vm_target(vm):
    return f"vm:{vm}"


def running_in(sid, vm, everything=False):
    """What would be stopped: ``{"terminals": n, "jobs": n, "services": [names]}``."""
    with termsess.SESSIONS_LOCK:
        terms = [s for (u, _t), s in termsess.SESSIONS.items()
                 if u == sid and getattr(s, "alive", False)
                 and (everything or getattr(s, "vm", "") == vm)]
    live_jobs = [j for _jid, j in jobs.list_jobs(sid)
                 if j.alive and (everything or getattr(j, "vm", "") == vm)]
    svcs = [s["name"] for s in services.list_for(sid)
            if everything or s["target"] == _vm_target(vm)]
    return {"terminals": len(terms), "jobs": len(live_jobs), "services": svcs}


def describe_running(r):
    parts = []
    if r["terminals"]:
        parts.append(f"{r['terminals']} terminal(s)")
    if r["jobs"]:
        parts.append(f"{r['jobs']} background job(s)")
    if r["services"]:
        parts.append("service(s) " + ", ".join(r["services"]))
    return ", ".join(parts)


def has_running(r):
    return bool(r["terminals"] or r["jobs"])


def stop_running(sid, vm, everything=False):
    """Stop what runs in ``vm`` (or, with ``everything``, everything of this user)."""
    if everything:
        services.stop_for(sid)
        jobs.kill_all_jobs(sid)
        termsess.kill_all_for(sid)
    else:
        services.stop_for(sid, _vm_target(vm))
        jobs.kill_vm(sid, vm)
        termsess.kill_vm(sid, vm)


# ------------------------------------------------------------------ preconditions
def can_start(sid, vm):
    """``None`` when ``vm`` may be updated now, else the reason why not."""
    if not sandbox.supported():
        return "the VM is unavailable (needs Linux and RCONSOLE_VM=1)."
    if vm not in sandbox.installed_vms(sid):
        return f"'{vm}' is not installed (see `vm`)."
    state = sandbox.install_state(sid, vm)["state"]
    if state == "updating":
        return f"{vm} is already being updated; `vm status` shows progress."
    if sandbox.installing(sid) or sandbox.updating(sid):
        busy = ", ".join(sandbox.installing(sid) + sandbox.updating(sid))
        return f"{busy} is still being installed/updated; one at a time. See `vm status`."
    if sandbox.is_admin(sid) and sandbox.effective_mode(sid) != sandbox.HOST:
        return ("admins update from the host shell, not from inside a VM. "
                "Run `vm mode host`, then `vm update " + vm + "` again.")
    if not sandbox.find_proot():
        return "proot is missing. Run `vm install` to fetch it."
    try:
        free = shutil.disk_usage(config.VM_DIR).free
    except OSError:
        free = MIN_FREE * 2
    if free < MIN_FREE:
        return (f"only {free // (1024 * 1024)} MB of disk are free; an update needs at least "
                f"{MIN_FREE // (1024 * 1024)} MB. Free some space first.")
    return None


def stops_everything(sid, vm):
    """True for a normal user whose only system is ``vm`` and who is working in it.

    Everything they run lives there, so everything stops. (A user who may also use the
    host, and is in the host shell right now, keeps what runs there.)"""
    return (not sandbox.is_admin(sid) and sandbox.installed_vms(sid) == [vm]
            and sandbox.current_target(sid) == _vm_target(vm))


# ------------------------------------------------------------------- the update
def _exec(sid, vm, script, timeout, on_line):
    """Run ``script`` as root inside ``vm``. Returns ``(returncode, output)``.

    Uses the VM's own proot launch directly (the public paths refuse a locked VM) with
    the user's normal restrictions and *without* the user's environment variables.
    """
    sandbox._ensure_pinned_proot()
    argv = (sandbox._proot_prefix(sid, vm, sandbox.VM_HOME, sandbox.is_admin(sid))
            + [sandbox._guest_shell(sid, vm), "-c", script])
    p = subprocess.Popen(argv, env=sandbox._vm_env(sid, vm, probe=True), cwd=str(config.VM_DIR),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1,
                         start_new_session=True)
    lines = []

    def pump():
        for line in p.stdout:
            line = line.rstrip("\n")
            if len(lines) < MAX_OUTPUT_LINES:
                lines.append(line)
            on_line(line)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.time() + timeout
    rc = None
    while rc is None:
        try:
            rc = p.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            if _abort.is_set() or time.time() > deadline:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
                rc = -9
                note = ("(interrupted: server shutting down)" if _abort.is_set()
                        else f"(timed out after {timeout}s)")
                lines.append(note)
                on_line(note)
    reader.join(5)
    try:
        p.stdout.close()
    except OSError:
        pass
    return rc, "\n".join(lines)


def _progress(sid, vm, msg):
    sandbox._set_install(sid, vm, "updating", msg)


def _run_update(sid, vm, plan=None):
    """The whole sequence. Returns ``(ok, message, packages)``; may raise _Refused/_Failed."""
    base = sandbox.vm_dir(sid, vm)
    root = sandbox.rootfs_path(sid, vm)
    info = sandbox.distro_info(sid, vm) or {}
    fam = info.get("family") or "debian"
    cmds = commands_for(fam, info.get("id") or vm)
    log = open(base / LOG, "w", buffering=1)
    last = [0.0]
    label = [""]

    def on_line(line):
        log.write(line + "\n")
        now = time.time()
        if line.strip() and now - last[0] > 1.0:
            last[0] = now
            _progress(sid, vm, f"{label[0]}: {line.strip()[:60]}")

    def step(name, text, script=None):
        if _abort.is_set():
            raise _Refused("interrupted because the server is shutting down")
        label[0] = text
        _progress(sid, vm, text)
        log.write(f"\n=== {text} ===\n")
        return _exec(sid, vm, script or cmds[name], TIMEOUTS[name], on_line)

    def tail(out, first=False):
        lines = out.strip().splitlines() or ["?"]
        return (lines[0] if first else lines[-1])[:120]

    try:
        distro = distros.CATALOG.get(info.get("id") or vm)
        if distro:
            sandbox._post_setup(root, distro)        # keep the proot-friendly apt settings
            if distro.family == "arch":
                err = sandbox.arch_keyring(sid, vm)  # pacman-key cannot run in a user's shell here
                if err:
                    raise _Refused(err)
        rc, out = step("health", "checking the package database")
        if rc != 0:
            raise _Refused("the package database already has problems, so it was not touched: "
                           + tail(out, True) + " (try `vm doctor`)")
        rc, out = step("refresh", "refreshing package lists")
        if rc != 0:
            raise _Refused("could not refresh the package lists (is the network up?): " + tail(out))
        hops = (plan or {}).get("hops") or []
        n = 0
        if not hops:
            rc, out = step("count", "checking what is new")
            if rc != 0:
                raise _Refused("could not work out what to upgrade: " + tail(out))
            n = parse_count(fam, out)
            if n == 0:
                return True, "already up to date" + (f" ({plan['note']})" if plan and plan.get("note") else ""), 0

        _progress(sid, vm, "saving a snapshot to roll back to")
        log.write("\n=== snapshot ===\n")
        need = snapshot_needed_bytes(root, base)
        free = shutil.disk_usage(base).free
        if free < need:
            raise _Refused(f"not enough free disk for a safe snapshot "
                           f"(need ~{need // (1024 * 1024)} MB, have {free // (1024 * 1024)} MB)")
        _write_meta(base, phase="snapshot")
        shutil.rmtree(base / SNAP, ignore_errors=True)
        try:
            stats = make_snapshot(root, base / SNAP)
        except OSError as e:
            shutil.rmtree(base / SNAP, ignore_errors=True)
            raise _Refused(f"could not save a snapshot ({e}); the system was not changed")
        log.write(f"snapshot: {stats['linked']} files linked, {stats['copied']} copied\n")
        _write_meta(base, phase="upgrade")

        try:
            if hops:
                start_rel = plan["from"]
                for i, branch in enumerate(hops, 1):
                    rc, out = step("upgrade", f"moving Alpine to {branch} (step {i}/{len(hops)})",
                                   alpine_hop_script(branch))
                    if rc != 0:
                        raise _Failed(f"the step to Alpine {branch} failed: " + tail(out))
                    rc, out = step("verify", f"checking the system after {branch}")
                    if rc != 0:
                        raise _Failed(f"the package database broke at Alpine {branch}: "
                                      + tail(out, True))
                    err = sandbox._probe(sid, vm)
                    if err:
                        raise _Failed(f"the system stopped starting programs at Alpine {branch}: "
                                      + err[:120])
                _write_meta(base, phase="verify")
                target = hops[-1]
                # 1. the repositories must really be on the target release ...
                try:
                    repos = [ln.strip() for ln in (root / "etc/apk/repositories").read_text().splitlines()
                             if ln.strip() and not ln.strip().startswith("#")]
                except OSError:
                    repos = []
                if not repos or not all(f"/v{target}/" in ln for ln in repos):
                    raise _Failed(f"the repositories are not on Alpine {target} afterwards")
                # 2. ... and nothing may be left behind on the old release
                rc, out = step("count", "checking that nothing is left to upgrade")
                left = parse_count("alpine", out) if rc == 0 else -1
                if left != 0:
                    rc, out = step("upgrade", "finishing the upgrade", "apk upgrade --available")
                    rc, out = step("count", "checking again")
                    left = parse_count("alpine", out) if rc == 0 else -1
                    if left != 0:
                        raise _Failed(f"{left if left > 0 else 'some'} package(s) could not be moved "
                                      f"to Alpine {target}")
                # 3. only now say which release this is
                rc, out = step("verify", f"recording Alpine {plan['to']}",
                               alpine_identify_script(plan["to"]))
                if rc != 0:
                    raise _Failed("could not record the new release: " + tail(out))
                try:
                    got = (root / "etc/alpine-release").read_text().strip().splitlines()[0]
                except (OSError, IndexError):
                    got = ""
                if not got.startswith(target + "."):
                    raise _Failed(f"expected Alpine {target} afterwards but found '{got or '?'}'")
                err = sandbox._probe(sid, vm)
                if err:
                    raise _Failed("the system stopped starting programs at the end: " + err[:120])
                return True, f"Alpine {start_rel} -> {got}", len(hops)
            rc, out = step("upgrade", f"upgrading {n} package(s)")
            if rc != 0:
                raise _Failed("the upgrade command failed: " + tail(out))
            _write_meta(base, phase="verify")
            err = sandbox._probe(sid, vm)
            if err:
                raise _Failed("the system no longer starts programs: " + err[:120])
            rc, out = step("verify", "verifying the result")
            if rc != 0:
                raise _Failed("the package database is inconsistent after the upgrade: "
                              + tail(out, True))
        except _Refused as e:                        # interrupted after the upgrade began
            raise _Failed(str(e))
        return True, f"{n} package(s) upgraded", n
    finally:
        log.close()


def _finish_meta(base, ok, message, phase):
    _write_meta(base, ok=ok, message=message, phase=phase, finished=int(time.time()))


def _worker(sid, vm, plan=None):
    base = sandbox.vm_dir(sid, vm)
    try:
        _write_meta(base, phase="refresh", started=int(time.time()), finished=None,
                    ok=None, message="")
        try:
            ok, msg, n = _run_update(sid, vm, plan)
            shutil.rmtree(base / SNAP, ignore_errors=True)
            if ok and n:
                _stamp_distro(base, n, plan)
            _finish_meta(base, True, msg, "done")
        except _Refused as e:
            shutil.rmtree(base / SNAP, ignore_errors=True)
            _finish_meta(base, False, str(e), "refused")
        except Exception as e:                       # _Failed or anything unexpected
            reason = str(e) if isinstance(e, _Failed) else f"{type(e).__name__}: {e}"
            if (base / SNAP).is_dir() and read_meta(sid, vm).get("phase") in (
                    "upgrade", "verify"):
                _progress(sid, vm, "something went wrong: restoring the previous system")
                ok, why = rollback(base)
                if ok:
                    _finish_meta(base, False, f"{reason}. The previous system was restored.",
                                 "rolled-back")
                else:
                    _finish_meta(base, False, f"{reason}. {why}; snapshot kept in {base / SNAP}",
                                 "rollback-failed")
            else:
                shutil.rmtree(base / SNAP, ignore_errors=True)
                _finish_meta(base, False, reason, "refused")
    except Exception as e:                            # never leave the VM locked forever
        try:
            _finish_meta(base, False, f"{type(e).__name__}: {e}", "error")
        except OSError:
            pass
    finally:
        sandbox._clear_install(sid, vm)
        with _start_lock:
            _threads.pop((sid, vm), None)
        try:
            _slots.release()
        except ValueError:
            pass


def _stamp_distro(base, n, plan=None):
    path = Path(base) / sandbox.DISTRO_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    data["updated_at"] = int(time.time())
    if plan and plan.get("hops"):                    # the release itself changed
        data["previous_release"] = data.get("release")
        if data.get("release") != "latest":
            data["release"] = plan["hops"][-1]
        try:
            ver = (Path(base) / sandbox.ROOTFS_DIRNAME / "etc/alpine-release").read_text().split()[0]
            data["label"] = f"Alpine Linux {ver}"
        except (OSError, IndexError):
            pass
    else:
        data["packages_updated"] = int(data.get("packages_updated", 0)) + n
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def start(sid, vm, everything=None, stay=False):
    """Lock ``vm``, stop what runs in it and update it in the background.

    Returns ``(ok, message)``. ``everything`` defaults to :func:`stops_everything`.
    ``stay`` keeps the current release (packages only); by default an Alpine VM moves to
    the latest release.
    """
    why = can_start(sid, vm)
    if why:
        return False, why
    plan = None
    if not stay:
        try:
            plan = plan_release(sid, vm)
        except distros.DistroError as e:
            return False, (f"could not look up the latest release ({e}). Try again, or update "
                           f"only the packages of the current release: `vm update {vm} --stay`.")
    with _start_lock:
        why = can_start(sid, vm)
        if why:
            return False, why
        if not _slots.acquire(blocking=False):
            return False, ("other updates are running on this server; try again in a few "
                           "minutes.")
        everything = stops_everything(sid, vm) if everything is None else everything
        # Lock first, so nothing can start again while we stop what is running.
        sandbox._set_install(sid, vm, "updating", "starting")
        t = threading.Thread(target=_worker, args=(sid, vm, plan), name=f"rconsole-vm-update-{vm}",
                             daemon=True)
        _threads[(sid, vm)] = t
    try:
        stop_running(sid, vm, everything)
    except Exception:
        pass
    t.start()
    scope = "everything you had running" if everything else f"what ran in {vm}"
    if plan and plan["hops"]:
        what = (f"Moving {vm} from Alpine {plan['from']} to {plan['to']}: {len(plan['hops'])} release "
                f"step(s), one at a time ({', '.join(plan['hops'])}). This can take a while.")
    elif plan and plan["note"]:
        what = f"Updating {vm} ({plan['note']})."
    elif plan:
        what = f"Updating {vm}: already on the latest Alpine ({plan['to']}), so only packages change."
    elif stay:
        what = f"Updating the packages of {vm} (staying on its release, as asked)."
    else:
        what = (f"Updating the packages of {vm}. It stays on its release: a newer one is "
                f"installed next to it (`vm install <name>:<release>`).")
    return True, (f"{what} {vm} is locked until it finishes and {scope} was stopped. "
                  f"Progress: `vm status {vm}` (`vm update {vm} --log` shows the output). If anything "
                  f"goes wrong the previous system is restored automatically.")


# ------------------------------------------------------- shutdown and crash recovery
def abort_all(timeout=25):
    """Ask running updates to stop (and roll back); wait for them. Used at shutdown."""
    _abort.set()
    deadline = time.time() + timeout
    for t in list(_threads.values()):
        t.join(max(0.1, deadline - time.time()))


def reset():
    """Forget a previous abort (tests, or a worker that is kept alive)."""
    _abort.clear()


def _recover_one(base):
    """Fix up a VM whose update was cut off by a crash. Returns ``(ok, message)``."""
    base = Path(base)
    try:
        phase = json.loads(_meta_file(base).read_text()).get("phase")
    except (OSError, ValueError):
        phase = None
    if phase in ("upgrade", "verify") and (base / SNAP).is_dir():
        ok, msg = rollback(base)
        return ok, ("the server stopped during an update; the previous system was restored"
                    if ok else f"the server stopped during an update and {msg}")
    shutil.rmtree(base / SNAP, ignore_errors=True)
    return True, "the server stopped during an update before the system was changed"


def stale_updates():
    """``[(user_dir_name, vm_id)]`` whose update never finished (found at start-up)."""
    out = []
    try:
        users = [u for u in config.VM_DIR.iterdir()
                 if u.is_dir() and u.name not in ("bin", "cache", "tmp")]
    except OSError:
        return out
    for u in users:
        try:
            for v in (x for x in u.iterdir() if x.is_dir()):
                meta = v / META
                if not meta.is_file():
                    continue
                phase = json.loads(meta.read_text()).get("phase")
                if phase in ("refresh", "snapshot", "upgrade", "verify") \
                        and (u.name, v.name) not in _threads:
                    out.append((u.name, v.name))
        except (OSError, ValueError):
            continue
    return out


def _sweep_leftovers():
    """Delete ``rootfs.failed`` trees a shutdown interrupted while they were being removed."""
    try:
        users = [u for u in config.VM_DIR.iterdir()
                 if u.is_dir() and u.name not in ("bin", "cache", "tmp")]
    except OSError:
        return 0
    found = []
    for u in users:
        try:
            found += [v / FAILED for v in u.iterdir()
                      if v.is_dir() and (v / FAILED).is_dir()
                      and sandbox.install_state(u.name, v.name)["state"] != "updating"]
        except OSError:
            continue
    if found:
        threading.Thread(target=lambda: [shutil.rmtree(p, ignore_errors=True) for p in found],
                         daemon=True, name="rconsole-vm-sweep").start()
    return len(found)


def recover_background():
    """Roll back updates a crash interrupted. Locks those VMs until that is done."""
    if not sandbox.supported():
        return False
    _sweep_leftovers()
    stale = [(u, v) for u, v in stale_updates() if sandbox.install_state(u, v)["state"] != "updating"]
    for sid, vm in stale:
        sandbox._set_install(sid, vm, "updating", "recovering from an interrupted update")
        t = threading.Thread(target=_recovery_thread, args=(sid, vm), daemon=True,
                             name=f"rconsole-vm-recover-{vm}")
        _threads[(sid, vm)] = t
        t.start()
    return bool(stale)


def _recovery_thread(sid, vm):
    base = sandbox.vm_dir(sid, vm)
    try:
        ok, msg = _recover_one(base)
        _finish_meta(base, False, msg, "recovered" if ok else "recovery-failed")
    except Exception as e:
        try:
            _finish_meta(base, False, f"recovery failed: {e}", "recovery-failed")
        except OSError:
            pass
    finally:
        sandbox._clear_install(sid, vm)
        _threads.pop((sid, vm), None)
