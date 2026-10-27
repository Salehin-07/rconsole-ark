"""Runs first, before anything reads or writes the data directory.

``app.py`` imports this before ``auth`` (which creates the password pepper at
import time) so that, on a blank instance, a backup is restored *before* fresh
secrets are generated — otherwise every restored password hash would be invalid.
It only imports :mod:`config` and :mod:`backup`, which touch nothing else.
"""
import sys

import backup

try:
    backup.restore_if_fresh(log=lambda m: print(m, file=sys.stderr, flush=True))
except Exception as e:  # a failed restore must never stop the server from booting
    print(f"[rconsole] restore skipped: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
