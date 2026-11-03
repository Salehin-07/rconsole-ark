"""Persistent terminal sessions keyed by (sid, tab_id).

A session owns a real PTY process and a rolling output buffer. Client
websockets attach/detach without killing the process; only an explicit kill
(e.g. the user closing the tab) terminates it. This lets a command such as a
long-running Discord bot survive page refreshes and tab switches, and lets the
user re-attach to see where they left off.
"""
import json
import os
import signal
import sys
import threading
import time

import config
import sandbox

BUFFER_MAX = 200 * 1024  # characters kept per session
REPLAY_MAX = 32 * 1024   # characters replayed to a (re)attaching client

_PTYEXEC = config.HERE / "_ptyexec.py"
SESSIONS = {}              # (sid, tab_id) -> TermSession
SESSIONS_LOCK = threading.Lock()


def get(sid, tab_id):
    return SESSIONS.get((sid, tab_id))


def kill_all_for(sid):
    with SESSIONS_LOCK:
        keys = [k for k in SESSIONS if k[0] == sid]
        sessions = [SESSIONS.pop(k, None) for k in keys]
    for s in sessions:
        try:
            if s is not None:
                s.kill()
        except Exception:
            pass


def kill_vm(sid, vm):
    """Kill this user's terminals that run inside ``vm``. Returns how many."""
    with SESSIONS_LOCK:
        keys = [k for k, s in SESSIONS.items() if k[0] == sid and getattr(s, "vm", "") == vm]
        sessions = [SESSIONS.pop(k, None) for k in keys]
    for s in sessions:
        try:
            if s is not None:
                s.kill()
        except Exception:
            pass
    return len(keys)


