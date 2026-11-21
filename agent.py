"""``sudo agent``: open the AI agent, or run it right here in the console line by line.

The agent itself lives in :mod:`agent_core` (loop, tools, context), :mod:`agent_providers` (models),
:mod:`agent_store` (chats on disk) and :mod:`agent_web` (the page and its API). This file is the console command:

    sudo agent                    open the agent page (new chat, working directory = this tab's)
    sudo agent <task words>       open it and start the task
    sudo agent -c                 continue the most recent chat      (also --continue)
    sudo agent -r <id>            resume a chat by id                (also --resume)
    sudo agent ls                 list chats
    sudo agent -p <task>          run a task here and print the answer (no page; asks nothing, edits files freely)
    sudo agent --cli [task]       a plain line-by-line session in this console (no page, asks before acting)
    sudo agent models | providers | help
"""
from urllib.parse import quote

import agent_core as core
import agent_providers as providers
import agent_store as store
import sandbox

R, DIM, BOLD = "\x1b[0m", "\x1b[2m", "\x1b[1m"
CYAN, GREEN, YELLOW, RED = "\x1b[36m", "\x1b[32m", "\x1b[33m", "\x1b[31m"

HELP = (
    BOLD + "agent" + R + "  AI coding and operations agent\n"
    "  sudo agent                 open the agent page\n"
    "  sudo agent <task>          open it and start the task\n"
    "  sudo agent -c              continue the latest chat\n"
    "  sudo agent -r <id>         resume a chat      (sudo agent ls lists them)\n"
    "  sudo agent -p <task>       run a task here, print the result (edits freely, no questions)\n"
    "  sudo agent --cli [task]    line-by-line session in this console (asks before acting)\n"
    "  sudo agent models          models of the current provider\n"
    "  sudo agent providers       which providers are set up\n"
    "Set up providers (OpenRouter, Hugging Face, OpenAI, Anthropic, Groq, ...) in Dashboard > AI agent.\n"
    "Inside a chat: /help lists the slash commands; @file mentions a file; Esc stops a turn."
)


def _setup_text():
    return (BOLD + "No AI provider is set up yet." + R + "\nOpen Dashboard > AI agent and add an API key "
            "(OpenRouter, Hugging Face, OpenAI, Anthropic, Groq, ...).\nA key set with `sudo env set OPENROUTER_API_KEY=...` "
            "(or OPENAI_API_KEY, ANTHROPIC_API_KEY, HF_TOKEN, ...) also works.")


def _providers_text(user):
    rows = providers.public_table(user)
    out = []
    for r in rows:
        if not (r["usable"] or r["has_key"] or r["custom"]):
            continue
        state = (GREEN + "ready" + R) if r["usable"] else (YELLOW + "off" + R)
        src = f" ({r['key_source']})" if r["has_key"] else ""
        out.append(f"  {r['id']:<12} {state}  {r['model']}{DIM}{src}{R}")
    return "AI providers\n" + ("\n".join(out) if out else "  none set up yet - Dashboard > AI agent")


def _list_text(user):
    rows = store.list_sessions(user)[:20]
    if not rows:
        return "No chats yet. Run `sudo agent` to start one."
    import time
    out = []
    for r in rows:
        when = time.strftime("%b %d %H:%M", time.localtime(r["updated"]))
        out.append(f"  {r['id']}  {DIM}{when}{R}  {r['title'][:50]}  {DIM}{r['model']}{R}")
    return "Recent chats (open with `sudo agent -r <id>`)\n" + "\n".join(out)


def _new_session(user, cwd):
    cfg = providers.resolve(user)
    return store.create(user, cwd, provider=cfg["id"] if cfg else "", model=cfg["model"] if cfg else "",
                        mode=providers.options()["mode"], target=sandbox.current_target(user) or "")


