"""Background job registry for rconsole.

A "job" is a real host command launched with a trailing ``&``. It runs detached
from the line console: its stdout/stderr are streamed into a rolling buffer by a
reader thread, and it can be inspected with ``jobs`` / ``jobout`` and terminated
with ``kill %n``. Jobs are tracked per user so ``%n`` is stable within a session.
"""
import os
import re
import subprocess
import threading
import time

import sandbox

JOBS = {}                 # (sid, jid) -> Job
JOBS_LOCK = threading.Lock()
_NEXT = {}                # sid -> int (last issued job id)

BUFFER_MAX = 256 * 1024   # bytes of output kept per job
MAX_JOBS_PER_USER = 50    # finished jobs beyond this are pruned on start


class Job:
    def __init__(self, sid, jid, cmd, cwd):
        self.sid = sid
        self.jid = jid
        self.cmd = cmd
        self.cwd = cwd
        self.buffer = ""
        self.alive = True
        self.start = time.time()
        self.end = None
        self.pid = None
        self._lock = threading.Lock()
        popen_kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=False,
        )
        # On POSIX, give the job its own session/process group so we can kill
        # the whole tree (shell + children) with killpg without affecting the
        # rconsole server's own process group.
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        try:
            # sandbox.spec authorizes the launch and picks host vs VM.
            sp = sandbox.spec(self.sid, cmd, cwd)
            self.proc = subprocess.Popen(
                sp.argv, cwd=sp.cwd, env=sp.env, **popen_kwargs)
            self.pid = self.proc.pid
        except sandbox.NeedVmChoice:
            raise                      # interp turns this into a system picker
        except Exception as e:
            self.alive = False
            self.end = time.time()
            self._append("job failed to start: %s\n" % e)
            self.proc = None
        if self.proc is not None:
            threading.Thread(target=self._reader, daemon=True).start()

    def _append(self, s):
        with self._lock:
            self.buffer += s
            if len(self.buffer) > BUFFER_MAX:
                # Trim with hysteresis so we don't re-copy on every append.
                self.buffer = self.buffer[-(BUFFER_MAX * 3 // 4):]

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self._append(line)
        except Exception:
            pass
        try:
            self.proc.wait()
        except Exception:
            pass
        self.alive = False
        self.end = time.time()
        with self._lock:
            self.buffer += "\r\n[job %d finished, exit %s]\r\n" % (
                self.jid, self.proc.returncode)

    def tail(self, n=50):
        with self._lock:
            text = self.buffer
        lines = text.split("\n")
        if n and len(lines) > n:
            lines = lines[-n:]
        return "\n".join(lines)

    def read_from(self, offset):
        """Return (text_since_offset, new_offset) for incremental tailing."""
        with self._lock:
            text = self.buffer
        if offset is None or offset < 0:
            offset = 0
        if offset > len(text):
            # Buffer rotated past our offset; send from the current start.
            offset = 0
        return text[offset:], len(text)

    def kill(self):
        if self.proc is None:
            self.alive = False
            return
        pid = self.proc.pid
        # Kill the whole process tree, not just the launcher (cmd.exe / sh),
        # so orphaned children (e.g. the real `python` holding a port) die too.
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except Exception:
                pass
        else:
            try:
                import signal
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                pass
        # Ensure the launcher itself is stopped as a fallback.
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass

        def _force():
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass

        if self.proc.poll() is None:
            threading.Thread(target=_force, daemon=True).start()
        # Non-blocking: the reader thread reaps the process and appends the
        # final "[job finished, exit ...]" line on its own. (The old blocking
        # wait(timeout=3) stalled the request thread, e.g. on taskkill lag.)
        self.alive = False
        self.end = time.time()
        self._append("\r\n[job %d killed]\r\n" % self.jid)


def _next_jid(sid):
    n = _NEXT.get(sid, 0) + 1
    _NEXT[sid] = n
    return n


def start_job(sid, cmd, cwd):
    """Launch ``cmd`` as a background job. Returns the new job id (int)."""
    with JOBS_LOCK:
        jid = _next_jid(sid)
        job = Job(sid, jid, cmd, cwd)
        JOBS[(sid, jid)] = job
    _prune_finished(sid)
    return jid, job


def _prune_finished(sid):
    """Drop oldest finished jobs so the registry can't grow without bound."""
    with JOBS_LOCK:
        mine = sorted(k for (s, k) in JOBS if s == sid)
        if len(mine) <= MAX_JOBS_PER_USER:
            return
        overflow = len(mine) - MAX_JOBS_PER_USER
        dropped = 0
        for k in mine:
            if dropped >= overflow:
                break
            if not JOBS[(sid, k)].alive:
                JOBS.pop((sid, k), None)
                dropped += 1


def list_jobs(sid):
    with JOBS_LOCK:
        return [
            (jid, JOBS[(sid, jid)])
            for jid in sorted(k for (s, k) in JOBS if s == sid)
        ]


def get_job(sid, jid):
    return JOBS.get((sid, jid))


def kill_job(sid, jid):
    job = JOBS.get((sid, jid))
    if not job:
        return False
    job.kill()
    return True


def kill_all_jobs(sid):
    with JOBS_LOCK:
        keys = [(s, k) for (s, k) in JOBS if s == sid]
    for s, k in keys:
        try:
            JOBS[(s, k)].kill()
        except Exception:
            pass


def parse_job_ref(arg):
    """Accept '%1', '1', or '%n'. Return int jid or None."""
    a = (arg or "").lstrip("%").strip()
    if re.fullmatch(r"\d+", a):
        return int(a)
    return None
