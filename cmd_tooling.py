"""External tooling launcher (opencode TUI)."""
import posixpath
import shutil

import commands
import jobs
import sandbox


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
    try:
        view = sandbox.fs(ctx["sid"])
        in_vm = not view.unrestricted
        local_host = view.host("node_modules/.bin/opencode", cwd)
    except (OSError, sandbox.SandboxError) as e:
        return f"opencode: {e}"
    if local_host.exists():
        # `cwd` is a guest path in VM mode, a host path otherwise: both are
        # what the launched command sees.
        bin_cmd = posixpath.join(cwd, "node_modules/.bin/opencode")
    else:
        # `shutil.which` only knows the host PATH; inside the VM install locally.
        bin_cmd = None if in_vm else shutil.which("opencode")
        if not bin_cmd:
            # No global install permission on many hosts, so install locally.
            jid, _ = jobs.start_job(ctx["sid"], "npm install opencode-ai", cwd)
            return {"output":
                f"opencode not found — started `npm install opencode-ai` as job {jid}.\n"
                f"When it finishes (check `jobs`), run `sudo opencode` again."
                + ("\n(In your VM: `vm run apk add nodejs npm` first if npm is missing.)"
                   if in_vm else "")}
    extra = " ".join(args)
    cmd = (bin_cmd + " " + extra).strip()
    return {"pty": True, "cmd": cmd}
