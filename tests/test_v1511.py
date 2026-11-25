"""v1.5.11: VM snapshots, clones and templates, Active Ports, and the read-only database viewer."""
import json
import os
import re
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

import auth
import dbview
import exposure
import sandbox
import vmports
import vmsnap

ROOT = Path(__file__).resolve().parent.parent


def make_vm(sid, vm, distro="debian", marker="hello"):
    """A VM directory that looks installed (no real system inside)."""
    base = sandbox.vm_dir(sid, vm)
    (base / "rootfs" / "etc").mkdir(parents=True)
    (base / "rootfs" / "etc" / "marker").write_text(marker)
    (base / "rootfs" / "tmp").mkdir()
    os.chmod(base / "rootfs" / "tmp", 0o1777)
    os.symlink("/etc/marker", base / "rootfs" / "link")
    (base / "home").mkdir()
    (base / "home" / "notes.txt").write_text("my notes")
    (base / "home" / ".bash_history").write_text("secret command")
    (base / "home" / ".ssh").mkdir()
    (base / "home" / ".ssh" / "id_rsa").write_text("PRIVATE")
    (base / "distro.json").write_text(json.dumps({"id": distro, "release": "13", "family": "debian",
                                                  "label": "Debian", "installed_at": 1}))
    (base / "setup.json").write_text(json.dumps(["python"]))
    (base / ".ready").write_text("1")
    return base


