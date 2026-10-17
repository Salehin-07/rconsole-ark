"""Hosting commands (serve / gunicorn) plus hosted-process management."""
import functools
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import commands
import config
import sandbox
import state

HERE = Path(__file__).resolve().parent


def _folder_name(path):
    return Path(path).name or "root"


def _port_from_bind(val):
    """Pull a valid TCP port out of a bind string.

    Accepts '9000', ':9000', '127.0.0.1:9000', '0.0.0.0:9000', and the same
    forms with a trailing unix-socket path. Returns an int in 1..65535 or None.
    """
    val = (val or "").strip().strip("'\"")
    if not val:
        return None
    if ":" in val:
        tail = val.rsplit(":", 1)[1]
    else:
        tail = val
    tail = tail.split("/")[0]  # drop any socket path
    if tail.isdigit():
        port = int(tail)
        if 1 <= port <= 65535:
            return port
    return None


def _parse_port(args):
    """Extract a port from gunicorn-style args.

    Recognizes a bare port ('9000'), a host:port (':9000', '127.0.0.1:9000'),
    '-b :9000' / '-b 127.0.0.1:9000', and '--bind=:9000'. Returns an int or
    None when no port is present.
    """
    for i, a in enumerate(args):
        if a in ("--bind", "-b") and i + 1 < len(args):
            p = _port_from_bind(args[i + 1])
            if p is not None:
                return p
        elif a.startswith("--bind="):
            p = _port_from_bind(a.split("=", 1)[1])
            if p is not None:
                return p
        elif a.startswith("-b") and len(a) > 2 and a[2] == ":":
            p = _port_from_bind(a[3:])
            if p is not None:
                return p
    # Fall back to any token that looks like a port specification.
    for a in args:
        p = _port_from_bind(a)
        if p is not None:
            return p
    return None


@functools.lru_cache(maxsize=1)
def _use_gunicorn():
    """Return True only if the real gunicorn module can actually run here.

    gunicorn installs on platforms where it cannot start (e.g. native
    Windows), so we probe with `--version` rather than just importing it.
    When unavailable, callers fall back to the lightweight wsgiref server.
    """
    try:
        r = subprocess.run(
            [sys.executable, "-m", "gunicorn", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _log_path(folder, port):
    config.ensure_private_dir(config.LOG_DIR)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", folder) or "root"
    return config.LOG_DIR / f"{safe}-{port}.log"


def _tail(path, lines=15):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:]).rstrip()
    except OSError:
        return ""


def _build_argv(sid, mode, host_dir, guest_dir, port, wsgi_spec, prefix):
    """Return ``(Spec, backend)`` for the hosted process in the given mode."""
    if wsgi_spec is None:
        # Static files are always served by the server's own Python (nothing
        # needs to be installed in the VM); VM dirs are symlink-confined.
        import shellutil
        argv = [sys.executable, str(HERE / "_static_host.py"), str(port), str(host_dir)]
        if mode == sandbox.VM:
            argv.append("--confine")
        return sandbox.Spec(argv, str(host_dir), shellutil.build_env(sid), sandbox.HOST), "http.server"

    env = {"RCONSOLE_SPEC": wsgi_spec, "RCONSOLE_PREFIX": prefix}
    if mode == sandbox.VM:
        env.update(RCONSOLE_CWD=guest_dir, PYTHONPATH="/opt/rconsole")
        argv = ["python3", "-m", "gunicorn", "_wsgi_host:application",
                "-b", f"127.0.0.1:{port}", "--chdir", guest_dir]
        sp = sandbox.argv_spec(sid, argv, guest_dir, forced=sandbox.VM, extra_env=env,
                               extra_binds=[(str(HERE), "/opt/rconsole")])
        return sp, "gunicorn (in VM)"

    env["RCONSOLE_CWD"] = str(host_dir)
    env["PYTHONPATH"] = (str(HERE) + os.pathsep + os.environ.get("PYTHONPATH", "")).rstrip(os.pathsep)
    if _use_gunicorn():
        argv = [sys.executable, "-m", "gunicorn", "_wsgi_host:application",
                "-b", f"127.0.0.1:{port}", "--chdir", str(host_dir)]
        backend = "gunicorn"
    else:
        # Fallback: lightweight wsgiref server (same prefix-aware wrapper).
        argv = [sys.executable, str(HERE / "_wsgi_host.py"),
                wsgi_spec, str(port), str(host_dir), prefix]
        backend = "wsgiref"
    return sandbox.argv_spec(sid, argv, str(host_dir), forced=sandbox.HOST, extra_env=env), backend


