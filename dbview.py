"""Read-only SQLite browsing for the file manager's DB tab.

Open ``app.db`` in the file browser and a small database browser appears (static/db-viewer.js): the tables on
the left, their rows on the right, a *Structure* page with the schema, and a *Query* box for ``SELECT``\\ s.
It only ever **reads**.

Why a worker process
--------------------
A database file is untrusted input (somebody's app wrote it, or somebody uploaded it) and SQLite is a C library.
A damaged or hostile file could in theory crash the interpreter, and the console runs everyone's terminals in one
process. So the engine runs in a short-lived child (``python -I -S dbview.py --worker``) that

* sets hard limits on itself first (address space, CPU time, file size, open files),
* reads one JSON request from stdin and writes one JSON answer to stdout,
* is killed by the parent after a timeout, and whose crash costs one request, not the server.

Why it cannot write
-------------------
Four independent layers: the file is opened ``mode=ro`` (the OS-level read-only flag of SQLite), ``PRAGMA
query_only`` is on, an *authorizer* refuses everything except ``SELECT`` / read-only ``PRAGMA`` / safe functions
(no ``ATTACH``, no ``load_extension``, no ``writefile``), and the process has ``RLIMIT_FSIZE`` set so it cannot
create files of any size. A progress handler stops a query that runs longer than its budget.

This module is stdlib-only on purpose: the worker runs with ``-I -S`` and must not import the rest of Rconsole.
"""
import json
import math
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

DB_EXTS = ("db", "sqlite", "sqlite3", "db3", "s3db", "sl3", "sqlitedb")   # keep equal to DB_EXTS in static/console.js
MAGIC = b"SQLite format 3\x00"

PAGE_ROWS = 100               # default rows per page in the table browser
MAX_ROWS = 500                # most rows a table page returns
MAX_QUERY_ROWS = 1000         # most rows a query returns
MAX_SQL = 20000               # longest query text
CELL_CHARS = 400              # text cells are cut to this many characters (the viewer can open the full cell)
OUTPUT_BUDGET = 3_000_000     # bytes of cell data in one answer
COUNT_CAP = 100000            # row counts stop here ("100000+")
TIME_BUDGET = 8.0             # seconds one request may spend inside SQLite
WORKER_TIMEOUT = 20           # seconds before the parent kills the worker
MAX_PARALLEL = 2              # worker processes at once (tiny hardware)

# What the authorizer lets through (see https://sqlite.org/c3ref/c_alter_table.html)
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)
_PRAGMA = sqlite3.SQLITE_PRAGMA
_PRAGMA_NO_ARG = {"user_version", "application_id", "page_count", "page_size", "encoding", "freelist_count",
                  "journal_mode", "schema_version", "data_version", "database_list", "collation_list",
                  "compile_options", "function_list", "module_list", "pragma_list", "foreign_keys",
                  "integrity_check", "quick_check", "foreign_key_check", "table_list"}
_PRAGMA_WITH_ARG = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo", "foreign_key_list",
                    "integrity_check", "quick_check", "foreign_key_check", "table_list"}
_BAD_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "zipfile", "sqlar_compress"}


class DbError(Exception):
    """Something the user can act on; ``status`` is the HTTP status the API answers with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# =============================================================================== the worker's side
def _authorizer(action, arg1, arg2, _db, _src):
    if action in _ALLOWED_ACTIONS or action == _RECURSIVE:
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _BAD_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action == _PRAGMA:
        name = (arg1 or "").lower()
        if arg2 is None and name in _PRAGMA_NO_ARG:
            return sqlite3.SQLITE_OK
        if name in _PRAGMA_WITH_ARG:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _qi(name):
    """Quote an identifier for SQL."""
    return '"' + str(name).replace('"', '""') + '"'


def _check_file(path):
    try:
        st = os.stat(path)
    except OSError as e:
        raise DbError(f"cannot read the file: {e.strerror or e}", 404)
    if not stat.S_ISREG(st.st_mode):
        raise DbError("not a regular file", 400)
    if st.st_size == 0:
        raise DbError("the file is empty (no database in it yet)", 415)
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError as e:
        raise DbError(f"cannot read the file: {e.strerror or e}", 404)
    if head != MAGIC:
        raise DbError("this is not a SQLite database (it may be encrypted or another kind of .db file)", 415)
    return st.st_size


def _connect(path, deadline):
    """Open ``path`` read-only. Returns ``(connection, stale)``; ``stale`` when the write-ahead log was not read."""
    base = Path(path).resolve().as_uri()
    last, con, stale = None, None, False
    for params, is_stale in (("mode=ro", False), ("mode=ro&immutable=1", True)):
        try:
            con = sqlite3.connect(f"{base}?{params}", uri=True, timeout=1.0, isolation_level=None)
            con.text_factory = lambda b: b.decode("utf-8", "replace")
            con.execute("PRAGMA query_only = ON")
            con.execute("PRAGMA temp_store = MEMORY")
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()      # reads the header: errors surface now
            stale = is_stale
            break
        except sqlite3.Error as e:
            last = e
            if con is not None:
                con.close()
            con = None
    if con is None:
        raise DbError(f"cannot open the database: {last}", 400)
    for name, val in (("SQLITE_LIMIT_LENGTH", 5_000_000), ("SQLITE_LIMIT_SQL_LENGTH", MAX_SQL + 100),
                      ("SQLITE_LIMIT_EXPR_DEPTH", 200), ("SQLITE_LIMIT_COMPOUND_SELECT", 20)):
        lim = getattr(sqlite3, name, None)
        if lim is not None and hasattr(con, "setlimit"):
            try:
                con.setlimit(lim, val)
            except (sqlite3.Error, ValueError, OverflowError):
                pass
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline[0] else 0, 20000)
    con.set_authorizer(_authorizer)
    return con, stale


def _cell(v, budget):
    """A JSON-safe cell. Plain values stay plain; odd ones become small tagged objects the viewer understands."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if abs(v) < 2 ** 53 else {"big": str(v)}
    if isinstance(v, float):
        return v if math.isfinite(v) else {"big": str(v)}
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        budget[0] -= 64
        return {"blob": len(b), "hex": b[:24].hex()}
    s = str(v)
    if len(s) > CELL_CHARS:
        budget[0] -= CELL_CHARS
        return {"text": s[:CELL_CHARS], "len": len(s)}
    budget[0] -= len(s) + 4
    return s