class SnapBase(unittest.TestCase):
    def setUp(self):
        helpers.wipe_vms()
        import shutil
        shutil.rmtree(vmsnap.store_dir(), ignore_errors=True)
        self.sid = "alice"
        if auth.get_user(self.sid) is None:
            auth.create_user(self.sid, "password123", False)
        p = mock.patch.object(sandbox, "supported", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.base = make_vm(self.sid, "debian")


class Snapshots(SnapBase):
    def test_create_restore_roundtrip(self):
        ok, msg = vmsnap.create_snapshot(self.sid, "debian", "one", "before", wait=True)
        self.assertTrue(ok, msg)
        snaps = vmsnap.list_snapshots(self.sid, "debian")
        self.assertEqual([s["name"] for s in snaps], ["one"])
        self.assertEqual(snaps[0]["note"], "before")
        (self.base / "rootfs" / "etc" / "marker").write_text("CHANGED")
        (self.base / "home" / "new.txt").write_text("new")
        ok, msg = vmsnap.restore_snapshot(self.sid, "debian", "one", wait=True)
        self.assertTrue(ok, msg)
        self.assertEqual((self.base / "rootfs" / "etc" / "marker").read_text(), "hello")
        self.assertFalse((self.base / "home" / "new.txt").exists())
        self.assertEqual((self.base / "home" / "notes.txt").read_text(), "my notes")
        self.assertTrue(os.path.islink(self.base / "rootfs" / "link"))
        self.assertEqual(os.stat(self.base / "rootfs" / "tmp").st_mode & 0o7777, 0o1777)   # sticky bit kept
        self.assertTrue((self.base / ".ready").exists())
        self.assertEqual(sandbox.install_state(self.sid, "debian")["state"], "ready")        # unlocked again

    def test_snapshot_keeps_everything_including_secrets(self):
        vmsnap.create_snapshot(self.sid, "debian", "full", wait=True)
        (self.base / "home" / ".ssh" / "id_rsa").unlink()
        vmsnap.restore_snapshot(self.sid, "debian", "full", wait=True)
        self.assertEqual((self.base / "home" / ".ssh" / "id_rsa").read_text(), "PRIVATE")

    def test_limits_and_names(self):
        for i in range(vmsnap.MAX_SNAPSHOTS):
            self.assertTrue(vmsnap.create_snapshot(self.sid, "debian", f"s{i}", wait=True)[0])
        ok, msg = vmsnap.create_snapshot(self.sid, "debian", "extra", wait=True)
        self.assertFalse(ok)
        self.assertIn("most", msg)
        self.assertFalse(vmsnap.create_snapshot(self.sid, "debian", "s0", wait=True)[0])        # duplicate
        self.assertFalse(vmsnap.create_snapshot(self.sid, "debian", "Bad Name!", wait=True)[0])
        self.assertTrue(vmsnap.delete_snapshot(self.sid, "debian", "s0")[0])
        self.assertFalse(vmsnap.delete_snapshot(self.sid, "debian", "s0")[0])

    def test_vm_locked_while_working(self):
        sandbox._set_install(self.sid, "debian", "working", "snapshotting")
        self.assertIn("busy", sandbox.busy_reason(self.sid, "debian"))
        ok, msg = vmsnap.create_snapshot(self.sid, "debian", "x", wait=True)
        self.assertFalse(ok)
        sandbox._clear_install(self.sid, "debian")
        self.assertIsNone(sandbox.busy_reason(self.sid, "debian"))

    def test_restore_unknown_and_other_users_are_separate(self):
        self.assertFalse(vmsnap.restore_snapshot(self.sid, "debian", "nope", wait=True)[0])
        vmsnap.create_snapshot(self.sid, "debian", "mine", wait=True)
        self.assertEqual(vmsnap.list_snapshots("bob", "debian"), [])

    def test_failed_restore_leaves_vm_untouched(self):
        vmsnap.create_snapshot(self.sid, "debian", "s", wait=True)
        arch = vmsnap.snap_dir(self.sid, "debian") / "s.tar.gz"
        arch.write_bytes(b"not a tar file")
        (self.base / "home" / "keep.txt").write_text("keep")
        ok, _msg = vmsnap.restore_snapshot(self.sid, "debian", "s", wait=True)
        self.assertFalse(ok)
        self.assertEqual((self.base / "home" / "keep.txt").read_text(), "keep")
        self.assertFalse((self.base / ".restore-new").exists())
        self.assertEqual(sandbox.install_state(self.sid, "debian")["state"], "ready")


class Clones(SnapBase):
    def test_clone_is_independent(self):
        ok, msg = vmsnap.clone(self.sid, "debian", "dev", wait=True)
        self.assertTrue(ok, msg)
        self.assertEqual(sorted(sandbox.installed_vms(self.sid)), ["debian", "dev"])
        info = sandbox.distro_info(self.sid, "dev")
        self.assertEqual(info["id"], "debian")                       # the distro, not the clone's name
        self.assertEqual(info["cloned_from"], "debian")
        dev = sandbox.vm_dir(self.sid, "dev")
        (dev / "home" / "notes.txt").write_text("changed in the clone")
        self.assertEqual((self.base / "home" / "notes.txt").read_text(), "my notes")
        self.assertTrue(os.path.islink(dev / "rootfs" / "link"))
        self.assertEqual(sandbox.recorded_setups(self.sid, "dev"), ["python"])

    def test_clone_from_snapshot_even_after_removal(self):
        vmsnap.create_snapshot(self.sid, "debian", "base", wait=True)
        sandbox.reset(self.sid, "debian", keep_home=False)
        self.assertNotIn("debian", sandbox.installed_vms(self.sid))
        self.assertIn("debian", vmsnap.snapshot_vms(self.sid))
        ok, msg = vmsnap.clone(self.sid, "debian", "again", snapshot="base", wait=True)
        self.assertTrue(ok, msg)
        self.assertEqual((sandbox.vm_dir(self.sid, "again") / "rootfs/etc/marker").read_text(), "hello")

    def test_bad_names_refused(self):
        for bad in ("debian", "Ubuntu", "kali", "a b", "", "x" * 40, "archlinux"):
            self.assertFalse(vmsnap.clone(self.sid, "debian", bad, wait=True)[0], bad)
        self.assertTrue(vmsnap.clone(self.sid, "debian", "dev", wait=True)[0])
        self.assertFalse(vmsnap.clone(self.sid, "debian", "dev", wait=True)[0])       # exists now
        self.assertFalse(vmsnap.clone(self.sid, "nothere", "x1", wait=True)[0])

    def test_failed_clone_leaves_no_half_vm(self):
        with mock.patch.object(vmsnap, "_copy_tree", side_effect=vmsnap.SnapError("disk full")):
            ok, msg = vmsnap.clone(self.sid, "debian", "dev", wait=True)
        self.assertFalse(ok)
        self.assertIn("disk full", msg)
        self.assertFalse(sandbox.vm_dir(self.sid, "dev").exists())
        self.assertEqual(sandbox.install_state(self.sid, "debian")["state"], "ready")
        self.assertEqual(sandbox.install_state(self.sid, "dev")["state"], "absent")

    def test_rebuild_after_wipe_uses_the_base_distro(self):
        vmsnap.clone(self.sid, "debian", "dev", wait=True)
        (sandbox.vm_dir(self.sid, "dev") / ".ready").unlink()
        calls = []
        with mock.patch.object(sandbox, "_install_one", side_effect=lambda s, d, r, vm=None: calls.append((d.id, vm))), \
                mock.patch.object(sandbox, "installed", return_value=True):
            ok, _ = sandbox._rebuild_one(self.sid, "dev", "13", "dev")
        self.assertTrue(ok)
        self.assertEqual(calls, [("debian", "dev")])


class Templates(SnapBase):
    def test_save_use_and_secrets_stay_out(self):
        ok, msg = vmsnap.save_template(self.sid, "debian", "pydev", description="python box", wait=True)
        self.assertTrue(ok, msg)
        t = vmsnap.find_template(self.sid, "pydev")
        self.assertEqual(t["setups"], ["python"])
        ok, msg = vmsnap.use_template(self.sid, "pydev", "work", wait=True)
        self.assertTrue(ok, msg)
        work = sandbox.vm_dir(self.sid, "work")
        self.assertEqual((work / "home" / "notes.txt").read_text(), "my notes")
        self.assertFalse((work / "home" / ".bash_history").exists())
        self.assertFalse((work / "home" / ".ssh").exists())
        self.assertEqual(sandbox.recorded_setups(self.sid, "work"), ["python"])
        self.assertEqual(sandbox.distro_info(self.sid, "work")["cloned_from"], "template:pydev")

    def test_shared_templates_admin_only_and_visible_to_all(self):
        self.assertFalse(vmsnap.save_template(self.sid, "debian", "team", shared=True, wait=True)[0])
        with mock.patch.object(sandbox, "is_admin", return_value=True):
            self.assertTrue(vmsnap.save_template(self.sid, "debian", "team", shared=True, wait=True)[0])
        self.assertTrue(vmsnap.find_template("bob", "team")["shared"])
        self.assertFalse(vmsnap.delete_template("bob", "team", shared=True)[0])
        ok, msg = vmsnap.use_template("bob", "team", "mine", wait=True)
        self.assertTrue(ok, msg)

    def test_delete_and_account_cleanup(self):
        vmsnap.save_template(self.sid, "debian", "t1", wait=True)
        vmsnap.create_snapshot(self.sid, "debian", "s1", wait=True)
        self.assertTrue(vmsnap.delete_template(self.sid, "t1")[0])
        vmsnap.remove_user(self.sid)
        self.assertEqual(vmsnap.list_snapshots(self.sid, "debian"), [])
        self.assertEqual(vmsnap.list_templates(self.sid), [])

    def test_bake_validates_before_doing_anything(self):
        self.assertFalse(vmsnap.bake_template(self.sid, "b1", "nosuchdistro", [], wait=True)[0])
        self.assertFalse(vmsnap.bake_template(self.sid, "b1", "debian", ["nosuchprofile"], wait=True)[0])
        self.assertFalse(vmsnap.bake_template(self.sid, "b1", "alpine", ["kali"], wait=True)[0])

    def test_bake_builds_saves_and_removes_the_scratch_vm(self):
        def fake_install(sid, distro, release, vm=None):
            make_vm(sid, vm, distro.id)
        with mock.patch.object(sandbox, "_install_one", side_effect=fake_install), \
                mock.patch.object(vmsnap, "_need_free"):
            ok, msg = vmsnap.bake_template(self.sid, "baked", "debian", [], wait=True)
        self.assertTrue(ok, msg)
        self.assertIsNotNone(vmsnap.find_template(self.sid, "baked"))
        self.assertFalse(sandbox.vm_dir(self.sid, "bake-baked").exists())


class CommandText(SnapBase):
    def run_cmd(self, *args):
        import cmd_vm
        return cmd_vm.cmd_vm({"sid": self.sid, "cols": 80, "is_admin": False, "username": self.sid}, list(args))

    def test_snapshot_commands(self):
        out = self.run_cmd("snapshot", "create", "debian", "first", "--note", "hi")
        self.assertIn("background", out)
        import time
        for _ in range(100):
            if vmsnap.list_snapshots(self.sid, "debian"):
                break
            time.sleep(0.05)
        self.assertIn("first", self.run_cmd("snapshot", "list"))
        self.assertIn("--yes", self.run_cmd("snapshot", "restore", "debian", "first"))
        self.assertIn("unknown", self.run_cmd("snapshot", "frobnicate"))
        self.assertIn("usage", self.run_cmd("snapshot", "restore"))

    def test_clone_template_and_ports_usage(self):
        self.assertIn("usage", self.run_cmd("clone"))
        self.assertIn("vm template", self.run_cmd("template", "help"))
        self.assertIn("No templates", self.run_cmd("template", "list"))
        self.assertIn("nothing is listening", self.run_cmd("ports"))
        self.assertIn("usage", self.run_cmd("ports", "open"))


class PortsParsing(unittest.TestCase):
    TCP = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
           "   0: 0100007F:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 11111 1 0 100 0 0\n"
           "   1: 00000000:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 22222 1 0 100 0 0\n"
           "   2: 0100007F:1F41 0100007F:9999 01 00000000:00000000 00:00000000 00000000  1000        0 33333 1 0 100 0 0\n")
    TCP6 = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 00000000000000000000000001000000:1F90 00000000000000000000000000000000:0000 0A 0:0 0:0 0 1000 0 44444 1 0 100 0 0\n"
            "   1: 00000000000000000000000000000000:0BB8 00000000000000000000000000000000:0000 0A 0:0 0:0 0 1000 0 55555 1 0 100 0 0\n")

    def test_only_listening_rows(self):
        rows = vmports.parse_table(self.TCP, "tcp")
        self.assertEqual([(r["addr"], r["port"], r["inode"]) for r in rows],
                         [("127.0.0.1", 8000, "11111"), ("0.0.0.0", 80, "22222")])

    def test_ipv6(self):
        rows = vmports.parse_table(self.TCP6, "tcp6")
        self.assertEqual([(r["addr"], r["port"]) for r in rows], [("::1", 8080), ("::", 3000)])

    def test_proxyable_rules(self):
        self.assertTrue(vmports.is_loopback_or_any("0.0.0.0"))
        self.assertTrue(vmports.is_loopback_or_any("127.0.0.1"))
        self.assertTrue(vmports.is_loopback_or_any("::"))
        self.assertFalse(vmports.is_loopback_or_any("10.1.2.3"))


