"""External tooling launcher (opencode TUI)."""
import shutil
from pathlib import Path

import commands
import jobs


@commands.command("opencode")
def cmd_opencode(ctx, args):
    """Launch the opencode AI agent TUI (sudo opencode [args...]).

    The opencode binary is located automatically: a local install at
    ./node_modules/.bin/opencode is preferred, then one on PATH. If neither is
    found, `opencode-ai` is installed locally in the current directory as a
    background job (no global install permission required). API keys are injected
    from `sudo env`, so opencode picks them up automatically.

    Tip: if the interactive TUI is too heavy for your host, use the non-interactive
    mode instead:  sudo opencode run "your prompt"  (or `opencode run ...`)."""
    cwd = ctx["tab"]["cwd"]
    local_bin = Path(cwd) / "node_modules" / ".bin" / "opencode"
    if local_bin.exists():
        bin_cmd = str(local_bin)
    else:
        bin_cmd = shutil.which("opencode")
        if not bin_cmd:
            # No global install permission on many hosts, so install locally.
            jid, _ = jobs.start_job(ctx["sid"], "npm install opencode-ai", cwd)
            return {"output":
                f"opencode not found — started `npm install opencode-ai` as job {jid}.\n"
                f"When it finishes (check `jobs`), run `sudo opencode` again."}
    extra = " ".join(args)
    cmd = (bin_cmd + " " + extra).strip()
    return {"pty": True, "cmd": cmd}
