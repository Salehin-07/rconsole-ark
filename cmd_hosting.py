"""Hosting commands (serve / gunicorn) plus hosted-process management."""
import os
import signal
import subprocess
import sys
from pathlib import Path

import commands
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


def _launch(ctx, port, wsgi_spec=None):
    if not (1024 <= port <= 65535):
        return f"Port must be between 1024 and 65535 (got {port})"
    real_cwd = ctx["tab"]["cwd"]
    if not Path(real_cwd).is_dir():
        return f"serve: {real_cwd}: not a directory"
    folder = _folder_name(real_cwd)
    key = (folder, port)
    if key in state.ACTIVE_HOSTS:
        old = state.ACTIVE_HOSTS[key]
        state.kill_host(old["sid"], folder, port)

    if wsgi_spec:
        prefix = f"/{folder}/{port}"
        if _use_gunicorn():
            # Prefer the real gunicorn WSGI server. We launch our own bootstrap
            # module as the WSGI app so the user's app is mounted under the
            # proxy prefix (SCRIPT_NAME) — otherwise its absolute routes/links
            # (e.g. Flask's url_for) would collide with the main rconsole site.
            env = dict(os.environ)
            env["RCONSOLE_SPEC"] = wsgi_spec
            env["RCONSOLE_CWD"] = real_cwd
            env["RCONSOLE_PREFIX"] = prefix
            # Ensure the bootstrap module is importable wherever gunicorn runs.
            existing_pp = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (str(HERE) + os.pathsep + existing_pp).rstrip(os.pathsep)
            proc = subprocess.Popen(
                [sys.executable, "-m", "gunicorn", "_wsgi_host:application",
                 "-b", f"127.0.0.1:{port}", "--chdir", real_cwd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            )
            backend = "gunicorn"
        else:
            # Fallback: lightweight wsgiref server (same prefix-aware wrapper).
            proc = subprocess.Popen(
                [sys.executable, str(HERE / "_wsgi_host.py"),
                 wsgi_spec, str(port), real_cwd, prefix],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            backend = "wsgiref"
    else:
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "_static_host.py"), str(port), real_cwd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        backend = "http.server"

    state.register_host(ctx["sid"], folder, port, proc)
    return (f"Hosting '{real_cwd}' at /{folder}/{port}/\n"
            f"Open: /{folder}/{port}/  (process pid {proc.pid}, backend: {backend})")


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
    """List the running hosted processes (serve/gunicorn) with their PIDs."""
    sid = ctx["sid"]
    sess = state.get_session(sid)
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
        # Not a tracked host: attempt a direct OS kill as a fallback.
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
