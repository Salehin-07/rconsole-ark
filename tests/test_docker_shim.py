"""``docker`` / ``drun`` (vmtools/): the daemon-less docker shim built on udocker."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKER = ROOT / "vmtools" / "docker"
DRUN = ROOT / "vmtools" / "drun"

STUB = """#!/bin/sh
# fake udocker: records calls, keeps tiny state in $STUB_DIR
echo "$*" >> "$STUB_DIR/calls.log"
[ "$1" = "--allow-root" ] && shift
[ "$1" = "-q" ] && shift
case "$1" in
  inspect)
    if [ "$2" = "-p" ]; then [ -e "$STUB_DIR/created.$3" ] && echo "$STUB_DIR/root" && exit 0; exit 1; fi
    [ -e "$STUB_DIR/pulled" ] && exit 0; exit 1 ;;
  pull) touch "$STUB_DIR/pulled" ;;
  create) n="${2#--name=}"; touch "$STUB_DIR/created.$n" ;;
  rm) rm -f "$STUB_DIR/created.$2" ;;
  run) shift; echo "RUN $*" >> "$STUB_DIR/calls.log"; [ -n "${STUB_RC:-}" ] && exit "$STUB_RC" ;;
  version) echo "version: 9.9.9" ;;
esac
exit 0
"""


class Env:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "root").mkdir()
        bindir = self.dir / "bin"
        bindir.mkdir()
        stub = bindir / "udocker"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.bindir = bindir

    def run(self, args, script=DOCKER, **extra):
        env = {"PATH": f"{self.bindir}:/usr/bin:/bin", "STUB_DIR": str(self.dir), "HOME": str(self.dir)}
        env.update({k: str(v) for k, v in extra.items()})
        return subprocess.run(["sh", str(script), *args], capture_output=True, text=True, env=env)

    @property
    def calls(self):
        p = self.dir / "calls.log"
        return p.read_text() if p.exists() else ""


class DockerShimTests(unittest.TestCase):
    def test_scripts_are_valid_sh(self):
        for script in (DOCKER, DRUN):
            self.assertEqual(subprocess.run(["sh", "-n", str(script)]).returncode, 0, script)

    def test_help_is_honest(self):
        r = Env(self).run(["--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("NO Docker daemon", r.stderr)
        self.assertIn("STATIC", r.stderr)

    def test_missing_udocker_is_reported(self):
        e = Env(self)
        (e.bindir / "udocker").unlink()
        r = e.run(["run", "x"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("udocker is not installed", r.stderr)

    def test_run_translates_options(self):
        e = Env(self)
        r = e.run(["run", "-it", "-e", "A=b", "-e", "FROMENV", "-v", "/h:/c:ro", "-w", "/w", "-u", "5",
                   "--entrypoint", "ep", "-p", "80:80", "--restart", "always", "--name", "keep",
                   "img", "cmd", "a b"], FROMENV="zz")
        self.assertEqual(r.returncode, 0, r.stderr)
        run = [line for line in e.calls.splitlines() if line.startswith("RUN ")][0]
        for part in ("--env=A=b", "--env=FROMENV=zz", "--volume=/h:/c", "--workdir=/w", "--user=5",
                     "--entrypoint=ep", "keep cmd a b"):
            self.assertIn(part, run)
        self.assertNotIn(":ro", run)
        self.assertIn("ignoring -p 80:80", r.stderr)          # no port publishing without a daemon
        self.assertIn("--restart", r.stderr)                    # unsupported options are said out loud
        self.assertTrue((e.dir / "root/etc/passwd").exists())  # images like hello-world have no /etc
        self.assertTrue((e.dir / "root/etc/group").exists())
        self.assertTrue((e.dir / "root/root").is_dir())

    def test_pull_only_when_the_image_is_missing(self):
        e = Env(self)
        e.run(["run", "img"])
        self.assertEqual(e.calls.count("pull "), 1)
        e.run(["run", "img"])
        self.assertEqual(e.calls.count("pull "), 1)

    def test_container_is_removed_unless_named(self):
        e = Env(self)
        e.run(["run", "img"])
        self.assertIn("rm dk_", e.calls)
        e2 = Env(self)
        e2.run(["run", "--name", "keep", "img"])
        self.assertNotIn("rm ", e2.calls.replace("--rm", ""))
        e3 = Env(self)
        e3.run(["run", "--rm", "--name", "gone", "img"])
        self.assertIn("rm gone", e3.calls)

    def test_exit_code_passes_through(self):
        r = Env(self).run(["run", "img", "false"], STUB_RC=7)
        self.assertEqual(r.returncode, 7)

    def test_execution_mode(self):
        e = Env(self)
        e.run(["run", "img"])
        self.assertIn("--execmode=P1", e.calls)                  # host shell: proot
        e2 = Env(self)
        e2.run(["run", "img"], DRUN_MODE="F1")
        self.assertIn("--execmode=F1", e2.calls)                 # what a VM uses

    def test_detached_is_refused_clearly(self):
        for args in (["run", "-d", "img"], ["run", "-itd", "img"], ["run", "--detach", "img"]):
            r = Env(self).run(args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("needs a Docker daemon", r.stderr)

    def test_other_commands_map_to_udocker(self):
        e = Env(self)
        e.run(["pull", "alpine"])
        e.run(["images"])
        e.run(["ps", "-a"])
        e.run(["rm", "a", "b"])
        e.run(["rmi", "alpine"])
        for call in ("pull alpine", "images", "ps", "rm a", "rm b", "rmi alpine"):
            self.assertIn(call, e.calls)

    def test_daemon_only_commands_say_why(self):
        for cmd in ("exec", "logs", "build", "compose", "stop", "network"):
            r = Env(self).run([cmd, "x"])
            self.assertNotEqual(r.returncode, 0, cmd)
            self.assertIn("needs a Docker daemon", r.stderr)
            self.assertIn("DOCKER_HOST", r.stderr)

    def test_version_does_not_pretend(self):
        r = Env(self).run(["--version"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("udocker 9.9.9", r.stdout)
        self.assertIn("no Docker daemon", r.stdout)

    def test_docker_host_hands_over_to_a_real_client(self):
        e = Env(self)
        real = e.dir / "real-docker"
        real.write_text('#!/bin/sh\necho "REAL $*"\n')
        real.chmod(0o755)
        r = e.run(["ps"], DOCKER_HOST="ssh://x", DOCKER_REAL=real)
        self.assertEqual(r.stdout.strip(), "REAL ps")
        self.assertEqual(e.calls, "")                             # udocker untouched
        r = e.run(["images"], DOCKER_HOST="ssh://x", DOCKER_REAL=real, DOCKER_SHIM_LOCAL="1")
        self.assertIn("images", e.calls)                          # forced local

    def test_drun_is_docker_run_and_always_local(self):
        e = Env(self)
        r = e.run(["img", "cmd"], script=DRUN, DOCKER_HOST="ssh://x")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(line.startswith("RUN ") and line.endswith("cmd") for line in e.calls.splitlines()))
        self.assertEqual(Env(self).run(["--help"], script=DRUN).returncode, 0)

    def test_image_and_docs_wire_it_in(self):
        import devsetup
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn(f"udocker=={devsetup.UDOCKER_VERSION}", dockerfile)
        self.assertIn("/usr/local/bin/docker", dockerfile)
        self.assertIn("/usr/local/bin/drun", dockerfile)
        for doc in ("COMMANDS.md", "README.md"):
            self.assertIn("udocker", (ROOT / doc).read_text(), doc)


if __name__ == "__main__":
    unittest.main()
