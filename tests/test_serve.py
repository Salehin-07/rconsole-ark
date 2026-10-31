"""serve.py: gunicorn where it works, a built-in threaded server where it does not (Windows)."""
import os
import threading
import time
import unittest
import urllib.request
from unittest import mock

import helpers  # noqa: F401

import serve


class ChooseServerTests(unittest.TestCase):
    def test_gunicorn_when_usable(self):
        with mock.patch.object(serve, "gunicorn_usable", return_value=True):
            self.assertEqual(serve.choose_server({}), "gunicorn")

    def test_falls_back_when_gunicorn_is_not_usable(self):
        with mock.patch.object(serve, "gunicorn_usable", return_value=False):
            self.assertEqual(serve.choose_server({}), "builtin")

    def test_env_can_force_either_server(self):
        with mock.patch.object(serve, "gunicorn_usable", return_value=True):
            self.assertEqual(serve.choose_server({"RCONSOLE_SERVER": "builtin"}), "builtin")
        with mock.patch.object(serve, "gunicorn_usable", return_value=False):
            self.assertEqual(serve.choose_server({"RCONSOLE_SERVER": "gunicorn"}), "gunicorn")

    def test_windows_never_counts_as_gunicorn_capable(self):
        with mock.patch.object(serve.os, "name", "nt"):
            self.assertFalse(serve.gunicorn_usable())

    def test_forcing_gunicorn_where_it_is_missing_fails_loudly(self):
        with mock.patch.object(serve, "gunicorn_usable", return_value=False):
            with self.assertRaises(SystemExit) as cm:
                serve.run_gunicorn()
        self.assertIn("not available", str(cm.exception))

    def test_gunicorn_path_runs_the_production_command(self):
        with mock.patch.object(serve, "gunicorn_usable", return_value=True), \
                mock.patch.object(serve.os, "execvp") as ex, mock.patch.object(serve.os, "chdir"):
            serve.run_gunicorn()
        argv = ex.call_args[0][1]
        self.assertEqual(argv[1:3], ["-m", "gunicorn"])
        self.assertEqual(argv[3:], ["-c", "gunicorn.conf.py", "app:app"])


class BuiltinServerTests(unittest.TestCase):
    def test_serves_requests_and_runs_the_shutdown_on_stop(self):
        import lifecycle
        calls = []
        port = _free_port()
        with mock.patch.dict(os.environ, {"PORT": str(port), "RCONSOLE_HOST": "127.0.0.1"}), \
                mock.patch.object(lifecycle, "begin_shutdown", lambda: calls.append("shutdown")), \
                mock.patch.object(serve.signal, "signal"):
            t = threading.Thread(target=serve.run_builtin, daemon=True)
            t.start()
            body = None
            for _ in range(50):
                try:
                    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2).read()
                    break
                except OSError:
                    time.sleep(0.1)
            self.assertIsNotNone(body, "built-in server never answered /healthz")
            self.assertIn(b'"ok":true', body)
            _interrupt(t)
            t.join(5)
        self.assertEqual(calls, ["shutdown"])


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _interrupt(thread):
    """Raise KeyboardInterrupt inside the server thread (what Ctrl+C does in the main thread)."""
    import ctypes
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident), ctypes.py_object(KeyboardInterrupt))


if __name__ == "__main__":
    unittest.main()
