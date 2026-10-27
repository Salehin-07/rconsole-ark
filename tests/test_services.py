"""The service supervisor: autostart, crash restarts with backoff, commands, lifecycle."""
import threading
import time
import unittest

import helpers  # noqa: F401

import app as rapp
import auth
import config
import exposure
import jobs
import lifecycle
import services
import state


def wait_dead(name, timeout=5):
    """Wait until the supervised process has really exited."""
    s = services.get(name)
    rt = services._rt.get(name) or {}
    job = jobs.get_job(s["owner"], rt.get("jid")) if rt.get("jid") is not None else None
    end = time.time() + timeout
    while job is not None and job.alive and time.time() < end:
        time.sleep(0.05)
    return job


class Base(unittest.TestCase):
    def setUp(self):
        services.FILE.unlink(missing_ok=True)
        services._cache.update(sig=None, data={})
        services._rt.clear()
        exposure.FILE.unlink(missing_ok=True)
        exposure._cache.update(sig=None, data={})
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()
        auth.set_password("ark", "adminpass123")
        auth.create_user("alice", "longenough1")
        rapp.app.config["TESTING"] = True
        rapp.USER_THROTTLE._fails.clear()
        rapp.IP_THROTTLE._fails.clear()
        self._backoff = services.BACKOFF_MIN

    def tearDown(self):
        lifecycle.reset()
        services.clear()
        for sid in ("ark", "alice"):
            jobs.kill_all_jobs(sid)

    def admin(self):
        c = rapp.app.test_client()
        c.post("/login", data={"username": "ark", "password": "adminpass123"})
        return c

    def user(self):
        c = rapp.app.test_client()
        c.post("/login", data={"username": "alice", "password": "longenough1"})
        return c

    def run_cmd(self, c, cmd):
        return c.post("/api/run", json={"cmd": cmd, "tab": "main"}).get_json()["output"]


class SupervisorTests(Base):
    def test_starts_a_service_and_keeps_it_running(self):
        self.assertEqual(services.add("w", "ark", "sleep 30", "host"), (True, "created"))
        services.tick(now=1000)
        i = services.info("w")
        self.assertEqual(i["state"], "running")
        self.assertTrue(jobs.get_job("ark", i["jid"]).alive)
        jid = i["jid"]
        for t in (1001, 1002, 1030):
            services.tick(now=t)
        self.assertEqual(services.info("w")["jid"], jid)               # not restarted while healthy

    def test_crash_restarts_with_growing_backoff(self):
        services.add("crashy", "ark", "exit 3", "host")
        t = 1000.0
        services.tick(now=t)
        delays = []
        for _ in range(8):
            wait_dead("crashy")
            services.tick(now=t + 1)                                   # notices the death
            rt = services._rt["crashy"]
            delays.append(round(rt["next"] - (t + 1)))
            self.assertEqual(rt["last_exit"], 3)
            services.tick(now=t + 1.5)
            self.assertFalse(services._rt["crashy"]["running"])        # still waiting
            t = rt["next"]
            services.tick(now=t)                                       # backoff elapsed: restarted
            self.assertTrue(services._rt["crashy"]["running"])
        self.assertEqual(delays, [2, 4, 8, 16, 32, 60, 60, 60])        # capped at a minute
        self.assertEqual(services._rt["crashy"]["restarts"], 8)

    def test_a_run_that_lasted_resets_the_backoff(self):
        services.add("flaky", "ark", "exit 1", "host")
        services.tick(now=1000)
        wait_dead("flaky")
        services.tick(now=1001)
        self.assertEqual(services._rt["flaky"]["backoff"], 4)
        services.tick(now=1003)
        wait_dead("flaky")
        services._rt["flaky"]["started"] = 1003 - 3600                 # pretend it ran for an hour
        services.tick(now=1004)
        rt = services._rt["flaky"]
        self.assertEqual(round(rt["next"] - 1004), services.BACKOFF_MIN)

    def test_logs_and_environment(self):
        services.add("echoer", "ark", "echo value=$FOO; sleep 5", "host", env={"FOO": "bar baz"})
        services.tick(now=1000)
        time.sleep(0.5)
        self.assertIn("value=bar baz", services.logs("echoer"))

    def test_stop_start_restart_and_disabled_services(self):
        services.add("s", "ark", "sleep 30", "host", enabled=False)
        services.tick(now=1000)
        self.assertEqual(services.info("s")["state"], "stopped")
        self.assertIsNone(services._rt.get("s", {}).get("jid"))
        self.assertEqual(services.set_enabled("s", "ark", False, True), (True, "started"))
        services.tick(now=1001)
        jid = services.info("s")["jid"]
        self.assertEqual(services.info("s")["state"], "running")
        self.assertEqual(services.set_enabled("s", "ark", False, False), (True, "stopped"))
        time.sleep(0.3)
        self.assertFalse(jobs.get_job("ark", jid).alive)
        self.assertEqual(services.info("s")["state"], "stopped")
        services.tick(now=1100)
        self.assertFalse(services._rt["s"]["running"])                 # stays stopped

    def test_unavailable_vm_retries_instead_of_crashing_the_supervisor(self):
        services.add("vmbot", "alice", "python bot.py", "vm:debian")
        services.tick(now=1000)
        wait_dead("vmbot")
        services.tick(now=1001)                                        # must not raise
        i = services.info("vmbot")
        self.assertIn(i["state"], ("backoff", "restarting"))
        self.assertGreaterEqual(i["restarts"], 1)

    def test_non_admin_can_never_run_on_the_host(self):
        services.add("sneaky", "alice", "id", "host")                  # even if the registry says so
        services.tick(now=1000)
        self.assertFalse(services._rt["sneaky"]["running"])
        self.assertIn("admin-only", services.info("sneaky")["last_error"])

    def test_deleted_users_services_are_skipped(self):
        services.add("orphan", "ghost", "sleep 30", "host")
        services.tick(now=1000)
        self.assertFalse((services._rt.get("orphan") or {}).get("running"))

    def test_supervisor_thread_can_be_started_and_stopped(self):
        saved = config.SUPERVISOR
        config.SUPERVISOR = True
        try:
            self.assertTrue(services.start_background())
            self.assertTrue(services.start_background())
            services._stop.set()
            services._thread.join(3)
            self.assertFalse(services._thread.is_alive())
        finally:
            config.SUPERVISOR = saved
            services._stop.clear()


