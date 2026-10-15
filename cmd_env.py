"""Per-user environment variables (`sudo env`), injected into every session."""
import os

import commands
import state

_PROVIDER_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "XAI_API_KEY", "GROQ_API_KEY", "AZURE_OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY", "DEEPSEEK_API_KEY", "OPENCODE_API_KEY", "ZEN_API_KEY",
    "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OPENCODE_BASE_URL", "ZEN_BASE_URL",
    "OPENCODE_MODEL", "ZEN_MODEL",
)


@commands.command("env")
def cmd_env(ctx, args):
    """Manage environment variables injected into every session.

    Usage:
      sudo env                    list variables (values masked)
      sudo env set KEY=VALUE      add/update a variable (e.g. OPENAI_API_KEY=sk-...)
      sudo env unset KEY          remove a variable
      sudo env import            copy known provider keys already in the server env

    These persist for your account and are exported into every command you run,
    so opencode / other tools see your API keys automatically."""
    sid = ctx["sid"]
    if not args:
        env = state.get_env(sid)
        if not env:
            return ("No custom environment variables set. Try:\n"
                    "  sudo env set OPENAI_API_KEY=sk-...\n"
                    "  sudo env import")
        out = ["Custom environment (exported to your sessions):", ""]
        for k in sorted(env):
            v = env[k]
            masked = (v[:4] + "…" + v[-2:]) if len(v) > 8 else "***"
            out.append(f"  {k}={masked}")
        return "\n".join(out)
    sub = args[0]
    if sub == "import":
        imported = []
        for k in _PROVIDER_KEYS:
            v = os.environ.get(k)
            if v:
                state.set_env(sid, k, v)
                imported.append(k)
        if imported:
            return "Imported from server environment:\n  " + "\n  ".join(imported)
        return "No known provider keys found in the server environment to import."
    if sub == "set":
        if len(args) < 2 or "=" not in args[1]:
            return "Usage: sudo env set KEY=VALUE"
        key, val = args[1].split("=", 1)
        key, val = key.strip(), val.strip().strip('"\'')
        if not key:
            return "env: invalid key"
        state.set_env(sid, key, val)
        return f"Set {key}."
    if sub == "unset":
        if len(args) < 2:
            return "Usage: sudo env unset KEY"
        ok = state.remove_env(sid, args[1].strip())
        return f"Removed {args[1]}." if ok else f"env: {args[1]}: not set"
    return "Usage: sudo env [set KEY=val | unset KEY | import]"