def _rows(cur, limit, budget):
    out, more = [], False
    while True:
        row = cur.fetchone()
        if row is None:
            break
        if len(out) >= limit or budget[0] <= 0:
            more = True
            break
        out.append([_cell(v, budget) for v in row])
    return out, more


def _capped_count(con, from_sql, params=()):
    try:
        return con.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {from_sql} LIMIT {COUNT_CAP + 1})", params).fetchone()[0]
    except sqlite3.Error:
        return None


def _objects(con):
    return con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()


def op_info(con, stale, size, args, deadline):
    pragmas = {}
    for key in ("page_size", "page_count", "encoding", "user_version", "application_id", "freelist_count",
                "journal_mode"):
        try:
            pragmas[key] = con.execute(f"PRAGMA {key}").fetchone()[0]
        except sqlite3.Error:
            pragmas[key] = None
    tables, others = [], []
    count_until = time.monotonic() + 3.0
    for typ, name, tbl, sql in _objects(con):
        if typ in ("table", "view"):
            internal = name.startswith("sqlite_")
            try:
                cols = [{"name": r[1], "type": r[2] or "", "pk": bool(r[5]), "notnull": bool(r[3]),
                         "default": r[4]} for r in con.execute(f"PRAGMA table_info({_qi(name)})")]
            except sqlite3.Error:
                cols = []
            n = _capped_count(con, _qi(name)) if (time.monotonic() < count_until and not internal) else None
            tables.append({"name": name, "type": typ, "columns": cols, "rows": n, "internal": internal,
                           "sql": (sql or "")[:4000], "virtual": bool(sql and sql.upper().lstrip().startswith("CREATE VIRTUAL"))})
        else:
            others.append({"type": typ, "name": name, "table": tbl, "sql": (sql or "")[:4000]})
    return {"size": size, "stale": stale, "pragmas": pragmas, "tables": tables, "other": others,
            "sqlite_version": sqlite3.sqlite_version, "count_cap": COUNT_CAP}


def op_table(con, stale, size, args, deadline):
    name = str(args.get("table") or "")
    known = {n: t for t, n, _tb, _s in _objects(con) if t in ("table", "view")}
    if name not in known:
        raise DbError(f"no table called '{name}'", 404)
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({_qi(name)})")]
    offset = max(0, min(int(args.get("offset") or 0), 10_000_000))
    limit = max(1, min(int(args.get("limit") or PAGE_ROWS), MAX_ROWS))
    where, params = "", []
    q = str(args.get("q") or "")[:100]
    if q and cols:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = " WHERE " + " OR ".join(f"CAST({_qi(c)} AS TEXT) LIKE ? ESCAPE '\\'" for c in cols[:40])
        params = [like] * len(cols[:40])
    order = ""
    sort = args.get("order")
    if sort in cols:
        order = f" ORDER BY {_qi(sort)} {'DESC' if args.get('desc') else 'ASC'}"
    sql = f"SELECT * FROM {_qi(name)}{where}{order} LIMIT {limit + 1} OFFSET {offset}"
    cur = con.execute(sql, params)
    headers = [d[0] for d in cur.description] if cur.description else cols
    rows, more = _rows(cur, limit, [OUTPUT_BUDGET])
    total = _capped_count(con, _qi(name) + where, params) if offset == 0 or args.get("count") else None
    return {"columns": headers, "rows": rows, "more": more, "offset": offset, "limit": limit,
            "total": total, "capped": total is not None and total > COUNT_CAP,
            "stale": stale}