class RegistryTests(Base):
    def test_names_limits_and_ownership(self):
        for bad in ("", "-x", "a b", "x" * 40, "A/B"):
            self.assertFalse(services.add(bad, "ark", "true", "host")[0], bad)
        self.assertFalse(services.add("ok", "ark", "  ", "host")[0])
        self.assertTrue(services.add("Mixed-Case", "ark", "true", "host")[0])
        self.assertIsNotNone(services.get("mixed-case"))
        ok, msg = services.add("mixed-case", "alice", "true", "host")
        self.assertFalse(ok)
        self.assertIn("used by ark", msg)
        for i in range(services.MAX_PER_USER - 1):
            self.assertTrue(services.add(f"s{i}", "ark", "true", "host", enabled=False)[0])
        ok, msg = services.add("one-too-many", "ark", "true", "host")
        self.assertFalse(ok)
        self.assertIn("remove one", msg)
        self.assertFalse(services.remove("s0", "alice")[0])
        self.assertTrue(services.remove("s0", "alice", is_admin=True)[0])

    def test_persistence_and_private_file(self):
        services.add("keep", "ark", "sleep 1", "vm:debian", cwd="/root/x", env={"A": "1"},
                     expose=(8123, "keepsite"), enabled=False)
        services._cache.update(sig=None, data={})
        s = services.get("keep")
        self.assertEqual((s["target"], s["cwd"], s["env"], s["expose"], s["enabled"]),
                         ("vm:debian", "/root/x", {"A": "1"}, [8123, "keepsite"], False))
        self.assertEqual(services.FILE.stat().st_mode & 0o777, 0o600)

    def test_expose_option_creates_and_removal_cleans_up(self):
        self.assertTrue(services.add("web", "alice", "sleep 5", "vm:debian", expose=(8123, "myweb"))[0])
        self.assertEqual(exposure.get("myweb")["port"], 8123)
        self.assertEqual(exposure.get("myweb")["owner"], "alice")
        self.assertTrue(services.remove("web", "alice")[0])
        self.assertIsNone(exposure.get("myweb"))
        ok, msg = services.add("web2", "alice", "sleep 5", "vm:debian", expose=(80, "bad"))
        self.assertFalse(ok)
        self.assertIsNone(services.get("web2"))                         # nothing half-created

    def test_owner_lifecycle(self):
        services.add("a1", "alice", "sleep 30", "host", expose=(8124, "a1site"))
        services.add("k1", "ark", "true", "host", enabled=False)
        services.rename_owner("alice", "alicia")
        self.assertEqual(services.get("a1")["owner"], "alicia")
        self.assertEqual(services.remove_owner("alicia"), 1)
        self.assertIsNone(services.get("a1"))
        self.assertIsNone(exposure.get("a1site"))
        self.assertIsNotNone(services.get("k1"))
        services.clear()
        self.assertEqual(services.list_all(), [])