class TermSession:
    def __init__(self, sid, tab_id, cmd, cwd, cols=80, rows=24):
        self.sid = sid
        self.tab_id = tab_id
        self.cmd = cmd
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.buffer = ""
        self.clients = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.alive = True
        self.exited = False
        self.vm = ""                # the VM this terminal runs in ("" = the host)
        self.last_cwd = None        # host shells: the directory the shell was last seen in
        self._spawn()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ------------------------------------------------------------------ spawn
    def _spawn(self):
        if sys.platform == "win32":
            self._spawn_win()
        else:
            self._spawn_posix()

    def _spawn_win(self):
        from winpty import PTY
        # Authorize first (the VM needs Linux, so this is always the host).
        sp = sandbox.spec(self.sid, self.cmd, self.cwd, tty=True)
        shell_exe, flag = sp.argv[0], "/c" if sp.argv[0].lower().endswith("cmd.exe") else "-c"
        _, cmd = sandbox.parse_target(self.cmd)
        cmdline = f'{flag} "{cmd.replace(chr(34), "\\" + chr(34))}"' if cmd else None
        env = sp.env
        env_str = "\0".join(f"{k}={v}" for k, v in env.items()) + "\0"
        pty = PTY(self.cols, self.rows)
        pty.spawn(shell_exe, cmdline=cmdline, cwd=self.cwd, env=env_str)
        self.pty = pty
        self.master_fd = None
        self.proc = None

    def _spawn_posix(self):
        import fcntl
        import pty as pty_mod
        import select
        import struct
        import subprocess
        import termios
        # sandbox.spec authorizes the launch (host vs VM) before any process
        # exists, so a forged websocket command can't bypass the admin gate.
        sp = sandbox.spec(self.sid, self.cmd, self.cwd, tty=True)
        self.vm = sp.vm if sp.mode == sandbox.VM else ""
        argv = sp.argv
        master_fd, slave_fd = pty_mod.openpty()
        try:
            cols = max(1, int(self.cols)); rows = max(1, int(self.rows))
        except Exception:
            cols, rows = 80, 24
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass
        # _ptyexec makes the slave our controlling terminal (job control, Ctrl+C).
        launcher = [sys.executable, "-S", str(_PTYEXEC)]
        proc = subprocess.Popen(
            launcher + argv, cwd=sp.cwd, env=sp.env,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        self.master_fd = master_fd
        self.proc = proc
        self._posix = (fcntl, select, signal, termios, struct)

    # ----------------------------------------------------------------- reader
    def _reader(self):
        if sys.platform == "win32":
            self._reader_win()
        else:
            self._reader_posix()

    def _reader_win(self):
        while not self.stop.is_set() and self.pty.isalive():
            try:
                data = self.pty.read()
            except Exception:
                break
            if data:
                self._append(data)
                self._broadcast(data)
            else:
                time.sleep(0.02)

    def _reader_posix(self):
        fcntl, select, signal, termios, struct = self._posix
        while not self.stop.is_set():
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not r:
                if self.proc.poll() is not None:
                    try:
                        tail = os.read(self.master_fd, 65536)
                    except OSError:
                        tail = b""
                    if tail:
                        s = tail.decode("utf-8", "replace")
                        self._append(s)
                        self._broadcast(s)
                    break
                continue
            try:
                data = os.read(self.master_fd, 65536)
            except OSError:
                break
            if not data:
                break
            s = data.decode("utf-8", "replace")
            self._append(s)
            self._broadcast(s)

    # ------------------------------------------------------------ cwd tracking
    def _sample_cwd(self):
        """Remember a host shell's current directory (read from /proc while it runs: once the shell
        has exited it is too late). VM shells run under proot, whose real directory is not a guest
        path, so those keep the directory the terminal was opened in."""
        if self.vm or sys.platform == "win32" or self.proc is None:
            return
        try:
            d = os.readlink("/proc/%d/cwd" % self.proc.pid)
        except OSError:
            return
        if not (d and os.path.isdir(d)):
            return
        # /proc shows the resolved path: keep the user's spelling when the start dir is a symlink
        # (/home/rconsole -> the persistent workspace).
        try:
            base = os.path.realpath(self.cwd)
            if base != self.cwd and (d == base or d.startswith(base + os.sep)):
                d = self.cwd + d[len(base):]
        except (OSError, TypeError):
            pass
        self.last_cwd = d

    # --------------------------------------------------------------- watchdog
    def _watchdog(self):
        rc = None
        if sys.platform == "win32":
            while not self.stop.is_set() and self.pty.isalive():
                time.sleep(0.2)
        else:
            try:
                import subprocess
                while True:
                    self._sample_cwd()
                    try:
                        rc = self.proc.wait(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except Exception:
                pass
        self.stop.set()
        self.alive = False
        self.exited = True
        try:
            self._append(
                "\r\n*** terminal session ended%s ***\r\n"
                % ((" (exit code %s)" % rc) if rc is not None else "")
            )
        except Exception:
            pass
        # Surface the tail of output server-side so the failure isn't invisible:
        # check the host/container logs (e.g. `render logs`) to see what the
        # command actually printed before it died or hung.
        try:
            tail = self.buffer[-2000:]
            sys.stderr.write(
                "[termsess] session (%s/%s) ended rc=%s tail=%r\n"
                % (self.sid, self.tab_id, rc, tail)
            )
        except Exception:
            pass
        # Keep the tab (and where the shell ended up) so the line console carries on in the same
        # directory instead of falling back to the default one.
        try:
            import state as _state
            _state.end_terminal(self.sid, self.tab_id, self.last_cwd)
        except Exception:
            pass
        # Tell the client the process has exited so it can automatically return
        # to the line console instead of leaving the user stuck in the terminal
        # view (the "ws") with a dead session.
        try:
            # Prefix control messages with SOH (\x01) so raw terminal output can
            # never be misinterpreted as a control frame (e.g. a program printing
            # the string '{"t":"exit"}' would otherwise kick the user to console).
            self._broadcast("\x01" + json.dumps({"t": "exit", "cwd": self.last_cwd}))
        except Exception:
            pass
        # A finished session can't be usefully re-attached, so drop it from memory.
        try:
            with SESSIONS_LOCK:
                SESSIONS.pop((self.sid, self.tab_id), None)
        except Exception:
            pass
        try:
            if sys.platform == "win32":
                self.pty.cancel_io()
            else:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------- lifecycle
    def _append(self, s):
        with self.lock:
            self.buffer += s
            if len(self.buffer) > BUFFER_MAX:
                # Trim with hysteresis so we don't re-copy on every append.
                self.buffer = self.buffer[-(BUFFER_MAX * 3 // 4):]

    def _broadcast(self, data):
        with self.lock:
            for ws in list(self.clients):
                try:
                    ws.send(data)
                except Exception:
                    try:
                        self.clients.remove(ws)
                    except Exception:
                        pass

    def attach(self, ws):
        with self.lock:
            if self.buffer:
                try:
                    # Replay only the tail: the full buffer (up to BUFFER_MAX)
                    # is pointlessly heavy on every tab switch / refresh.
                    ws.send(self.buffer[-REPLAY_MAX:])
                except Exception:
                    pass
            if ws not in self.clients:
                self.clients.append(ws)

    def detach(self, ws):
        with self.lock:
            if ws in self.clients:
                self.clients.remove(ws)
        # Intentionally does NOT kill the process: a disconnect (refresh / tab
        # switch) must preserve the session so the user can return to it.

    def write(self, data):
        try:
            if sys.platform == "win32":
                self.pty.write(data)
            else:
                os.write(self.master_fd, data.encode("utf-8", "replace"))
        except Exception:
            pass

    def resize(self, cols, rows):
        try:
            cols = int(cols)
            rows = int(rows)
        except (TypeError, ValueError):
            return
        # Ignore degenerate sizes (e.g. a transient 0x0 while the mobile soft
        # keyboard animates open/closed) — setting the PTY to 0x0 can crash the
        # child process such as nano and drop the user back to the console.
        if cols <= 0 or rows <= 0:
            return
        self.cols, self.rows = cols, rows
        try:
            if sys.platform == "win32":
                self.pty.set_size(int(cols), int(rows))
            else:
                fcntl, select, signal, termios, struct = self._posix
                winsize = struct.pack("HHHH", int(rows), int(cols), 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def kill(self):
        """Terminate the whole process group and tear down the session."""
        self.stop.set()
        try:
            if sys.platform == "win32":
                try:
                    self.pty.write("\x03")
                except Exception:
                    pass
                try:
                    self.pty.write("exit\r\n")
                except Exception:
                    pass
                try:
                    self.pty.cancel_io()
                except Exception:
                    pass
            else:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
        except Exception:
            pass
        self.alive = False
        self.exited = True
