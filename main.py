"""rconsole entry point.

Kept for compatibility. The documented start command is ``uv run python serve.py`` (gunicorn where it
exists, a built-in server where it does not, e.g. Windows); this file starts the plain development server.
"""
from app import main

if __name__ == "__main__":
    main()