def op_query(con, stale, size, args, deadline):
    sql = str(args.get("sql") or "").strip()
    if not sql:
        raise DbError("type a query first")
    if len(sql) > MAX_SQL:
        raise DbError(f"the query is too long (limit {MAX_SQL} characters)")
    limit = max(1, min(int(args.get("limit") or 200), MAX_QUERY_ROWS))
    t0 = time.monotonic()
    cur = con.execute(sql)
    if cur.description is None:
        return {"columns": [], "rows": [], "more": False, "ms": 0, "note": "the statement returned no rows",
                "stale": stale}
    headers = [d[0] for d in cur.description]
    rows, more = _rows(cur, limit, [OUTPUT_BUDGET])
    return {"columns": headers, "rows": rows, "more": more, "limit": limit,
            "ms": int((time.monotonic() - t0) * 1000), "stale": stale}


OPS = {"info": op_info, "table": op_table, "query": op_query}


def _friendly(e):
    msg = str(e)
    low = msg.lower()
    if "not authorized" in low or "authoriz" in low:
        return DbError("read-only: only SELECT, WITH, EXPLAIN and read-only PRAGMA statements are allowed here")
    if "interrupted" in low:
        return DbError(f"the query ran longer than {int(TIME_BUDGET)} s and was stopped; narrow it with WHERE or LIMIT",
                       408)
    if "one statement at a time" in low:
        return DbError("run one statement at a time")
    if "file is not a database" in low or "malformed" in low or "disk image is malformed" in low:
        return DbError(f"the database file is damaged or encrypted: {msg}", 422)
    if "string or blob too big" in low:
        return DbError("a value in that result is too large to show")
    return DbError(msg)


def execute(op, path, args):
    """Run one operation in-process and return ``(status, payload)``. (The worker calls this; tests may too.)"""
    fn = OPS.get(op)
    if fn is None:
        return 400, {"error": f"unknown operation '{op}'"}
    deadline = [time.monotonic() + TIME_BUDGET]
    con = None
    try:
        size = _check_file(path)
        con, stale = _connect(path, deadline)
        return 200, fn(con, stale, size, args or {}, deadline)
    except DbError as e:
        return e.status, {"error": str(e)}
    except sqlite3.Error as e:
        err = _friendly(e)
        return err.status, {"error": str(err)}
    except (ValueError, TypeError) as e:
        return 400, {"error": f"bad request: {e}"}
    except MemoryError:
        return 413, {"error": "that needs more memory than a database view may use; narrow it with WHERE or LIMIT"}
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


def _limit_self():
    try:
        import resource
    except ImportError:                                   # not Linux: the parent's timeout still applies
        return
    for name, val in (("RLIMIT_AS", 768 * 1024 * 1024), ("RLIMIT_CPU", 25),
                      ("RLIMIT_FSIZE", 8 * 1024 * 1024), ("RLIMIT_NOFILE", 64), ("RLIMIT_CORE", 0)):
        lim = getattr(resource, name, None)
        if lim is None:
            continue
        try:
            resource.setrlimit(lim, (val, val))
        except (ValueError, OSError):
            pass


def _worker_main():
    _limit_self()
    try:
        req = json.loads(sys.stdin.read())
        status, payload = execute(req.get("op"), req.get("path"), req.get("args"))
    except BaseException as e:                              # never die silently
        status, payload = 500, {"error": f"{type(e).__name__}: {e}"}
    sys.stdout.write(json.dumps({"status": status, "data": payload}))
    sys.stdout.flush()


# ================================================================================ the parent's side
_slots = threading.BoundedSemaphore(MAX_PARALLEL)


def run(op, path, args=None):
    """Run ``op`` on the database file ``path`` in a limited child process: ``(http_status, payload)``."""
    if not _slots.acquire(timeout=8):
        return 429, {"error": "the database viewer is busy; try again in a moment"}
    try:
        req = json.dumps({"op": op, "path": str(path), "args": args or {}}).encode()
        try:
            p = subprocess.run([sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--worker"], input=req,
                               capture_output=True, timeout=WORKER_TIMEOUT, cwd=os.path.dirname(os.path.abspath(path)) or "/",
                               env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})
        except subprocess.TimeoutExpired:
            return 408, {"error": f"the database took longer than {WORKER_TIMEOUT} s to answer and was stopped"}
        except OSError as e:
            return 500, {"error": f"could not start the database reader: {e}"}
        try:
            out = json.loads(p.stdout.decode("utf-8", "replace"))
            return int(out["status"]), out["data"]
        except (ValueError, KeyError, TypeError):
            return 500, {"error": "the database reader stopped unexpectedly (a very large result or a damaged file); "
                                  f"exit code {p.returncode}"}
    finally:
        _slots.release()


if __name__ == "__main__" and "--worker" in sys.argv:
    _worker_main()
