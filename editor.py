"""In-console file manager + editor (pure Python, no PTY / soft-keyboard).

Fully interactive through the normal line console, so they are smooth and
reliable on mobile phones where xterm + a soft keyboard is fragile.
"""
import os
import posixpath
from pathlib import Path

import commands
import sandbox


_INVALID = Path(os.devnull) / "invalid"   # any operation on it fails (ENOTDIR)


def _resolve_path(ctx, target):
    """Map a user-typed path to a real host path, confined to the user's view.

    Admins in host mode see the whole machine; VM users are confined to their
    root filesystem. Anything unresolvable maps to a path that cannot exist, so
    callers' normal "not found"/error handling applies.
    """
    try:
        view = sandbox.fs(ctx["sid"])
        p = view.host((target or "").strip(), ctx["tab"]["cwd"])
        return p.resolve() if view.unrestricted else p
    except (OSError, sandbox.SandboxError):
        return _INVALID


def _virt(ctx, host_path):
    """Path as the user sees it (VM paths are relative to the VM root)."""
    try:
        return sandbox.fs(ctx["sid"]).virt(host_path)
    except (OSError, sandbox.SandboxError):
        return str(host_path)


def _parent(ctx, host_path):
    """Parent directory that can never climb above the VM root."""
    view = sandbox.fs(ctx["sid"])
    if view.unrestricted:
        return Path(host_path).parent
    return view.host(posixpath.dirname(view.virt(host_path)) or "/", "/")


def _raw_arg(ctx):
    """Return the argument portion of the raw input line, preserving characters
    (e.g. Windows backslashes) that the shlex tokenizer would otherwise mangle."""
    raw = ctx.get("raw", "") or ""
    parts = raw.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
    except FileNotFoundError:
        return []
    except IsADirectoryError:
        return None
    except Exception:
        return None
    if data == "":
        return []
    if data.endswith("\n"):
        data = data[:-1]
    return data.split("\n")


def _write_lines(path, lines):
    content = "\n".join(lines)
    if lines:
        content += "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _render_editor(p, lines, dirty, cursor, mode, status):
    out = []
    tag = "  [modified]" if dirty else ""
    out.append(f"Editing: {p}   ({len(lines)} lines{tag})")
    out.append("-" * 60)
    if not lines:
        out.append("  (empty file)")
    for i, ln in enumerate(lines):
        mark = "> " if i == cursor else "  "
        out.append(f"{mark}{i + 1} | {ln}")
    if cursor == -1:
        out.append(">  (cursor before first line)")
    out.append("-" * 60)
    if mode == "edit":
        cur = lines[cursor] if 0 <= cursor < len(lines) else ""
        out.append(f"Editing line {cursor + 1}. Current: {cur}")
        out.append("Type the new text ('.' on its own cancels):")
    elif mode == "insert":
        out.append("Insert a new line before the cursor. Type text ('.' cancels):")
    else:
        out.append("Type text + Enter -> insert a line AFTER the cursor (keep going to write line by line).")
        out.append("up/down or j/k -> move   e -> edit line   i -> insert before   d -> delete")
        out.append("w -> save   r -> reload   q -> quit   ? -> help")
    if status:
        out.append("> " + status)
    return "\n".join(out)


EDITOR_HELP = (
    "Nano-style editor:\n"
    "  (type text) + Enter   insert a new line AFTER the cursor\n"
    "  up / down  (or j / k) move the cursor between lines\n"
    "  e                  edit the current line (then type its replacement)\n"
    "  i                  insert a new line BEFORE the cursor\n"
    "  a                  append a blank line at the end (then edit it)\n"
    "  d                  delete the current line\n"
    "  w                  save to disk\n"
    "  wq                 save and quit\n"
    "  r                  reload from disk (discard unsaved changes)\n"
    "  q                  quit (warns if unsaved)\n"
    "  q!                 quit discarding changes\n"
    "  ?                  show this help\n"
    "Tip: open a file, then just type lines and press Enter to write it top-to-bottom."
)


