"""Remember what is installed in each VM, so a restore can put it all back.

A VM's system files are not backed up (they are large and re-downloaded), and
neither are ``node_modules`` or virtualenvs. Without more, a bot that ran fine
before a deploy would come back to an empty Debian that has no ``requests``.
So each backup first records, per VM, a small **manifest** (``deps.json``, a few
KB, part of the backup):

* system packages — the manually installed ones (``apk`` world / ``apt-mark showmanual``);
* ``pip`` packages installed into the system Python;
* global ``npm`` packages;
* every Python virtualenv under ``/root`` and its packages;
* every Node project under ``/root`` that had a ``node_modules``.

After a restore, once the VM is rebuilt and its ``vm setup`` profiles re-applied,
:func:`restore` reinstalls all of it. Services start only after that finished.
Not captured: files outside ``/root`` (``/etc``, ``/opt``, ``/usr/local``), custom
apt repositories, and tools such as cargo/go binaries (those live in ``/root`` and
are already part of the backup).
"""
import json
import os
import subprocess
import time

import config
import sandbox

FILE = "deps.json"
REPORT = "deps-restore.json"
CAPTURE_TIMEOUT = 90
RESTORE_TIMEOUT = 1800
SKIP_WALK = {"node_modules", ".git", ".cache", ".npm", ".cargo", ".rustup", "__pycache__", "site-packages",
             ".local", "go", ".gradle", ".m2"}
MAX_DEPTH = 4

# Prints "@@section" headers followed by that section's lines. Needs only a POSIX sh;
# Python/Node parts run only when those are installed.
PYLIST = r'''
import importlib.metadata as m, json, sys
skip = {"pip", "setuptools", "wheel", "distribute"}
out = []
for d in m.distributions():
    try:
        if (d.read_text("INSTALLER") or "").strip() not in ("pip", "uv"):
            continue
        url = d.read_text("direct_url.json") or ""
        if "file://" in url or '"editable"' in url:
            continue
        n = d.metadata["Name"]
        if n and n.lower() not in skip:
            out.append(n + "==" + d.version)
    except Exception:
        pass
print("\n".join(sorted(set(out))))
'''

CAPTURE = r'''#!/bin/sh
sect() { printf '@@%s\n' "$1"; }
sect captured
PY=$(command -v python3 || command -v python)
if command -v apk >/dev/null 2>&1; then
  sect apk; cat /etc/apk/world 2>/dev/null
elif command -v apt-mark >/dev/null 2>&1; then
  sect apt; apt-mark showmanual 2>/dev/null
fi
if [ -n "$PY" ]; then
  sect pyversion; "$PY" -c 'import sys;print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null
  sect pip; "$PY" /tmp/rconsole-pylist.py 2>/dev/null
fi
if command -v npm >/dev/null 2>&1; then
  sect npmglobal; npm ls -g --depth=0 --json 2>/dev/null
fi
cd /root 2>/dev/null || exit 0
find . -maxdepth 6 \( -name node_modules -o -name .git -o -name .cache -o -name .npm -o -name .cargo \
  -o -name .rustup -o -name site-packages -o -path ./.local -o -path ./go \) -prune -o \
  \( -name pyvenv.cfg -o -name package.json \) -type f -print 2>/dev/null | head -200 | while read -r f; do
  d=$(dirname "$f")
  case "$f" in
    */pyvenv.cfg)
      if [ -x "$d/bin/python" ]; then
        sect "venv $d"; "$d/bin/python" /tmp/rconsole-pylist.py 2>/dev/null
        "$d/bin/python" -c 'import sys;print("#python %d.%d" % sys.version_info[:2])' 2>/dev/null
      fi;;
    */package.json)
      if [ -d "$d/node_modules" ]; then
        lock=npm; [ -f "$d/yarn.lock" ] && lock=yarn; [ -f "$d/pnpm-lock.yaml" ] && lock=pnpm
        [ -f "$d/package-lock.json" ] && lock=npm-ci
        sect "node $d"; echo "$lock"
      fi;;
  esac
done
'''


