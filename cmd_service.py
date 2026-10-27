"""``service`` — keep bots, workers and servers running (autostart + restart on crash).

::

    service add <name> [options] <command…>
    service list
    service start|stop|restart <name|all>
    service logs <name> [lines]
    service rm <name>

Options for ``add``:  ``--vm <name>`` run inside that VM  ·  ``--host`` on the server
itself (admins)  ·  ``--cwd <dir>``  ·  ``--env KEY=VALUE`` (repeatable)  ·
``--expose PORT:NAME`` publish the app at /NAME/  ·  ``--no-autostart``.

Examples::

    service add tgbot --vm debian --cwd /root/bot python bot.py
    service add site --vm debian --cwd /root/site --expose 8000:site python -m http.server 8000
    service add worker --env QUEUE=default -- celery -A tasks worker

Definitions persist (and are backed up); services start again after every reboot,
redeploy or restore. See COMMANDS.md.
"""
import commands
import distros
import sandbox
import services

USAGE = """\
service add <name> [--vm n | --host] [--cwd dir] [--env K=V]... [--expose PORT:NAME] [--no-autostart] <command…>
service list                          services and their state
service start|stop|restart <name|all>
service logs <name> [lines]           recent output
service rm <name>                     stop and delete

Example:  service add tgbot --vm debian --cwd /root/bot python bot.py"""

_VALUE_OPTS = {"--vm", "--cwd", "--env", "--expose"}
_FLAG_OPTS = {"--host", "--no-autostart"}


def _after(raw, pos, token):
    """Index in ``raw`` just past ``token`` (a possibly quoted word) searching from ``pos``."""
    i = raw.find(token, pos)
    if i < 0:
        return pos
    end = i + len(token)
    if i > 0 and raw[i - 1] in "\"'" and end < len(raw) and raw[end] == raw[i - 1]:
        end += 1
    return end


def parse_add(ctx, args):
    """Parse ``service add`` arguments. Returns ``(fields, error)``.

    The command is taken from the *raw* line so quoting inside it is preserved."""
    raw = ctx.get("raw") or " ".join(args)
    name = args[0]
    pos = _after(raw, raw.find("add") + 3 if "add" in raw else 0, name)
    opts = {"env": {}}
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--":
            pos = _after(raw, pos, "--")
            break
        if a in _FLAG_OPTS:
            opts[a[2:].replace("-", "_")] = True
            pos = _after(raw, pos, a)
            i += 1
        elif a in _VALUE_OPTS:
            if i + 1 >= len(args):
                return None, f"{a} needs a value"
            val = args[i + 1]
            pos = _after(raw, _after(raw, pos, a), val)
            if a == "--env":
                if "=" not in val or not val.split("=", 1)[0].isidentifier():
                    return None, "--env needs KEY=VALUE"
                k, v = val.split("=", 1)
                opts["env"][k] = v
            else:
                opts[a[2:]] = val
            i += 2
        elif a.startswith("--"):
            return None, f"unknown option {a}"
        else:
            break
    command = raw[pos:].strip()
    if not command:
        return None, "no command given — e.g. service add mybot --vm debian python bot.py"
    opts["name"], opts["cmd"] = name, command
    return opts, None


def _target(ctx, opts):
    """Decide where the service runs. Returns ``(target, error)``."""
    sid = ctx["sid"]
    if opts.get("host") and opts.get("vm"):
        return None, "choose --host or --vm, not both"
    if opts.get("host"):
        if not sandbox.host_allowed(sid):
            return None, "--host is for admins; use --vm <name> to run inside your VM"
        return "host", None
    vms = sandbox.installed_vms(sid)
    if opts.get("vm"):
        name = opts["vm"].lower()
        # A system you have, or one you will have again (e.g. while a restored
        # instance is still rebuilding it).
        if name not in vms and name not in distros.CATALOG:
            return None, f"unknown system '{name}' (yours: {', '.join(vms) or 'none'})"
        return f"vm:{name}", None
    cur = sandbox.current_target(sid)
    if cur:
        return cur, None
    if len(vms) > 1:
        return None, f"several systems installed — say which: --vm {vms[0]} (yours: {', '.join(vms)})"
    return None, "you have no system yet — `vm install` first, or pass --vm <name>"


def _fmt_age(s):
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    return f"{s // 3600}h {s % 3600 // 60}m"