class PortsLive(SnapBase):
    """A real listening socket, attributed to a (fake) VM process."""

    def setUp(self):
        super().setUp()
        exposure.clear()
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.proc = [{"pid": 7, "host_pid": os.getpid(), "state": "S", "rss_kb": 1, "cpu_s": 0, "cmd": "python -m http.server"}]
        p = mock.patch("pidns.vm_procs", return_value=self.proc)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.srv.close)
        vmports.forget()

    def test_scan_finds_the_port_and_toggle_roundtrips(self):
        rows = vmports.scan(self.sid, "debian")
        row = next(r for r in rows if r["port"] == self.port)
        self.assertEqual(row["pid"], 7)
        self.assertTrue(row["proxyable"])
        self.assertIsNone(row["exposed"])
        ok, msg, name = vmports.toggle(self.sid, "debian", self.port, True)
        self.assertTrue(ok, msg)
        self.assertEqual(exposure.get(name)["port"], self.port)
        self.assertEqual(exposure.get(name)["owner"], self.sid)
        self.assertEqual(next(r for r in vmports.scan(self.sid, "debian") if r["port"] == self.port)["exposed"], name)
        ok, _m, _n = vmports.toggle(self.sid, "debian", self.port, True)           # idempotent
        self.assertTrue(ok)
        ok, msg, _n = vmports.toggle(self.sid, "debian", self.port, False)
        self.assertTrue(ok, msg)
        self.assertIsNone(exposure.get(name))
        self.assertFalse(vmports.toggle(self.sid, "debian", self.port, False)[0])

    def test_cannot_open_a_port_the_vm_does_not_listen_on(self):
        ok, msg, _ = vmports.toggle(self.sid, "debian", 59999, True)
        self.assertFalse(ok)
        self.assertIn("nothing", msg)
        self.assertFalse(vmports.toggle(self.sid, "debian", True, True)[0])
        self.assertFalse(vmports.toggle(self.sid, "debian", "8000", True)[0])

    def test_other_vm_process_set_sees_nothing(self):
        with mock.patch("pidns.vm_procs", return_value=[]):
            self.assertEqual(vmports.scan(self.sid, "debian"), [])

    def test_default_name_is_valid_and_avoids_other_owners(self):
        exposure.add("debian-8000", 8000, "someone-else")
        name = vmports.default_name(self.sid, "debian", 8000)
        self.assertNotEqual(name, "debian-8000")
        self.assertIsNone(exposure.validate_name(name))