def edit_routine(path, ctx):
    p = Path(path)
    if p.is_dir():
        return {"output": f"edit: {path}: Is a directory"}
    existed = p.exists()
    try:
        lines = _read_lines(p)
    except Exception as e:
        return {"output": f"edit: cannot read {p}: {e}"}
    if lines is None:
        return {"output": f"edit: {path}: cannot open (is it a directory?)"}
    dirty = False
    cursor = -1 if not lines else 0
    mode = "normal"
    status = "New file - will be created on first save." if not existed else f"Loaded {len(lines)} lines."
    while True:
        prompt = "edit> "
        if mode == "edit":
            prompt = f"L{cursor + 1}> "
        elif mode == "insert":
            prompt = "ins> "
        view = _render_editor(p, lines, dirty, cursor, mode, status)
        cmd = yield {"prompt": prompt, "clear": True, "output": view}
        raw = cmd if cmd is not None else ""

        # Two-step "edit this line" / "insert before" sub-prompts capture the
        # raw next line of text (so it can start with any character, even a
        # command name) - a lone '.' cancels.
        if mode == "edit":
            if raw.strip() == ".":
                status = "Edit cancelled."
            else:
                lines[cursor] = raw
                dirty = True
                status = f"Line {cursor + 1} updated."
            mode = "normal"
            continue
        if mode == "insert":
            if raw.strip() == ".":
                status = "Insert cancelled."
            else:
                lines.insert(cursor, raw)
                cursor += 1
                dirty = True
                status = f"Inserted line {cursor + 1}."
            mode = "normal"
            continue

        # Normal mode.
        c = raw.strip()
        if c == "":
            continue
        if c in ("?", "h", "help"):
            status = EDITOR_HELP
            continue
        if c in ("q", "quit"):
            if dirty:
                status = "Unsaved changes! 'w' to save, 'q!' to discard, 'r' to reload."
                continue
            break
        if c == "q!":
            break
        if c in ("w", "wq", "w!"):
            try:
                _write_lines(p, lines)
                dirty = False
                status = f"Saved {len(lines)} lines to {p}."
            except Exception as e:
                status = f"Save failed: {e}"
                if c == "wq":
                    continue
                else:
                    continue
            if c == "wq":
                break
            continue
        if c == "r":
            try:
                reloaded = _read_lines(p)
                lines = reloaded if reloaded is not None else []
                dirty = False
                cursor = -1 if not lines else 0
                status = "Reloaded from disk."
            except Exception as e:
                status = f"Reload failed: {e}"
            continue
        if c in ("up", "k"):
            if cursor > -1:
                cursor -= 1
            status = ""
            continue
        if c in ("down", "j"):
            if cursor < len(lines) - 1:
                cursor += 1
            status = ""
            continue
        if c in ("d", "dd"):
            if 0 <= cursor < len(lines):
                lines.pop(cursor)
                dirty = True
                if cursor >= len(lines):
                    cursor = len(lines) - 1
                status = "Deleted current line."
            else:
                status = "No line at the cursor."
            continue
        if c == "e":
            if 0 <= cursor < len(lines):
                mode = "edit"
                status = ""
            else:
                status = "No line at the cursor to edit."
            continue
        if c == "i":
            mode = "insert"
            status = ""
            continue
        if c == "a":
            lines.append("")
            cursor = len(lines) - 1
            mode = "edit"
            status = ""
            continue
        # Default: insert a new line after the cursor (the "write line by line"
        # flow - the cursor follows each new line so Enter keeps appending).
        insert_at = cursor + 1
        lines.insert(insert_at, raw)
        cursor = insert_at
        dirty = True
        status = f"Inserted line {cursor + 1}."
    return {"output": f"Editor closed: {p}."}


@commands.command("edit")
def cmd_edit(ctx, args):
    """Open a file in the built-in code editor (syntax highlighting, smooth on mobile).

    Usage:  edit <file>
    Launches the in-browser code editor (highlighting + save). Great on phones.
    """
    if not args:
        return "Usage: edit <file>   (opens the code editor)"
    target = _raw_arg(ctx) or args[0]
    try:
        sandbox.fs(ctx["sid"])
    except sandbox.SandboxError as e:
        return f"edit: {e}"
    path = _resolve_path(ctx, target)
    if path.is_dir():
        return f"edit: {_virt(ctx, path)}: Is a directory"
    return {"editor": _virt(ctx, path)}


# --- file manager -----------------------------------------------------------

DIR_TAG = "[D]"


def _fm_listing(cwd, shown=None):
    out = []
    out.append(f"File manager: {shown or cwd}")
    out.append("-" * 60)
    try:
        entries = sorted(
            os.listdir(cwd),
            key=lambda s: (not os.path.isdir(os.path.join(cwd, s)), s.lower()),
        )
    except Exception as e:
        out.append(f"Cannot list: {e}")
        return "\n".join(out)
    if not entries:
        out.append("(empty)")
    for name in entries:
        full = os.path.join(cwd, name)
        if os.path.isdir(full):
            out.append(f"{DIR_TAG} {name}/")
        else:
            try:
                sz = os.path.getsize(full)
            except Exception:
                sz = 0
            out.append(f"    {name}  ({sz} bytes)")
    out.append("-" * 60)
    out.append("Cmds: cd <d>  up  open <f>  cat <f>  rm <f>  mkdir <d>  "
               "touch <f>  mv <a> <b>  q=quit  ?=help")
    return "\n".join(out)


