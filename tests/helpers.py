"""Shared test setup: isolates all rconsole state in a throwaway data dir.

Must be imported BEFORE any rconsole module, because ``config`` reads the
environment at import time.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "RCONSOLE_DATA_DIR" not in os.environ or not os.environ.get("RCONSOLE_TEST_KEEP"):
    _tmp = tempfile.mkdtemp(prefix="rconsole-test-")
    os.environ["RCONSOLE_DATA_DIR"] = _tmp
    atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.environ.pop("RENDER", None)
os.environ["RCONSOLE_LOGIN_ATTEMPTS"] = "3"
os.environ["RCONSOLE_LOGIN_LOCKOUT"] = "30"
os.environ["RCONSOLE_MIN_PASSWORD"] = "8"


def wipe_vms():
    """Remove every per-user VM so tests don't leak state into each other."""
    import config
    for d in list(config.VM_DIR.glob("*")) if config.VM_DIR.exists() else []:
        if d.is_dir() and d.name not in ("bin", "cache", "tmp"):
            shutil.rmtree(d, ignore_errors=True)
    try:
        import sandbox
        sandbox._installs.clear()
    except Exception:
        pass