def _line(i):
    where = i["target"].replace("vm:", "") if i["target"] != "host" else "host"
    dot = {"running": "●", "stopped": "○"}.get(i["state"], "◐")
    extra = ""
    if i["state"] == "running":
        extra = f" {_fmt_age(i['uptime'])}"
    elif i["state"] in ("backoff", "restarting"):
        extra = f" retry in {i['retry_in']}s" + (f", last exit {i['last_exit']}" if i["last_exit"] is not None else "")
    exp = f" → /{i['expose'][1]}/" if i.get("expose") else ""
    head = f"{dot} {i['name']} — {i['state']}{extra} · {where} · restarts {i['restarts']}{exp}"
    out = [head, f"    {i['cmd']}"]
    if i["state"] in ("backoff", "starting") and i["last_error"]:
        out.append(f"    ! {i['last_error']}")
    return "\n".join(out)


def _list(ctx):
    items = services.list_all() if ctx["is_admin"] else services.list_for(ctx["username"])
    if not items:
        return "No services yet.\n\nTry:  service add mybot --vm debian python bot.py"
    return "\n".join(_line(services.info(s["name"])) for s in items)


def _each(ctx, names, fn):
    if names[:1] == ["all"]:
        names = [s["name"] for s in services.list_for(ctx["username"])]
        if not names:
            return "You have no services."
    out = []
    for n in names:
        ok, msg = fn(n)
        out.append(f"{n}: {msg}")
    return "\n".join(out)


@commands.command("service")
def cmd_service(ctx, args):
    sub = args[0].lower() if args else "list"
    rest = args[1:]
    user, admin = ctx["username"], ctx["is_admin"]

    if sub in ("help", "-h", "--help"):
        return USAGE
    if sub in ("list", "ls", "status"):
        return _list(ctx)

    if sub == "add":
        if not rest:
            return USAGE
        opts, err = parse_add(ctx, rest)
        if err:
            return f"service: {err}"
        target, err = _target(ctx, opts)
        if err:
            return f"service: {err}"
        expose = None
        if opts.get("expose"):
            port_s, _, ename = opts["expose"].partition(":")
            if not port_s.isdigit() or not ename:
                return "service: --expose needs PORT:NAME, e.g. --expose 8000:site"
            expose = (int(port_s), ename.lower())
        ok, msg = services.add(opts["name"], user, opts["cmd"], target, cwd=opts.get("cwd", ""),
                               env=opts["env"], expose=expose,
                               enabled=not opts.get("no_autostart"))
        if not ok:
            return f"service: {msg}"
        services.tick()                                   # start it right away
        out = [f"Service '{opts['name'].lower()}' {msg} ({target}); "
               + ("starting now." if not opts.get("no_autostart") else "not started (--no-autostart).")]
        if expose:
            out.append(f"Exposed at /{expose[1]}/ (port {expose[0]}).")
        out.append("It restarts after crashes and starts again after every reboot or redeploy.")
        out.append(f"Watch it: service logs {opts['name'].lower()}")
        return "\n".join(out)

    if sub in ("start", "stop", "restart"):
        if not rest:
            return f"usage: service {sub} <name|all>"
        if sub == "restart":
            res = _each(ctx, rest, lambda n: services.set_enabled(n, user, admin, False))
            res = _each(ctx, rest, lambda n: services.set_enabled(n, user, admin, True))
        elif sub == "start":
            res = _each(ctx, rest, lambda n: services.set_enabled(n, user, admin, True))
        else:
            res = _each(ctx, rest, lambda n: services.set_enabled(n, user, admin, False))
        services.tick()
        return res

    if sub in ("rm", "remove", "delete", "del"):
        if not rest:
            return "usage: service rm <name>"
        return _each(ctx, rest, lambda n: services.remove(n, user, admin))

    if sub in ("logs", "log", "tail"):
        if not rest:
            return "usage: service logs <name> [lines]"
        s = services.get(rest[0])
        if not s:
            return f"service: no service called '{rest[0]}'"
        if s["owner"] != user and not admin:
            return f"service: '{rest[0]}' belongs to {s['owner']}"
        n = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 40
        text = services.logs(rest[0], n)
        i = services.info(rest[0])
        head = f"[{i['name']}: {i['state']}" + (f", last exit {i['last_exit']}" if i["last_exit"] is not None else "") + "]"
        return head + "\n" + (text.strip() or "(no output yet)")

    return f"service: unknown subcommand '{sub}'\n\n{USAGE}"

