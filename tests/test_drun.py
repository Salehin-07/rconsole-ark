"""``drun`` (vmtools/drun): the daemon-less ``docker run`` stand-in built on udocker."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRUN = ROOT / "vmtools" / "drun"


def run(args, path=None, env=None):
    e = dict(os.environ)
    e.update(env or {})
    if path is not None:
        e["PATH"] = path
    return subprocess.run(["sh", str(DRUN), *args], capture_output=True, text=True, env=e)


class DrunTests(unittest.TestCase):
    def test_script_is_valid_sh(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(DRUN)]).returncode, 0)

    def test_help_documents_the_limits(self):
        for args in (["--help"], []):
            r = run(args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("NOT Docker", r.stderr)
            self.assertIn("DRUN_OPTS", r.stderr)

    def test_says_so_when_udocker_is_missing(self):
        r = run(["hello-world"], path="/usr/bin:/bin")
        if subprocess.run(["sh", "-c", "PATH=/usr/bin:/bin command -v udocker"]).returncode == 0:
            self.skipTest("udocker is installed in /usr/bin")
        self.assertEqual(r.returncode, 127)
        self.assertIn("udocker is not installed", r.stderr)

    def test_flow_with_a_stub_udocker(self):
        """pull once, one container per image, seed /etc and /root, then run with the command."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            log, state, croot = tmp / "calls.log", tmp / "state", tmp / "croot"
            croot.mkdir()
            stub = tmp / "bin" / "udocker"
            stub.parent.mkdir()
            stub.write_text(f"""#!/bin/sh
echo "$*" >> {log}
args="$*"
case "$args" in
  *" inspect -p "*|"inspect -p "*|*"inspect -p "*) [ -e {state}.created ] && echo {croot} && exit 0; exit 1 ;;
  *"inspect "*) [ -e {state}.pulled ] && exit 0; exit 1 ;;
  *" pull "*|"pull "*) touch {state}.pulled ;;
  *" create "*|"create "*) touch {state}.created ;;
esac
exit 0
""")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            env = {"PATH": f"{stub.parent}:/usr/bin:/bin", "DRUN_PULL_OPTS": "--registry=http://x"}
            r = run(["hello-world", "/hello", "arg 1"], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            calls = log.read_text()
            self.assertIn("pull --registry=http://x hello-world", calls)
            self.assertIn("create --name=drun_hello-world hello-world", calls)
            self.assertIn("--execmode=P1 drun_hello-world", calls)
            self.assertIn("-q run drun_hello-world /hello arg 1", calls)
            self.assertTrue((croot / "etc" / "passwd").exists())     # images like hello-world have no /etc
            self.assertTrue((croot / "etc" / "group").exists())
            self.assertTrue((croot / "root").is_dir())                # nor a home to start in
            log.write_text("")
            self.assertEqual(run(["hello-world"], env=env).returncode, 0)
            again = log.read_text()
            self.assertNotIn(" pull ", " " + again)                   # second run: no pull, no create
            self.assertNotIn("create", again)

    def test_image_and_docs_wire_it_in(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("udocker==", dockerfile)
        self.assertIn("/usr/local/bin/drun", dockerfile)
        self.assertIn("vmtools/drun", dockerfile)
        self.assertIn("drun", (ROOT / "COMMANDS.md").read_text())
        self.assertIn("drun", (ROOT / "README.md").read_text())
        import devsetup
        self.assertIn("drun", devsetup.script("docker", "debian"))

    def test_refuses_inside_a_traced_shell(self):
        text = DRUN.read_text()
        self.assertIn("TracerPid", text)
        self.assertIn("proot cannot run inside proot", text)


if __name__ == "__main__":
    unittest.main()
