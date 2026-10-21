"""``expose`` — publish a server you started at ``https://<domain>/<name>/``.

Run your program however you like (on the host or inside a VM), then expose its port::

    python main.py &                  # a web server on port 8000
    expose 8000 myapp                 # -> https://<your-domain>/myapp/

    expose <port> <name> [--public] [--rewrite]
    expose list                       your exposures (admins see everyone's)
    expose rm <name>                  stop exposing (also: unexpose <name>)
    expose rm all                     remove all of yours

``--public``   anyone can open it without logging in (admins only; for webhooks/demos)
``--rewrite``  best effort: fix root-relative links (``/static/x.css``) in HTML/CSS so
               apps that don't know they live under ``/name/`` still load

The proxy forwards ``X-Forwarded-Prefix: /name`` (and Host/Proto), so frameworks
that support a prefix (Django ``FORCE_SCRIPT_NAME``, Flask ``ProxyFix(x_prefix=1)``,
Vite ``--base``) can serve under ``/name`` natively. WebSockets are not proxied.
"""
import socket

import commands
import exposure

USAGE = """\
expose <port> <name> [--public] [--rewrite]   publish a local port at /<name>/
expose list                                   show exposures
expose rm <name> | all                        stop exposing  (also: unexpose <name>)

  --public    open to anyone, no login (admins only)
  --rewrite   fix root-relative links in HTML/CSS (for apps unaware of /<name>/)"""


def _base_url():
    """``https://domain`` of the current request, or ``""`` outside a request."""
    try:
        from flask import has_request_context, request
        if has_request_context():
            return request.host_url.rstrip("/")
    except Exception:
        pass
    return ""


def _listening(port):
    """Is anything accepting connections on 127.0.0.1:port right now?"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _fmt(e, base):
    flags = " ".join(f for f, on in (("public", e["public"]), ("rewrite", e["rewrite"])) if on)
    up = "up" if _listening(e["port"]) else "down"
    return (f"/{e['name']}/ -> :{e['port']} [{up}] by {e['owner']}"
            + (f" ({flags})" if flags else "") + f"\n    {base}/{e['name']}/")


def _list(ctx):
    base = _base_url()
    items = exposure.list_all() if ctx["is_admin"] else exposure.list_for(ctx["username"])
    if not items:
        return "Nothing exposed yet. Try:  expose 8000 myapp"
    return "\n".join(_fmt(e, base) for e in items)


def _remove(ctx, names):
    if not names:
        return "usage: expose rm <name> | all"
    if names[0] == "all":
        mine = [e["name"] for e in exposure.list_for(ctx["username"])]
        for n in mine:
            exposure.remove(n, ctx["username"], ctx["is_admin"])
        return f"Removed {len(mine)} exposure(s)." if mine else "You have nothing exposed."
    out = []
    for n in names:
        ok, msg = exposure.remove(n, ctx["username"], ctx["is_admin"])
        out.append(f"{n}: {msg}")
    return "\n".join(out)


@commands.command("expose")
def cmd_expose(ctx, args):
    if not args or args[0] in ("help", "-h", "--help"):
        return USAGE + ("\n\n" + _list(ctx) if args == [] else "")
    sub = args[0].lower()
    if sub in ("list", "ls"):
        return _list(ctx)
    if sub in ("rm", "remove", "del", "delete", "stop"):
        return _remove(ctx, args[1:])

    flags = {a for a in args if a.startswith("--")}
    pos = [a for a in args if not a.startswith("--")]
    unknown = flags - {"--public", "--rewrite"}
    if unknown:
        return f"expose: unknown option {', '.join(sorted(unknown))}\n\n{USAGE}"
    if len(pos) != 2:
        return f"usage: expose <port> <name> [--public] [--rewrite]\n(got {len(pos)} argument(s))"
    try:
        port = int(pos[0])
    except ValueError:
        return f"expose: '{pos[0]}' is not a port number\n\n{USAGE}"
    name = pos[1].lower()
    public = "--public" in flags
    if public and not ctx["is_admin"]:
        return "expose: --public is for admins only (it removes the login requirement)."
    ok, msg = exposure.add(name, port, ctx["username"], public=public, rewrite="--rewrite" in flags)
    if not ok:
        return f"expose: {msg}"
    base = _base_url()
    lines = [f"Exposed port {port} at /{name}/  ({msg})",
             f"Open: {base}/{name}/" + ("   (anyone, no login)" if public else "   (login required)")]
    if not _listening(port):
        lines.append(f"Heads-up: nothing is listening on port {port} yet. Start your app "
                     "(e.g. `python main.py &`); the link works as soon as it is up.")
    lines.append(f"Your app is served under the prefix /{name}; it receives X-Forwarded-Prefix"
                 + ("." if "--rewrite" in flags else
                    ". If its CSS/JS links break, re-run with --rewrite."))
    return "\n".join(lines)


@commands.command("unexpose")
def cmd_unexpose(ctx, args):
    return _remove(ctx, args)