def cmd_agent(ctx, args):
    user = ctx["sid"]
    args = list(args or [])
    try:
        sandbox.fs(user)
    except sandbox.SandboxError as e:
        return {"output": f"agent: {e}"}
    if args and args[0] in ("help", "-h", "--help", "?"):
        return {"output": HELP}
    if args and args[0] in ("providers", "provider"):
        return {"output": _providers_text(user)}
    if args and args[0] in ("ls", "list", "sessions"):
        return {"output": _list_text(user)}
    if args and args[0] in ("models", "model"):
        cfg = providers.resolve(user)
        if not cfg:
            return {"output": _setup_text()}
        try:
            ids = providers.list_models(cfg)
        except providers.ProviderError as e:
            return {"output": YELLOW + str(e) + R}
        return {"output": f"{cfg['name']}: {len(ids)} models (current: {cfg['model']})\n" + "\n".join("  " + i for i in ids[:80]) +
                          (f"\n  ... and {len(ids) - 80} more" if len(ids) > 80 else "")}
    cwd = ctx["tab"]["cwd"]
    if args and args[0] in ("-c", "--continue"):
        rows = store.list_sessions(user)
        if not rows:
            return {"output": "No chats yet. Run `sudo agent` to start one."}
        return {"redirect": "/agent?s=" + rows[0]["id"]}
    if args and args[0] in ("-r", "--resume"):
        if len(args) < 2 or not store.valid_id(args[1]) or store.get(user, args[1]) is None:
            return {"output": "No such chat. " + _list_text(user)}
        return {"redirect": "/agent?s=" + args[1]}
    if args and args[0] in ("-p", "--print"):
        task = " ".join(args[1:]).strip()
        if not task:
            return {"output": "usage: sudo agent -p <task>"}
        if not providers.resolve(user):
            return {"output": _setup_text()}
        return _headless(user, cwd, task)
    if args and args[0] == "--cli":
        if not providers.resolve(user):
            return {"output": _setup_text()}
        return _cli_routine(user, cwd, " ".join(args[1:]).strip() or None)
    q = " ".join(args).strip()
    return {"redirect": "/agent?cwd=" + quote(cwd, safe="/") + (("&q=" + quote(q)) if q else "")}


# ------------------------------------------------------------------------------ rendering events
def _render(ev, out, state):
    """Turn one run event into console text appended to ``out`` (a list of strings)."""
    t = ev.get("t")
    if t == "text":
        state["text"] = state.get("text", "") + ev["d"]
    elif t == "tool":
        _flush_text(out, state)
        out.append(CYAN + "→ " + ev["title"] + R)
    elif t == "tool_end":
        ui = ev.get("ui") or {}
        mark = GREEN + "  ✓" + R if ev.get("ok") else RED + "  ✗ " + str(ui.get("error", "failed"))[:80] + R
        body = ui.get("out") or ui.get("diff") or ""
        lines = [ln for ln in body.split("\n")][:12]
        out.append(mark + (DIM + f" {ui.get('detail', '')}" + R if ui.get("detail") and ev.get("ok") else ""))
        if lines and (ui.get("cmd") or ui.get("diff")):
            out.append(DIM + "\n".join("    " + ln for ln in lines) + R)
    elif t == "notice":
        out.append(YELLOW + ev["m"] + R)
    elif t == "error":
        out.append(RED + "error: " + ev["m"] + R)
    elif t == "compacted":
        out.append(DIM + ev["m"] + R)
    elif t == "done":
        _flush_text(out, state)
        out.append(DIM + f"— {ev.get('steps', 0)} step(s), {ev.get('secs', 0)}s, {ev.get('in', 0) + ev.get('out', 0)} tokens —" + R)


def _flush_text(out, state):
    if state.get("text"):
        out.append(state["text"].strip("\n"))
        state["text"] = ""


def _headless(user, cwd, task):
    sess = _new_session(user, cwd)
    sess.set_meta(mode="auto")
    try:
        run = core.start(user, sess, task)
    except core.Busy as e:
        return {"output": str(e)}
    seq, out, st = 0, [], {}
    import time
    end = time.time() + 55          # a console request must answer in time; the chat carries on in the page
    while time.time() < end:
        evs, done, _r = run.follow(seq, 2.0)
        for ev in evs:
            seq = ev["seq"]
            _render(ev, out, st)
        if done:
            break
    else:
        _flush_text(out, st)
        out.append(YELLOW + f"Still working. Follow it with `sudo agent -r {sess.id}`." + R)
    _flush_text(out, st)
    return {"output": "\n".join(out)}