class CommandTests(Base):
    def test_add_list_logs_stop_start_restart_rm(self):
        c = self.admin()
        out = self.run_cmd(c, "service add bot --host --env TOKEN=abc echo token=$TOKEN; sleep 30")
        self.assertIn("Service 'bot' created (host); starting now", out)
        self.assertEqual(services.get("bot")["cmd"], "echo token=$TOKEN; sleep 30")      # raw text kept
        self.assertEqual(services.get("bot")["env"], {"TOKEN": "abc"})
        time.sleep(0.5)
        self.assertIn("● bot — running", self.run_cmd(c, "service list"))
        self.assertIn("token=abc", self.run_cmd(c, "service logs bot"))
        self.assertIn("bot: stopped", self.run_cmd(c, "service stop bot"))
        self.assertIn("○ bot — stopped", self.run_cmd(c, "service"))
        self.assertIn("bot: started", self.run_cmd(c, "service start bot"))
        self.assertIn("bot: started", self.run_cmd(c, "service restart bot"))
        self.assertIn("bot: removed", self.run_cmd(c, "service rm bot"))
        self.assertIn("No services yet", self.run_cmd(c, "service list"))

    def test_command_text_keeps_quoting_and_double_dash(self):
        c = self.admin()
        self.run_cmd(c, "service add q1 --host --no-autostart sh -c 'echo \"a b\"; sleep 9'")
        self.assertEqual(services.get("q1")["cmd"], "sh -c 'echo \"a b\"; sleep 9'")
        self.assertFalse(services.get("q1")["enabled"])
        self.run_cmd(c, "service add q2 --host --no-autostart -- --weird-program --flag")
        self.assertEqual(services.get("q2")["cmd"], "--weird-program --flag")
        self.run_cmd(c, 'service add q3 --host --no-autostart --cwd "/tmp" python app.py')
        self.assertEqual((services.get("q3")["cwd"], services.get("q3")["cmd"]), ("/tmp", "python app.py"))

    def test_errors_are_explained(self):
        c = self.admin()
        self.assertIn("unknown option --wat", self.run_cmd(c, "service add x --wat python a.py"))
        self.assertIn("no command given", self.run_cmd(c, "service add x --host"))
        self.assertIn("--expose needs PORT:NAME", self.run_cmd(c, "service add x --host --expose 8000 sleep 1"))
        self.assertIn("--env needs KEY=VALUE", self.run_cmd(c, "service add x --host --env nope sleep 1"))
        self.assertIn("--host or --vm", self.run_cmd(c, "service add x --host --vm debian sleep 1"))
        self.assertIn("unknown system 'windows'", self.run_cmd(c, "service add x --vm windows sleep 1"))
        self.assertIn("no service called 'nothere'", self.run_cmd(c, "service logs nothere"))
        self.assertIn("unknown subcommand", self.run_cmd(c, "service frobnicate"))
        self.assertIn("service add <name>", self.run_cmd(c, "service help"))

    def test_normal_users_stay_in_their_vms(self):
        u = self.user()
        self.assertIn("--host is for admins", self.run_cmd(u, "service add x --host sleep 1"))
        self.assertIn("no system yet", self.run_cmd(u, "service add x sleep 1"))
        self.run_cmd(u, "service add vmsvc --vm debian --no-autostart python bot.py")   # a system you will have
        self.assertEqual(services.get("vmsvc")["target"], "vm:debian")

    def test_users_only_touch_their_own_services(self):
        a, adm = self.user(), self.admin()
        self.run_cmd(a, "service add mine --vm debian --no-autostart sleep 9")
        self.run_cmd(adm, "service add rootsvc --host --no-autostart sleep 9")
        self.assertIn("mine", self.run_cmd(a, "service list"))
        self.assertNotIn("rootsvc", self.run_cmd(a, "service list"))
        self.assertIn("belongs to ark", self.run_cmd(a, "service stop rootsvc"))
        self.assertIn("belongs to ark", self.run_cmd(a, "service rm rootsvc"))
        self.assertIn("belongs to ark", self.run_cmd(a, "service logs rootsvc"))
        self.assertIn("mine", self.run_cmd(adm, "service list"))                     # admins see all
        self.assertIn("mine: removed", self.run_cmd(adm, "service rm mine"))

    def test_expose_option_via_command(self):
        u = self.user()
        out = self.run_cmd(u, "service add site --vm debian --no-autostart --expose 8125:mysite python -m http.server 8125")
        self.assertIn("Exposed at /mysite/", out)
        self.assertEqual(exposure.get("mysite")["owner"], "alice")
        self.assertIn("→ /mysite/", self.run_cmd(u, "service list"))
        self.run_cmd(u, "service rm site")
        self.assertIsNone(exposure.get("mysite"))

    def test_start_stop_all(self):
        c = self.admin()
        self.run_cmd(c, "service add a --host --no-autostart sleep 30")
        self.run_cmd(c, "service add b --host --no-autostart sleep 30")
        out = self.run_cmd(c, "service start all")
        self.assertIn("a: started", out)
        self.assertIn("b: started", out)
        self.assertIn("a: stopped", self.run_cmd(c, "service stop all"))
        self.assertIn("You have no services", self.run_cmd(self.user(), "service stop all"))