def _launch(ctx, port, wsgi_spec=None):
    sid = ctx["sid"]
    if not (1024 <= port <= 65535):
        return f"Port must be between 1024 and 65535 (got {port})"
    try:
        view = sandbox.fs(sid)
        mode = sandbox.effective_mode(sid)
        guest_dir = ctx["tab"]["cwd"]
        host_dir = view.host(guest_dir, guest_dir)
    except (sandbox.SandboxError, OSError) as e:
        return f"serve: {e}"
    if not host_dir.is_dir():
        return f"serve: {guest_dir}: not a directory"
    folder = _folder_name(guest_dir)
    key = (folder, port)
    if key in state.ACTIVE_HOSTS:
        old = state.ACTIVE_HOSTS[key]
        state.kill_host(old["sid"], folder, port)

    prefix = f"/{folder}/{port}"
    try:
        sp, backend = _build_argv(sid, mode, host_dir, guest_dir, port, wsgi_spec, prefix)
    except sandbox.SandboxError as e:
        return f"serve: {e}"

    # Output goes to a per-app log (not DEVNULL) so failures are diagnosable.
    log = _log_path(folder, port)
    try:
        with open(log, "ab") as logf:
            proc = subprocess.Popen(
                sp.argv, cwd=sp.cwd, env=sp.env, stdin=subprocess.DEVNULL,
                stdout=logf, stderr=subprocess.STDOUT)
    except OSError as e:
        return f"serve: could not start: {e}"

    # A process that dies immediately (missing module, bad spec, no gunicorn in
    # the VM, ...) is far more useful reported now than discovered as a 502.
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    if proc.poll() is not None:
        hint = ""
        if mode == sandbox.VM and wsgi_spec:
            hint = ("\nHint: install it in your VM:  "
                    "vm run apk add python3 py3-pip && vm run pip install gunicorn --break-system-packages")
        return (f"serve: process exited immediately (code {proc.returncode}).\n"
                f"--- {log.name} ---\n{_tail(log)}{hint}")

    state.register_host(sid, folder, port, proc)
    where = "VM" if mode == sandbox.VM else "host"
    return (f"Hosting '{guest_dir}' ({where}) at /{folder}/{port}/\n"
            f"Open: /{folder}/{port}/  (pid {proc.pid}, backend: {backend})\n"
            f"Logs: sudo sprocess {port}")


@commands.command("serve")
def cmd_serve(ctx, args):
    if not args or not args[0].isdigit():
        return "Usage: sudo serve <port>"
    return _launch(ctx, int(args[0]))


@commands.command("gunicorn")
def cmd_gunicorn(ctx, args):
    if not args:
        return "Usage: sudo gunicorn <module:app> [-b :<port>] [<port>]"
    spec = args[0]
    if ":" not in spec:
        return "gunicorn: expected <module:app> (e.g. app:app)"
    port = _parse_port(args[1:])
    if port is None:
        # No port on the command line — ask interactively so the user can
        # always choose one (instead of silently falling back to a default and
        # colliding with the host web server's port).
        status = ""
        while True:
            prompt = "Port to bind (1024-65535) [8000]: "
            if status:
                ans = (yield {"prompt": prompt, "output": status}).strip()
            else:
                ans = (yield prompt).strip()
            if not ans:
                port = 8000
                break
            p = _port_from_bind(ans)
            if p is not None:
                port = p
                break
            status = "Please enter a valid port number (1024-65535)."
    return _launch(ctx, port, wsgi_spec=spec)


@commands.command("sprocess")
def cmd_sprocess(ctx, args):
    """List hosted processes, or `sprocess <port>` to show an app's log tail."""
    sid = ctx["sid"]
    sess = state.get_session(sid)
    if args and args[0].isdigit():
        port = int(args[0])
        for (folder, p) in (sess or {}).get("hosts", {}):
            if p == port:
                log = _log_path(folder, port)
                return f"--- {log.name} ---\n{_tail(log, 40) or '(empty)'}"
        return f"sprocess: no hosted process on port {port}."
    hosts = sess.get("hosts", {}) if sess else {}
    if not hosts:
        return "No hosted processes are running (use: sudo serve <port> / sudo gunicorn <spec> -b :<port>)."
    out = ["Running hosted processes (serve / gunicorn):", ""]
    out.append(f"  {'PORT':<7}{'FOLDER':<22}{'PID':<8}STATE")
    for (folder, port), proc in sorted(hosts.items(), key=lambda kv: kv[0][1]):
        alive = proc is not None and proc.poll() is None
        pid = str(proc.pid) if alive else "-"
        out.append(f"  {port:<7}{folder[:21]:<22}{pid:<8}{'running' if alive else 'exited'}")
    return "\n".join(out)


@commands.command("kprocess")
def cmd_kprocess(ctx, args):
    """Kill a hosted process by PID, or all hosted processes if no PID given.

    Usage:
      sudo kprocess            -> kill every running serve/gunicorn process
      sudo kprocess <pid>      -> kill the process with that PID (hosted or not)
    """
    sid = ctx["sid"]

    if args:
        try:
            pid = int(args[0])
        except ValueError:
            return f"kprocess: invalid pid: {args[0]!r}"

        # Prefer a tracked hosted process (clean teardown + proxy deregister).
        sess = state.get_session(sid)
        target = None
        if sess:
            for (folder, port), proc in sess["hosts"].items():
                if proc is not None and proc.pid == pid:
                    target = (folder, port)
                    break
        if target:
            state.kill_host(sid, target[0], target[1])
            return f"Killed hosted process {pid} (port {target[1]})."
        # Not a tracked host: attempt a direct OS kill as a fallback — but never
        # the server itself (or its supervisor), which would take everyone down.
        if pid <= 1 or pid in (os.getpid(), os.getppid()):
            return f"kprocess: refusing to kill pid {pid} (the rconsole server)."
        try:
            os.kill(pid, signal.SIGTERM)
            return f"Sent SIGTERM to process {pid}."
        except ProcessLookupError:
            return f"kprocess: no process with pid {pid}."
        except Exception as e:
            return f"kprocess: could not kill pid {pid}: {e}"

    # No PID -> kill everything this session is hosting.
    sess = state.get_session(sid)
    count = len(sess.get("hosts", {})) if sess else 0
    state.kill_all_hosts(sid)
    return f"Killed all hosted processes ({count} stopped)."