FM_HELP = (
    "File manager commands:\n"
    "  cd <dir>    change into a directory (relative or absolute)\n"
    "  up          go up one directory\n"
    "  open <file> edit a file (in-console editor)\n"
    "  cat <file>  print a file's contents\n"
    "  rm <file>   delete a file or empty directory\n"
    "  mkdir <dir> create a directory\n"
    "  touch <f>   create an empty file\n"
    "  mv <a> <b>  rename / move\n"
    "  q           quit (cd persists to your console)\n"
    "  ?           show this help"
)


def fm_routine(start, ctx):
    cwd = _resolve_path(ctx, start)
    if not cwd.is_dir():
        cwd = _resolve_path(ctx, ctx["tab"]["cwd"])
    status = ""
    pending = None
    while True:
        listing = _fm_listing(cwd, _virt(ctx, cwd))
        if pending is not None:
            listing += "\n" + pending
            pending = None
        if status:
            listing += "\n> " + status
        cmd = yield {"prompt": "fm> ", "clear": True, "output": listing}
        cmd = (cmd or "").strip()
        if cmd == "":
            status = ""
            continue
        if cmd in ("?", "h", "help"):
            status = FM_HELP
            continue
        if cmd in ("q", "quit", "exit"):
            ctx["tab"]["cwd"] = _virt(ctx, cwd)
            return {"output": f"File manager closed. cwd is now {_virt(ctx, cwd)}."}
        if cmd in ("up", ".."):
            cwd = _parent(ctx, cwd)
            status = ""
            continue
        if cmd == "ls":
            status = ""
            continue
        if cmd == "pwd":
            status = f"cwd: {_virt(ctx, cwd)}"
            continue
        if cmd.startswith("cd "):
            target = cmd[3:].strip()
            nc = _resolve_path(ctx, target)
            if nc.is_dir():
                cwd = nc
                ctx["tab"]["cwd"] = _virt(ctx, cwd)
                status = ""
            else:
                status = f"Not a directory: {target}"
            continue
        if cmd.startswith("open ") or cmd.startswith("edit "):
            name = cmd.split(" ", 1)[1].strip()
            path = _resolve_path(ctx, name)
            if path.is_dir():
                status = f"Not a file: {name}"
                continue
            # Hand off to the in-browser code editor (returns, ending the fm
            # session; closing the editor drops back to the console).
            return {"editor": str(path)}
        if cmd.startswith("cat "):
            name = cmd[4:].strip()
            path = _resolve_path(ctx, name)
            try:
                data = _read_lines(path)
                if data is None:
                    status = f"Cannot cat: {name} (directory?)"
                else:
                    pending = f"--- {name} ---\n" + "\n".join(data)
                    status = ""
            except Exception as e:
                status = f"cat failed: {e}"
            continue
        if cmd.startswith("mkdir "):
            name = cmd[6:].strip()
            try:
                os.makedirs(_resolve_path(ctx, name), exist_ok=True)
                status = f"Created {name}"
            except Exception as e:
                status = f"mkdir failed: {e}"
            continue
        if cmd.startswith("touch "):
            name = cmd[6:].strip()
            try:
                open(_resolve_path(ctx, name), "a").close()
                status = f"Touched {name}"
            except Exception as e:
                status = f"touch failed: {e}"
            continue
        if cmd.startswith("rm "):
            name = cmd[3:].strip()
            path = _resolve_path(ctx, name)
            try:
                if path.is_dir():
                    os.rmdir(path)
                else:
                    os.remove(path)
                status = f"Removed {name}"
            except Exception as e:
                status = f"rm failed: {e}"
            continue
        if cmd.startswith("mv "):
            parts = cmd[3:].split(None, 1)
            if len(parts) == 2:
                a = _resolve_path(ctx, parts[0])
                b = _resolve_path(ctx, parts[1])
                try:
                    os.rename(a, b)
                    status = f"Moved {parts[0]} -> {parts[1]}"
                except Exception as e:
                    status = f"mv failed: {e}"
            else:
                status = "Usage: mv <src> <dst>"
            continue
        status = f"Unknown command: {cmd}  (type ? for help)"


@commands.command("fm")
def cmd_fm(ctx, args):
    """Open the in-console file manager (navigate + edit files, mobile-friendly).

    Usage:  fm [path]
    Browse directories and open files in the Python editor without a PTY.
    """
    try:
        sandbox.fs(ctx["sid"])
    except sandbox.SandboxError as e:
        return f"fm: {e}"
    start = _raw_arg(ctx) or (args[0] if args else ctx["tab"]["cwd"])
    return fm_routine(start, ctx)
