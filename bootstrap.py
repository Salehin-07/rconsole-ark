"""Runs first, before anything reads or writes the data directory.

``app.py`` imports this before ``auth`` (which creates the password pepper at
import time) so that, on a blank instance, a backup is restored *before* fresh
secrets are generated — otherwise every restored password hash would be invalid.
It only imports :mod:`config`, :mod:`backup` and :mod:`updater`, which touch nothing else.
"""
import sys

import backup

def _log(m):
    print(m, file=sys.stderr, flush=True)


try:
    if backup.restore_if_fresh(log=_log):
        # The backup also carries the release `rconsole update` installed: on a disk-less host the image
        # was rebuilt from older code, so put the newer release back (and restart once to load it).
        import updater
        updater.after_restore(log=_log)
except Exception as e:  # a failed restore must never stop the server from booting
    print(f"[rconsole] restore skipped: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

try:
    # A restore may have brought back settings.json (dashboard changes): apply it, then rebuild the files that
    # live in the repo (workspace and VM folders) before anything starts that needs them.
    import settings
    import gitstore
    settings.apply_saved()
    gitstore.bootstrap(log=_log)
except Exception as e:                      # never stop the server from booting
    print(f"[rconsole] repo storage skipped: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