def _cli_routine(user, cwd, initial):
    """Generator the console steps through: prompt, run a turn, show events as they come."""
    sess = _new_session(user, cwd)
    cfg = providers.resolve(user)
    banner = (BOLD + "Agent" + R + DIM + f" ({cfg['name']} / {cfg['model']}, mode {sess.meta['mode']})" + R +
              "\nType a task, /help for commands, 'exit' to leave. The full page: `sudo agent`.")
    line = initial
    first = True
    while True:
        if line is None:
            line = yield {"prompt": "agent> ", "output": banner if first else ""}
            first = False
            continue
        text = (line or "").strip()
        line = None
        if not text:
            continue
        if text in ("exit", "quit", "q", "/exit", "/quit"):
            return {"output": f"chat saved: sudo agent -r {sess.id}"}
        prompt = None
        cmd = core.handle_command(user, sess, text)
        if cmd is not None:
            if cmd.get("notice"):
                yield {"_tick": True, "output": cmd["notice"]}
            if cmd.get("action") == "pick-model":
                cfgx = providers.resolve(user, sess.meta.get("provider"), sess.meta.get("model"))
                try:
                    ids = providers.list_models(cfgx) if cfgx else []
                except providers.ProviderError as e:
                    ids = []
                    yield {"_tick": True, "output": YELLOW + str(e) + R}
                yield {"_tick": True, "output": f"Current: {cfgx['name']} / {cfgx['model']}\n" + "\n".join("  " + i for i in ids[:40]) +
                       "\nSet one with /model <id>"}
                continue
            if cmd.get("action") == "pick-mode":
                yield {"_tick": True, "output": "\n".join(f"  {m}  {core.MODE_HELP[m]}" for m in core.MODES) + "\nSet one with /mode <name>"}
                continue
            if cmd.get("action") == "sessions":
                yield {"_tick": True, "output": _list_text(user)}
                continue
            if cmd.get("action") in ("new", "open"):
                sess = _new_session(user, cwd) if cmd["action"] == "new" else store.get(user, cmd["id"])
                yield {"_tick": True, "output": GREEN + "New chat." + R}
                continue
            if not cmd.get("prompt") and cmd.get("start") != "compact":
                continue
            prompt = cmd.get("prompt")
        try:
            run = core.start(user, sess, text, prompt=prompt, job="compact" if (cmd or {}).get("start") == "compact" else "turn")
        except core.Busy as e:
            yield {"_tick": True, "output": YELLOW + str(e) + R}
            continue
        seq, st = 0, {}
        while True:
            evs, done, _r = run.follow(seq, 1.5)
            out = []
            ask = None
            for ev in evs:
                seq = ev["seq"]
                if ev.get("t") == "ask":
                    ask = ev
                    continue
                _render(ev, out, st)
            if ask:
                _flush_text(out, st)
                head = YELLOW + "? " + ask["title"] + R
                det = ask.get("detail") or ""
                if det:
                    head += "\n" + DIM + "\n".join("  " + ln for ln in det.split("\n")[:15]) + R
                opts = ask.get("options") or []
                tag = "  ".join(f"[{i + 1}] {o}" for i, o in enumerate(opts))
                ans = yield {"prompt": "choose> " if opts else "answer> ", "output": "\n".join(out + [head, tag])}
                pick = (ans or "").strip()
                if pick.isdigit() and 1 <= int(pick) <= len(opts):
                    pick = opts[int(pick) - 1]
                if ask["kind"] == "permission" and pick.lower() in ("y", "yes"):
                    pick = "Allow once"
                if ask["kind"] == "permission" and pick.lower() in ("a", "always"):
                    pick = "Always allow"
                if ask["kind"] == "permission" and pick.lower() in ("n", "no", ""):
                    pick = "Deny"
                core.answer(user, sess.id, ask["id"], pick)
                continue
            if done:
                yield {"prompt": "agent> ", "output": "\n".join(out)}
                break
            if out:
                yield {"_tick": True, "output": "\n".join(out)}
            else:
                yield {"_tick": True, "output": ""}