class DefaultsTests(Base):
    def test_user_cap_keeps_this_a_small_team_tool(self):
        saved = config.MAX_USERS
        try:
            config.MAX_USERS = len(auth.list_users()) + 1
            self.assertTrue(auth.create_user("third", "longenough1")[0])
            ok, msg = auth.create_user("fourth", "longenough1")
            self.assertFalse(ok)
            self.assertIn("limited to", msg)
            self.assertIn("small team", msg)
        finally:
            config.MAX_USERS = saved

    def test_host_commands_start_in_the_persistent_workspace(self):
        d = state.default_cwd()
        self.assertEqual(d, str(config.WORKSPACE_DIR))
        self.assertTrue(config.WORKSPACE_DIR.is_dir())
        self.assertEqual(self.run_cmd(self.admin(), "pwd").strip(), d)

    def test_app_is_installable_on_a_phone(self):
        c = rapp.app.test_client()
        m = c.get("/static/manifest.webmanifest").get_json()
        self.assertEqual((m["display"], m["start_url"]), ("standalone", "/console"))
        self.assertTrue(m["icons"])
        self.assertEqual(c.get("/static/icon.svg").status_code, 200)
        self.assertIn('rel="manifest"', c.get("/login").get_data(as_text=True))
        self.assertIn('rel="manifest"', self.admin().get("/console").get_data(as_text=True))


class RebootTests(Base):
    def test_requires_confirmation_and_explains(self):
        c = self.admin()
        out = self.run_cmd(c, "sudo reboot")
        self.assertIn("--yes", out)
        self.assertIn("files and VMs stay", out)
        self.assertIn("EXITS the process", self.run_cmd(c, "sudo reboot --hard"))
        self.assertIn("not in the sudoers", self.run_cmd(self.user(), "sudo reboot --yes"))

    def test_confirmed_reboot_schedules_the_restart_after_replying(self):
        import cmd_reboot
        calls = []
        real = threading.Timer
        cmd_reboot.threading.Timer = lambda delay, fn, args=(): calls.append((delay, fn, args)) or type(
            "T", (), {"start": lambda self: None})()
        try:
            out = self.run_cmd(self.admin(), "sudo reboot --yes")
        finally:
            cmd_reboot.threading.Timer = real
        self.assertIn("Restarting", out)
        self.assertEqual(calls, [(cmd_reboot.DELAY, cmd_reboot.perform, (False,))])

    def test_perform_backs_up_stops_everything_then_reloads(self):
        import backup
        import cmd_reboot
        services.add("svc", "ark", "sleep 60", "host")
        services.tick(now=1000)
        jid = services.info("svc")["jid"]
        self.assertTrue(jobs.get_job("ark", jid).alive)
        order = []
        lifecycle.reset()
        saved = (backup.final, cmd_reboot._reload, cmd_reboot._hard_exit)
        backup.final = lambda timeout=25: order.append(("backup", timeout))
        cmd_reboot._reload = lambda: order.append("reload")
        cmd_reboot._hard_exit = lambda: order.append("exit")
        try:
            cmd_reboot.perform(hard=False)
            time.sleep(0.3)
            self.assertFalse(jobs.get_job("ark", jid).alive)             # nothing left to orphan
            self.assertEqual(order, [("backup", lifecycle.FINAL_BACKUP_TIMEOUT), "reload"])
            order.clear()
            lifecycle.reset()
            cmd_reboot.perform(hard=True)
            self.assertEqual(order, [("backup", lifecycle.FINAL_BACKUP_TIMEOUT), "exit"])
        finally:
            backup.final, cmd_reboot._reload, cmd_reboot._hard_exit = saved
            lifecycle.reset()

    def test_gunicorn_detection(self):
        import cmd_reboot
        self.assertFalse(cmd_reboot._is_gunicorn(1_999_999))
        self.assertFalse(cmd_reboot._is_gunicorn(__import__("os").getpid()))


if __name__ == "__main__":
    unittest.main()
