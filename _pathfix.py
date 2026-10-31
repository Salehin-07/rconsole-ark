"""Imported first by ``app.py``: makes dependencies installed by ``rconsole update`` importable.

An update that changes ``requirements.txt`` installs the new packages into ``<data>/update/site-<version>``
(never into the system's site-packages); this puts that folder at the front of ``sys.path`` before Flask
and friends are imported. A no-op unless an update installed dependencies."""
import updater

updater.activate_deps()