def _path(sid, vm):
    return sandbox.vm_dir(sid, vm) / FILE


def load(sid, vm):
    try:
        return json.loads(_path(sid, vm).read_text())
    except (OSError, ValueError):
        return None


def _stamp(p):
    try:
        return int(os.stat(p).st_mtime_ns)
    except OSError:
        return 0


def fingerprint(sid, vm):
    """Cheap "did anything get installed or removed?" value (directory mtimes only)."""
    root = sandbox.rootfs_path(sid, vm)
    home = sandbox.home_path(sid, vm)
    parts = [root / p for p in ("var/lib/dpkg/status", "lib/apk/db/installed", "etc/apk/world",
                                "usr/local/lib/node_modules", "usr/lib/node_modules", "usr/lib/python3/dist-packages")]
    for base in ("usr/lib", "usr/local/lib"):
        try:
            for n in os.listdir(root / base):
                if n.startswith("python3"):
                    parts += [root / base / n / "site-packages", root / base / n / "dist-packages"]
        except OSError:
            pass
    items = []
    for dirpath, dirs, files in os.walk(home):
        depth = os.path.relpath(dirpath, home).count(os.sep) + (0 if dirpath == str(home) else 1)
        if "pyvenv.cfg" in files:
            for lib in ("lib", "Lib"):
                try:
                    for n in os.listdir(os.path.join(dirpath, lib)):
                        parts.append(os.path.join(dirpath, lib, n, "site-packages"))
                except OSError:
                    pass
        if "node_modules" in dirs:
            parts.append(os.path.join(dirpath, "node_modules"))
            items.append(os.path.join(dirpath, "package.json"))
        dirs[:] = [d for d in dirs if d not in SKIP_WALK and (not d.startswith(".") or d in (".venv",))]
        if depth >= MAX_DEPTH:
            dirs[:] = []
    return "|".join(str(_stamp(p)) for p in [*parts, *items])