def make_db(path, rows=250):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, bio BLOB, big TEXT)")
    c.executemany("INSERT INTO users(name, bio, big) VALUES (?,?,?)",
                  [(f"user{i}", b"\x00\x01" * 10, "x" * 1000 if i == 0 else "s") for i in range(rows)])
    c.execute("CREATE VIEW names AS SELECT name FROM users")
    c.execute("CREATE INDEX ix_name ON users(name)")
    c.commit()
    c.close()


class DbViewer(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="dbv-")
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.p = os.path.join(self.d, "app.db")
        make_db(self.p)

    def q(self, sql, **kw):
        return dbview.execute("query", self.p, dict(sql=sql, **kw))

    def test_info_lists_tables_views_and_counts(self):
        s, r = dbview.execute("info", self.p, {})
        self.assertEqual(s, 200)
        names = {t["name"]: t for t in r["tables"]}
        self.assertEqual(names["users"]["rows"], 250)
        self.assertEqual(names["names"]["type"], "view")
        self.assertEqual([c["name"] for c in names["users"]["columns"]], ["id", "name", "bio", "big"])
        self.assertTrue(names["users"]["columns"][0]["pk"])
        self.assertTrue(any(o["name"] == "ix_name" for o in r["other"]))

    def test_table_paging_sorting_search_and_cells(self):
        s, r = dbview.execute("table", self.p, {"table": "users", "limit": 100})
        self.assertEqual((s, len(r["rows"]), r["more"], r["total"]), (200, 100, True, 250))
        s, r = dbview.execute("table", self.p, {"table": "users", "offset": 200, "limit": 100})
        self.assertEqual((len(r["rows"]), r["more"]), (50, False))
        s, r = dbview.execute("table", self.p, {"table": "users", "order": "id", "desc": True, "limit": 1})
        self.assertEqual(r["rows"][0][0], 250)
        s, r = dbview.execute("table", self.p, {"table": "users", "q": "user24"})
        self.assertEqual(r["total"], 11)                                     # user24, user240..user249
        s, r = dbview.execute("table", self.p, {"table": "users", "limit": 1})
        row = r["rows"][0]
        self.assertEqual(row[2], {"blob": 20, "hex": "0001" * 10})
        self.assertEqual(row[3]["len"], 1000)
        self.assertLessEqual(len(row[3]["text"]), dbview.CELL_CHARS)

    def test_search_wildcards_are_literal(self):
        s, r = dbview.execute("table", self.p, {"table": "users", "q": "%"})
        self.assertEqual(r["total"], 0)
        s, r = dbview.execute("table", self.p, {"table": "users", "q": "_"})
        self.assertEqual(r["total"], 0)

    def test_unknown_table_and_sql_injection_in_names(self):
        self.assertEqual(dbview.execute("table", self.p, {"table": "nope"})[0], 404)
        self.assertEqual(dbview.execute("table", self.p, {"table": 'users"; DROP TABLE users; --'})[0], 404)
        s, r = dbview.execute("table", self.p, {"table": "users", "order": 'id"; DROP TABLE users; --', "limit": 1})
        self.assertEqual(s, 200)                                             # unknown sort column is ignored

    def test_select_works_and_everything_else_is_refused(self):
        self.assertEqual(self.q("SELECT count(*) FROM users")[1]["rows"], [[250]])
        self.assertEqual(self.q("WITH t(x) AS (SELECT 1) SELECT x FROM t")[0], 200)
        self.assertEqual(self.q("EXPLAIN QUERY PLAN SELECT * FROM users")[0], 200)
        self.assertEqual(self.q("PRAGMA table_info(users)")[0], 200)
        for sql in ("INSERT INTO users(name) VALUES('x')", "UPDATE users SET name='x'", "DELETE FROM users",
                    "DROP TABLE users", "CREATE TABLE z(a)", "ALTER TABLE users ADD COLUMN z", "ATTACH 'x.db' AS y",
                    "PRAGMA journal_mode=WAL", "PRAGMA user_version=5", "SELECT load_extension('x')", "VACUUM",
                    "BEGIN", "REINDEX", "SELECT writefile('/tmp/x', 'y')"):
            s, r = self.q(sql)
            self.assertEqual(s, 400, sql)
            self.assertIn("error", r, sql)
        self.assertEqual(self.q("SELECT 1; SELECT 2")[0], 400)
        self.assertEqual(self.q("")[0], 400)
        self.assertEqual(self.q("x" * (dbview.MAX_SQL + 1))[0], 400)

    def test_database_is_never_modified(self):
        before = Path(self.p).read_bytes()
        for sql in ("DELETE FROM users", "DROP TABLE users", "SELECT * FROM users"):
            self.q(sql)
        dbview.execute("table", self.p, {"table": "users"})
        self.assertEqual(Path(self.p).read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.d)), ["app.db"])             # no journal, no temp files

    def test_row_limit_and_truncation_flag(self):
        s, r = self.q("SELECT * FROM users", limit=10)
        self.assertEqual((len(r["rows"]), r["more"]), (10, True))

    def test_runaway_query_is_stopped(self):
        with mock.patch.object(dbview, "TIME_BUDGET", 0.3):
            s, r = self.q("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c")
        self.assertEqual(s, 408)
        self.assertIn("stopped", r["error"])

    def test_not_a_database(self):
        bad = os.path.join(self.d, "other.db")
        Path(bad).write_text("just text")
        self.assertEqual(dbview.execute("info", bad, {})[0], 415)
        empty = os.path.join(self.d, "empty.db")
        Path(empty).write_bytes(b"")
        self.assertEqual(dbview.execute("info", empty, {})[0], 415)
        self.assertEqual(dbview.execute("info", os.path.join(self.d, "missing.db"), {})[0], 404)

    def test_wal_database_opens(self):
        p = os.path.join(self.d, "wal.db")
        c = sqlite3.connect(p)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE t(a)")
        c.execute("INSERT INTO t VALUES (1)")
        c.commit()                                  # keep the connection open: -wal and -shm exist
        s, r = dbview.execute("table", p, {"table": "t"})
        c.close()
        self.assertEqual(s, 200)

    def test_worker_process_end_to_end(self):
        s, r = dbview.run("info", self.p)
        self.assertEqual(s, 200)
        s, r = dbview.run("query", self.p, {"sql": "DELETE FROM users"})
        self.assertEqual(s, 400)
        s, r = dbview.run("nonsense", self.p)
        self.assertEqual(s, 400)

    def test_extension_lists_match_the_browser_code(self):
        js = (ROOT / "static" / "console.js").read_text()
        m = re.search(r"const DB_EXTS = \[([^\]]*)\]", js)
        self.assertEqual(re.findall(r'"([^"]+)"', m.group(1)), list(dbview.DB_EXTS))


class DbApi(unittest.TestCase):
    def setUp(self):
        import app as rapp
        self.c = rapp.app.test_client()
        auth.create_user("dbuser", "password123", False)
        r = self.c.post("/login", data={"username": "dbuser", "password": "password123"}, follow_redirects=False)
        self.assertIn(r.status_code, (200, 302))

    def test_requires_login_and_db_extension(self):
        import app as rapp
        anon = rapp.app.test_client()
        self.assertEqual(anon.get("/api/db/info?path=/x.db").status_code, 401)
        self.assertEqual(anon.get("/api/vm/ports").status_code, 401)
        self.assertIn(self.c.get("/api/db/info?path=/etc/passwd").status_code, (409, 415))

    def test_ports_api_scoped_to_the_user(self):
        r = self.c.get("/api/vm/ports")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["systems"], [])
        r = self.c.get("/api/vm/ports?owner=someone-else")
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/vm/ports", json={"vm": "debian", "port": 8000, "on": True})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
