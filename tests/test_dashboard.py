"""Admin dashboard: access control, data sections, admin actions, audit trail, logout button."""
import json
import re
import time
import unittest

import helpers  # noqa: F401

import app as rapp
import auth
import dashboard
import exposure
import jobs
import services
import termsess
from test_services import Base


class DashBase(Base):
    def setUp(self):
        super().setUp()
        dashboard._audit.clear()
        dashboard._audit_loaded = True
        dashboard._seen.clear()
        dashboard._default_pw.update(t=0.0, bad=False)

    def act(self, c, action, **kw):
        r = c.post("/api/dashboard/action", json={"action": action, **kw})
        return r.status_code, r.get_json()

    def data(self, c, section):
        r = c.get(f"/api/dashboard/data?section={section}")
        self.assertEqual(r.status_code, 200, section)
        return r.get_json()


class AccessTests(DashBase):
    def test_anonymous_is_sent_to_login(self):
        c = rapp.app.test_client()
        self.assertIn(c.get("/dashboard").status_code, (302, 404))
        self.assertIn(c.get("/api/dashboard/data").status_code, (302, 401, 403, 404))
        self.assertIn(c.post("/api/dashboard/action", json={"action": "backup.now"}).status_code, (302, 401, 403, 404))

    def test_normal_user_gets_a_limited_dashboard(self):
        c = self.user()
        self.assertEqual(c.get("/dashboard").status_code, 200)
        self.assertEqual(c.get("/api/dashboard/data?section=overview").status_code, 200)
        for sec in ("settings", "users", "activity", "health", "repo"):          # admin-only sections
            self.assertEqual(c.get(f"/api/dashboard/data?section={sec}").status_code, 403, sec)
        self.assertEqual(c.get("/api/dashboard/history").status_code, 403)
        self.assertEqual(self.act(c, "user.create", username="x", password="longenough1")[0], 403)
        self.assertIsNone(auth.get_user("x"))

    def test_normal_user_only_sees_own_things(self):
        d = self.data(self.user(), "services")
        self.assertTrue(all(s["owner"] == "alice" for s in d["services"]))
        d = self.data(self.user(), "overview")
        self.assertEqual(d["account"]["name"], "alice")
        self.assertNotIn("users_online", d)
        # logs: not someone else's job
        self.assertEqual(self.user().get("/api/dashboard/log?kind=job&owner=bob&id=1").status_code, 404)

    def test_everyone_can_change_their_own_password(self):
        c = self.user()
        pw = lambda **kw: c.post("/api/dashboard/password", json=kw)  # noqa: E731
        self.assertEqual(pw(current="wrong", password="brandnewpass1", confirm="brandnewpass1").status_code, 400)
        self.assertEqual(pw(current="longenough1", password="brandnewpass1", confirm="different").status_code, 400)
        self.assertEqual(pw(current="longenough1", password="x", confirm="x").status_code, 400)       # too short
        r = pw(current="longenough1", password="brandnewpass1", confirm="brandnewpass1")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(auth.verify("alice", "brandnewpass1"))
        self.assertFalse(auth.verify("alice", "longenough1"))
        self.assertEqual(rapp.app.test_client().post("/api/dashboard/password", json={}).status_code, 401)

    def test_admin_can_change_own_password_too(self):
        c = self.admin()
        r = c.post("/api/dashboard/password", json={"current": "adminpass123", "password": "adminnewpass1"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.data(c, "account")["account"]["admin"], True)

    def test_admin_gets_the_page(self):
        r = self.admin().get("/dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Dashboard", r.data)

    def test_link_is_rendered_for_everyone(self):
        self.assertIn(b'href="/dashboard"', self.admin().get("/console").data)
        self.assertIn(b'href="/dashboard"', self.user().get("/console").data)

    def test_demoted_admin_keeps_a_limited_dashboard(self):
        auth.create_user("bob", "longenough1", True)
        c = rapp.app.test_client()
        c.post("/login", data={"username": "bob", "password": "longenough1"})
        self.assertEqual(c.get("/api/dashboard/data?section=settings").status_code, 200)
        auth.set_admin("bob", False)
        self.assertEqual(c.get("/dashboard").status_code, 200)
        self.assertEqual(c.get("/api/dashboard/data?section=settings").status_code, 403)

    def test_cross_site_post_is_refused(self):
        c = self.admin()
        r = c.post("/api/dashboard/action", json={"action": "storage.rescan"},
                   headers={"Origin": "https://evil.example"})
        self.assertIn(r.status_code, (400, 403))


class LogoutButtonTests(DashBase):
    def test_logout_endpoint_ends_the_session(self):
        c = self.user()
        self.assertEqual(c.get("/console").status_code, 200)
        r = c.post("/api/logout")
        self.assertEqual(r.status_code, 200)
        self.assertIn("login", r.get_json()["redirect"])
        self.assertEqual(c.get("/console").status_code, 302)

    def test_logout_keeps_terminals_like_the_command(self):
        c = self.user()
        self.run_cmd(c, "echo hi")
        before = set(termsess.SESSIONS)
        c.post("/api/logout")
        self.assertEqual(set(termsess.SESSIONS), before)

    def test_logout_requires_login_and_is_audited(self):
        self.assertEqual(rapp.app.test_client().post("/api/logout").status_code, 401)
        c = self.user()
        c.post("/api/logout")
        self.assertIn("logout", [e["action"] for e in dashboard.recent_audit()])

    def test_logout_command_still_works(self):
        c = self.user()
        out = c.post("/api/run", json={"cmd": "logout", "tab": "main"}).get_json()
        self.assertIn("redirect", out)

    def test_buttons_are_in_the_page(self):
        html = self.user().get("/console").data.decode()
        self.assertIn('id="logoutbtn"', html)
        self.assertIn('id="sm-logout-slot"', html)


class SectionTests(DashBase):
    def test_every_section_returns_json(self):
        c = self.admin()
        for s in dashboard.SECTIONS:
            self.assertIsInstance(self.data(c, s), dict)

    def test_unknown_section_is_a_400(self):
        self.assertEqual(self.admin().get("/api/dashboard/data?section=nope").status_code, 400)

    def test_overview_has_current_numbers_and_counts(self):
        d = self.data(self.admin(), "overview")
        self.assertIn("cpu", d["current"])
        self.assertGreaterEqual(d["counts"]["users"], 2)
        self.assertIsInstance(d["alerts"], list)

    def test_history_grows_with_samples(self):
        dashboard.sample()
        dashboard.sample()
        rows = self.admin().get("/api/dashboard/history?minutes=5").get_json()["samples"]
        self.assertGreaterEqual(len(rows), 2)

    def test_users_section_lists_roles(self):
        d = self.data(self.admin(), "users")
        by = {u["name"]: u for u in d["users"]}
        self.assertTrue(by["ark"]["admin"])
        self.assertFalse(by["alice"]["admin"])
        self.assertTrue(by["ark"]["you"])

    def test_online_status_comes_from_requests(self):
        a = self.admin()
        self.user().get("/console")
        by = {u["name"]: u for u in self.data(a, "users")["users"]}
        self.assertTrue(by["alice"]["online"])

    def test_processes_show_services_and_jobs(self):
        services.add("w", "ark", "sleep 30", "host")
        services.tick(now=1000)
        d = self.data(self.admin(), "processes")
        self.assertEqual([s["name"] for s in d["services"]], ["w"])
        self.assertTrue(any(j["alive"] for j in d["jobs"]))

    def test_apps_show_exposures(self):
        exposure.add("site", 8123, "ark")
        d = self.data(self.admin(), "apps")
        self.assertEqual(d["exposures"][0]["name"], "site")
        self.assertFalse(d["exposures"][0]["listening"])

    def test_alert_for_default_admin_password(self):
        auth.USERS_FILE.unlink(missing_ok=True)
        auth._cache.update(sig=None, users={})
        auth.seed_defaults()
        self.assertTrue(any("factory password" in a["text"] for a in dashboard._alerts(dashboard.latest())))

    def test_log_tail_for_job_and_service(self):
        services.add("w", "ark", "echo hello-from-w; sleep 5", "host")
        services.tick(now=1000)
        time.sleep(0.5)
        c = self.admin()
        r = c.get("/api/dashboard/log?kind=service&id=w")
        self.assertEqual(r.status_code, 200)
        self.assertIn("hello-from-w", r.get_json()["text"])
        self.assertEqual(c.get("/api/dashboard/log?kind=service&id=missing").status_code, 404)


class ActionTests(DashBase):
    def test_create_promote_demote_delete(self):
        c = self.admin()
        code, d = self.act(c, "user.create", username="carol", password="longenough1", admin=False)
        self.assertEqual(code, 200, d)
        self.assertFalse(auth.is_admin("carol"))
        self.assertEqual(self.act(c, "user.promote", username="carol")[0], 200)
        self.assertTrue(auth.is_admin("carol"))
        self.assertEqual(self.act(c, "user.demote", username="carol")[0], 200)
        self.assertFalse(auth.is_admin("carol"))
        self.assertEqual(self.act(c, "user.delete", username="carol")[0], 200)
        self.assertIsNone(auth.get_user("carol"))

    def test_create_admin_directly(self):
        self.act(self.admin(), "user.create", username="dave", password="longenough1", admin=True)
        self.assertTrue(auth.is_admin("dave"))

    def test_bad_input_is_rejected_with_a_reason(self):
        c = self.admin()
        code, d = self.act(c, "user.create", username="e", password="x")
        self.assertEqual(code, 400)
        self.assertFalse(d["ok"])
        self.assertEqual(self.act(c, "user.create", username="alice", password="longenough1")[0], 400)
        self.assertEqual(self.act(c, "user.promote", username="nobody")[0], 400)
        self.assertEqual(self.act(c, "nonsense")[0], 400)

    def test_cannot_change_or_delete_yourself(self):
        c = self.admin()
        self.assertEqual(self.act(c, "user.demote", username="ark")[0], 400)
        self.assertEqual(self.act(c, "user.delete", username="ark")[0], 400)
        self.assertEqual(self.act(c, "user.stop", username="ark")[0], 400)
        self.assertTrue(auth.is_admin("ark"))

    def test_last_admin_is_protected_even_from_another_admin(self):
        auth.create_user("bob", "longenough1", True)
        bob = rapp.app.test_client()
        bob.post("/login", data={"username": "bob", "password": "longenough1"})
        self.assertEqual(self.act(bob, "user.demote", username="ark")[0], 200)
        # bob is now the only admin; ark (a normal user now) is locked out of the dashboard entirely
        self.assertEqual(self.act(self.admin(), "user.demote", username="bob")[0], 403)
        self.assertTrue(auth.is_admin("bob"))
        self.assertEqual(self.act(bob, "user.demote", username="bob")[0], 400)

    def test_reset_password(self):
        self.act(self.admin(), "user.password", username="alice", password="brandnew-pass1")
        self.assertTrue(auth.verify("alice", "brandnew-pass1"))
        self.assertEqual(self.act(self.admin(), "user.password", username="alice", password="x")[0], 400)

    def test_stop_everything_of_a_user(self):
        auth.create_user("bob", "longenough1", True)
        jobs.start_job("bob", "sleep 30", "/")
        self.assertTrue([1 for o, _i, j in dashboard._all_jobs() if o == "bob" and j.alive])
        code, _ = self.act(self.admin(), "user.stop", username="bob")
        self.assertEqual(code, 200)
        time.sleep(0.5)
        self.assertFalse([1 for o, _i, j in dashboard._all_jobs() if o == "bob" and j.alive])

    def test_service_controls(self):
        services.add("w", "alice", "sleep 30", "host")
        services.tick(now=1000)
        c = self.admin()
        self.assertEqual(self.act(c, "service.restart", name="w")[0], 200)
        self.assertEqual(self.act(c, "service.stop", name="w")[0], 200)
        self.assertEqual(services.info("w")["state"], "stopped")
        self.assertEqual(self.act(c, "service.start", name="w")[0], 200)
        self.assertEqual(self.act(c, "service.remove", name="w")[0], 200)
        self.assertIsNone(services.get("w"))
        self.assertEqual(self.act(c, "service.stop", name="w")[0], 400)

    def test_kill_job(self):
        services.add("w", "ark", "sleep 30", "host")
        services.tick(now=1000)
        jid = services.info("w")["jid"]
        self.assertEqual(self.act(self.admin(), "job.kill", owner="ark", jid=jid)[0], 200)
        self.assertEqual(self.act(self.admin(), "job.kill", owner="ark", jid=9999)[0], 400)

    def test_remove_exposure(self):
        exposure.add("site", 8123, "alice")
        self.assertEqual(self.act(self.admin(), "expose.remove", name="site")[0], 200)
        self.assertEqual(self.act(self.admin(), "expose.remove", name="site")[0], 400)

    def test_backup_now_needs_configuration(self):
        code, d = self.act(self.admin(), "backup.now")
        self.assertEqual(code, 400)
        self.assertIn("not configured", d["message"])

    def test_actions_are_audited_including_failures(self):
        c = self.admin()
        self.act(c, "user.create", username="fred", password="longenough1")
        self.act(c, "user.promote", username="nobody")
        ev = self.data(c, "activity")["events"]
        self.assertEqual([(e["action"], e["ok"]) for e in ev[:2]], [("user.promote", False), ("user.create", True)])
        self.assertEqual(ev[0]["actor"], "ark")

    def test_sudo_commands_are_audited_too(self):
        c = self.admin()
        self.run_cmd(c, "sudo auser alice")
        self.assertIn(("user.promote", "alice"), [(e["action"], e["target"]) for e in dashboard.recent_audit()])

    def test_logins_are_audited(self):
        rapp.app.test_client().post("/login", data={"username": "alice", "password": "wrong-password"})
        self.user()
        acts = [(e["action"], e["ok"]) for e in dashboard.recent_audit()]
        self.assertIn(("login.failed", False), acts)
        self.assertIn(("login", True), acts)


class AuditFileTests(DashBase):
    def test_audit_is_persisted_and_reloaded(self):
        dashboard.audit("ark", "test.event", "t", "detail")
        lines = dashboard._audit_path().read_text().splitlines()
        self.assertEqual(json.loads(lines[-1])["action"], "test.event")
        dashboard._audit.clear()
        dashboard._audit_loaded = False
        self.assertEqual(dashboard.recent_audit()[0]["action"], "test.event")

    def test_audit_never_raises(self):
        dashboard.audit(None, None, object(), object())


class SamplerTests(unittest.TestCase):
    def test_sample_has_the_expected_shape(self):
        s = dashboard.sample()
        for k in ("t", "cpu", "mem_pct", "disk_pct", "rx", "tx", "load", "procs", "terms", "jobs", "svc"):
            self.assertIn(k, s)
        self.assertTrue(s["disk_pct"] is None or 0 <= s["disk_pct"] <= 100)

    def test_history_is_bounded(self):
        for _ in range(dashboard.HISTORY_MAX + 5):
            dashboard._hist.append({"t": int(time.time()), "cpu": 1})
        self.assertLessEqual(len(dashboard._hist), dashboard.HISTORY_MAX)

    def test_host_processes_lists_this_process(self):
        import os
        d = dashboard.host_processes(limit=500)
        if d["supported"]:
            self.assertIn(os.getpid(), [r["pid"] for r in d["rows"]])

    def test_sampler_thread_starts_once(self):
        try:
            self.assertTrue(dashboard.start_background() or True)
            self.assertFalse(dashboard.start_background())
        finally:
            dashboard.stop()


class FrontendFilesTests(unittest.TestCase):
    def test_dashboard_js_never_uses_innerhtml(self):
        src = open("static/dashboard.js", encoding="utf-8").read()
        self.assertIsNone(re.search(r"innerHTML|insertAdjacentHTML|document\.write", src))


if __name__ == "__main__":
    unittest.main()