def _run(sid, vm, script, timeout):
    root = sandbox.rootfs_path(sid, vm)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    for name, text in (("rconsole-pylist.py", PYLIST), ("rconsole-deps.sh", script)):
        p = root / "tmp" / name
        p.write_text(text)
        p.chmod(0o755)
    sp = sandbox.spec(sid, f"@vm:{vm} sh /tmp/rconsole-deps.sh", "/root")
    r = subprocess.run(sp.argv, cwd=sp.cwd, env=sp.env, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    return r


def parse(text):
    """Turn the capture script's output into the manifest sections."""
    sections, cur = {}, None
    for line in text.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            sections[cur] = []
        elif cur is not None and line.strip():
            sections[cur].append(line.rstrip())
    man = {"version": 1, "captured_at": int(time.time())}
    fam = "alpine" if "apk" in sections else "debian"
    man["system"] = {"manager": "apk" if "apk" in sections else "apt",
                     "packages": sorted(set(sections.get("apk") or sections.get("apt") or []))}
    man["pip"] = {"python": (sections.get("pyversion") or [""])[0], "packages": sections.get("pip", [])}
    npm = []
    if sections.get("npmglobal"):
        try:
            deps = json.loads("\n".join(sections["npmglobal"])).get("dependencies") or {}
            npm = sorted(f"{n}@{v.get('version')}" for n, v in deps.items() if n not in ("npm", "corepack"))
        except (ValueError, AttributeError):
            pass
    man["npm_global"] = npm
    venvs, nodes = [], []
    for name, lines in sections.items():
        if name.startswith("venv "):
            ver = next((x.split()[1] for x in lines if x.startswith("#python ")), "")
            venvs.append({"path": name[5:].lstrip("./"), "python": ver,
                          "packages": [x for x in lines if not x.startswith("#")]})
        elif name.startswith("node "):
            nodes.append({"path": name[5:].lstrip("./") or ".", "lock": (lines or ["npm"])[0]})
    man["venvs"], man["node_projects"] = venvs, nodes
    man["family"] = fam
    return man


def capture(sid, vm, force=False, timeout=CAPTURE_TIMEOUT):
    """Record this VM's manifest. Returns ``(ok, message)``; keeps the old one on any failure."""
    if sandbox.busy_reason(sid, vm) or not sandbox.installed(sid, vm):
        return False, "VM is busy or not installed"
    fp = fingerprint(sid, vm)
    old = load(sid, vm)
    if old and not force and old.get("fp") == fp:
        return True, "unchanged"
    try:
        r = _run(sid, vm, CAPTURE, timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"
    if "@@" not in r.stdout:
        return False, "the VM did not answer"
    man = parse(r.stdout)
    man["fp"] = fp
    rep = last_report(sid, vm)
    if old and rep and not rep.get("ok"):
        _merge_missing(man, old)            # the last restore was partial: never forget what it could not reinstall
    d = sandbox.distro_info(sid, vm) or {}
    man["distro"], man["release"] = d.get("id", vm), d.get("release", "")
    path = _path(sid, vm)
    tmp = path.with_name(FILE + ".part")
    tmp.write_text(json.dumps(man, indent=1, sort_keys=True))
    os.replace(tmp, path)
    return True, "captured"


def _merge_missing(new, old):
    """Keep entries of ``old`` that ``new`` lacks (by package name), for a VM whose restore was partial."""
    def name(x):
        return x.split("==")[0].lower() if "==" in x else x.rsplit("@", 1)[0].lower()
    for path, key in ((("system",), "packages"), (("pip",), "packages")):
        a, b = new[path[0]], old.get(path[0], {})
        have = {name(x) for x in a[key]}
        a[key] = sorted(set(a[key]) | {x for x in b.get(key, []) if name(x) not in have})
    have = {name(x) for x in new["npm_global"]}
    new["npm_global"] = sorted(set(new["npm_global"]) | {x for x in old.get("npm_global", []) if name(x) not in have})
    for k, ident in (("venvs", "path"), ("node_projects", "path")):
        have = {x[ident] for x in new[k]}
        new[k] += [x for x in old.get(k, []) if x[ident] not in have]


def capture_all(deadline=None):
    """Refresh every ready VM's manifest (never raises); stops at ``deadline`` (epoch seconds)."""
    done = 0
    try:
        users = [u for u in config.VM_DIR.iterdir() if u.is_dir() and u.name not in ("bin", "cache", "tmp")]
    except OSError:
        return 0
    for u in users:
        try:
            vms = [v.name for v in u.iterdir() if v.is_dir() and sandbox._valid_vm_id(v.name)]
        except OSError:
            continue
        for vm in vms:
            left = None if deadline is None else deadline - time.time()
            if left is not None and left < 8:
                return done
            try:
                ok, _ = capture(u.name, vm, timeout=min(CAPTURE_TIMEOUT, left) if left else CAPTURE_TIMEOUT)
                done += bool(ok)
            except Exception:
                pass
    return done


def summary(sid, vm):
    m = load(sid, vm)
    if not m:
        return "no dependency list recorded yet (it is taken at the next backup, or run `vm deps capture`)"
    n = lambda k: len(m.get(k) or [])
    return (f"{len(m['system']['packages'])} system packages, {len(m['pip']['packages'])} pip, "
            f"{n('npm_global')} npm global, {n('venvs')} virtualenvs, {n('node_projects')} node projects "
            f"(recorded {int(time.time() - m['captured_at']) // 60} min ago)")


# --------------------------------------------------------------------- restore
def _q(s):
    import shlex
    return shlex.quote(s)


def restore_script(man):
    """The shell script that reinstalls everything the manifest lists (best effort, never aborts)."""
    L = ["#!/bin/sh", "export DEBIAN_FRONTEND=noninteractive", "fail=0",
         'step() { printf "== %s\\n" "$1"; }', 'bad() { printf "!! %s failed\\n" "$1"; fail=1; }']
    sysp = man.get("system", {}).get("packages") or []
    if sysp:
        q = " ".join(_q(p) for p in sysp)
        L.append("step 'system packages'")
        if man["system"]["manager"] == "apk":
            L.append("apk update >/dev/null 2>&1; apk add " + q + " || { for p in " + q +
                     '; do apk add "$p" >/dev/null 2>&1 || bad "apk $p"; done; }')
        else:
            L.append("apt-get update >/dev/null 2>&1; apt-get install -y --no-install-recommends " + q +
                     " >/dev/null 2>&1 || { for p in " + q +
                     '; do apt-get install -y --no-install-recommends "$p" >/dev/null 2>&1 || bad "apt $p"; done; }')
    PY = 'PY=$(command -v python3 || command -v python)'
    pipp = man.get("pip", {}).get("packages") or []
    if pipp:
        L += ["step 'pip packages'", PY, "cat > /tmp/rc-pip.txt <<'RCEOF'", *pipp, "RCEOF",
              '[ -n "$PY" ] && { "$PY" -m pip install -r /tmp/rc-pip.txt >/dev/null 2>&1 || '
              '{ while read -r l; do "$PY" -m pip install "$l" >/dev/null 2>&1 || bad "pip $l"; done < /tmp/rc-pip.txt; }; } '
              '|| bad "pip (no python in this VM)"']
    npmg = man.get("npm_global") or []
    if npmg:
        L += ["step 'npm global packages'",
              "command -v npm >/dev/null 2>&1 && { npm install -g " + " ".join(_q(p) for p in npmg) +
              ' >/dev/null 2>&1 || bad "npm -g"; } || bad "npm (not installed)"']
    for v in man.get("venvs") or []:
        path, pk = _q("/root/" + v["path"].rstrip("/").removesuffix("/pyvenv.cfg")), v.get("packages") or []
        L += [f"step 'virtualenv {v['path']}'", PY,
              f'[ -x {path}/bin/python ] || {{ mkdir -p {path}; "$PY" -m venv {path} >/dev/null 2>&1 || '
              f'uv venv {path} >/dev/null 2>&1 || bad "venv {v["path"]}"; }}']
        if pk:
            L += ["cat > /tmp/rc-venv.txt <<'RCEOF'", *pk, "RCEOF",
                  f'{path}/bin/python -m pip install -r /tmp/rc-venv.txt >/dev/null 2>&1 || '
                  f'uv pip install --python {path}/bin/python -r /tmp/rc-venv.txt >/dev/null 2>&1 || '
                  f'bad "venv packages {v["path"]}"']
    for n in man.get("node_projects") or []:
        d = _q("/root/" + n["path"])
        tool = {"yarn": "yarn install", "pnpm": "pnpm install", "npm-ci": "npm ci"}.get(n.get("lock"), "npm install")
        L += [f"step 'node project {n['path']}'",
              f'[ -f {d}/package.json ] && (cd {d} && {tool} >/dev/null 2>&1 || npm install >/dev/null 2>&1) '
              f'|| bad "node project {n["path"]}"']
    L += ['[ "$fail" = 0 ] && echo "RESTORE-OK" || echo "RESTORE-PARTIAL"']
    return "\n".join(L) + "\n"


def restore(sid, vm, log=print):
    """Reinstall what the manifest lists inside the (rebuilt) VM. Returns ``(ok, report)``."""
    man = load(sid, vm)
    if not man:
        return True, "no dependency list recorded"
    try:
        r = _run(sid, vm, restore_script(man), RESTORE_TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        out = f"{type(e).__name__}: {e}"
    failed = [x[3:] for x in out.splitlines() if x.startswith("!! ")]
    ok = "RESTORE-OK" in out
    (sandbox.vm_dir(sid, vm) / REPORT).write_text(json.dumps(
        {"time": int(time.time()), "ok": ok, "failed": failed, "log": out[-4000:]}))
    log(f"[rconsole] dependencies for {sid}/{vm}: " + ("all restored" if ok else f"{len(failed)} failed: " + "; ".join(failed[:5])))
    return ok, "; ".join(failed)


def last_report(sid, vm):
    try:
        return json.loads((sandbox.vm_dir(sid, vm) / REPORT).read_text())
    except (OSError, ValueError):
        return None
