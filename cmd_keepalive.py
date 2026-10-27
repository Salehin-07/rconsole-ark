"""``keepalive`` — stay awake on free hosting by pinging our own public URL.

::

    keepalive            show status (public URL, interval, last ping)
    keepalive now        ping right now and show the result
    keepalive on|off     turn pinging on/off until the next restart  (admin)

Set ``RCONSOLE_KEEPALIVE=1`` / ``RCONSOLE_PUBLIC_URL`` to make it permanent (it is
on by default on Render). ``RCONSOLE_KEEPALIVE_EXTRA`` adds other URLs to ping.
"""
import time

import commands
import keepalive


def _ago(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60} min ago"


def _status_text():
    s = keepalive.status()
    lines = ["Keep-alive", "----------",
             f"  enabled   : {'yes' if s['enabled'] else 'no'}"
             + ("" if s["running"] or not s["enabled"] else "  (thread starting…)"),
             f"  every     : {s['interval'] // 60} min {s['interval'] % 60}s".replace(" 0s", ""),
             f"  public URL: {s['public_url'] or 'unknown'}"]
    for t in s["targets"]:
        lines.append(f"  pings     : {t}")
    lines.append(f"  last ping : {_ago(s['last_time'])}"
                 + (f"  ({'ok' if s['last_ok'] else 'FAILED'}, {s['last_ms']} ms)" if s["last_time"] else ""))
    lines.append(f"  total     : {s['pings']} pings, {s['failures']} failed")
    if s["last_error"]:
        lines.append(f"  last error: {s['last_error']}")
    if not s["public_url"]:
        lines += ["", "No public URL known. Set RCONSOLE_PUBLIC_URL=https://your-site.example "
                      "(Render/Fly/Railway/Koyeb are detected automatically)."]
    elif not s["enabled"]:
        lines += ["", "Off. Free instances sleep after ~15 min without visitors — "
                      "`keepalive on` (or RCONSOLE_KEEPALIVE=1) prevents that."]
    return "\n".join(lines)


@commands.command("keepalive")
def cmd_keepalive(ctx, args):
    sub = args[0].lower() if args else "status"
    if sub in ("status", "info"):
        return _status_text()
    if sub in ("now", "ping", "test"):
        if not keepalive.targets():
            return _status_text()
        out = []
        for url, ok, detail in keepalive.ping_once():
            out.append(f"{'✓' if ok else '✗'} {url}  {detail}")
        return "\n".join(out)
    if sub in ("on", "off"):
        if not ctx["is_admin"]:
            return "keepalive: only admins can change it."
        keepalive.set_enabled(sub == "on")
        return f"keepalive: {'on' if sub == 'on' else 'off'} (until restart; set RCONSOLE_KEEPALIVE to make it permanent)"
    return "usage: keepalive [status | now | on | off]"
