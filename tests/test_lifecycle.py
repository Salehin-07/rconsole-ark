"""Shutdown ordering: nothing may be restarted or orphaned while the server goes down."""
import runpy
import signal
import threading
import time
import unittest

import helpers  # noqa: F401

import auth
import backup
import jobs
import lifecycle
import services
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        lifecycle.reset()
        services.FILE.unlink(missing_ok=True)
        services._cache.update(sig=None, data={})
        services._rt.clear()
        auth._cache.update(sig=None, users={})
        auth.USERS_FILE.unlink(missing_ok=True)
        auth.seed_defaults()
        self.calls = []
        self._final = backup.final
        backup.final = lambda timeout=25: self.calls.append(("backup", timeout))

    def tearDown(self):
        backup.final = self._final
        lifecycle.reset()
        services.clear()
        jobs.kill_all_jobs("ark")

    def start_service(self):
        services.add("svc", "ark", "sleep 60", "host")
        services.tick(now=1000)
        jid = services.info("svc")["jid"]
        self.assertTrue(jobs.get_job("ark", jid).alive)
        return jid

    def test_supervisor_cannot_restart_anything_once_shutdown_begins(self):
        jid = self.start_service()
        lifecycle.begin_shutdown()
        time.sleep(0.3)
        self.assertFalse(jobs.get_job("ark", jid).alive)             # killed...
        services.tick(now=5000)                                      # ...and the supervisor, still
        self.assertFalse(services._rt["svc"]["running"])             # running in the old worker,
        self.assertEqual(services.info("svc")["jid"], None)          # does NOT bring it back
        services.resume()
        services.tick(now=5001)                                      # (it would after a resume)
        self.assertTrue(services._rt["svc"]["running"])

    def test_background_jobs_are_not_orphaned(self):
        jid, job = jobs.start_job("ark", "sleep 60", "/tmp")
        self.assertTrue(job.alive)
        lifecycle.begin_shutdown()
        time.sleep(0.3)
        self.assertFalse(job.alive)

    def test_final_backup_runs_after_processes_are_stopped_and_only_once(self):
        self.start_service()
        order = []
        backup.final = lambda timeout=25: order.append(("backup", services._paused,
                                                         services._rt["svc"]["running"]))
        lifecycle.begin_shutdown()
        lifecycle.begin_shutdown()
        lifecycle.begin_shutdown()
        self.assertEqual(order, [("backup", True, False)])          # paused, stopped, then saved; once

    def test_concurrent_callers_wait_for_the_first(self):
        gate = threading.Event()
        backup.final = lambda timeout=25: (gate.wait(5), self.calls.append("done"))
        t1 = threading.Thread(target=lifecycle.begin_shutdown)
        t1.start()
        time.sleep(0.2)
        finished = []
        t2 = threading.Thread(target=lambda: (lifecycle.begin_shutdown(), finished.append(1)))
        t2.start()
        time.sleep(0.3)
        self.assertEqual(finished, [])                              # still waiting for the backup
        gate.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual((self.calls, finished), (["done"], [1]))

    def test_async_shutdown_thread_is_non_daemon(self):
        """The interpreter must not exit before the final backup has finished."""
        t = lifecycle.begin_shutdown_async()
        self.assertFalse(t.daemon)
        t.join(5)
        self.assertEqual(self.calls, [("backup", lifecycle.FINAL_BACKUP_TIMEOUT)])


class GunicornConfigTests(unittest.TestCase):
    def conf(self):
        return runpy.run_path(str(ROOT / "gunicorn.conf.py"))

    def test_settings_that_protect_shutdown_and_state(self):
        c = self.conf()
        self.assertEqual(c["workers"], 1)                           # state is in memory
        self.assertEqual(c["keepalive"], 0)                         # an open browser connection used to
        self.assertGreaterEqual(c["graceful_timeout"], 25)          # hold shutdown for the whole timeout
        self.assertEqual(c["worker_class"], "gthread")

    def test_sigterm_handler_chains_gunicorns_and_starts_the_shutdown(self):
        c = self.conf()
        seen = []

        class FakeWorker:
            def handle_exit(self, sig, frame):
                seen.append(("gunicorn", sig))

        installed = {}
        real = signal.signal
        signal.signal = lambda sig, handler: installed.setdefault(sig, handler)
        started = []
        real_async = lifecycle.begin_shutdown_async
        lifecycle.begin_shutdown_async = lambda: started.append(1)
        try:
            c["post_worker_init"](FakeWorker())
            installed[signal.SIGTERM](signal.SIGTERM, None)
        finally:
            signal.signal = real
            lifecycle.begin_shutdown_async = real_async
        self.assertEqual(seen, [("gunicorn", signal.SIGTERM)])      # gunicorn still stops normally
        self.assertEqual(started, [1])                              # and our shutdown work begins

    def test_worker_exit_finishes_the_shutdown_work(self):
        c = self.conf()
        calls = []
        real = lifecycle.begin_shutdown
        lifecycle.begin_shutdown = lambda: calls.append(1)
        try:
            c["worker_exit"](None, None)
        finally:
            lifecycle.begin_shutdown = real
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
