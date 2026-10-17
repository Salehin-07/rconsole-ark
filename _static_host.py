"""Bootstrap static file server for `serve` (serves a real materialized dir).

Usage: python _static_host.py <port> <directory> [--confine]

``--confine`` (used for VM directories) refuses to follow symlinks that leave
the served directory, so a link inside a VM can't expose host files.
"""
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


def make_handler(root, confine):
    root = os.path.realpath(root)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def translate_path(self, path):
            p = super().translate_path(path)
            if confine:
                real = os.path.realpath(p)
                if real != root and not real.startswith(root + os.sep):
                    return os.path.join(root, ".rconsole-forbidden")  # -> 404
            return p

    return Handler


if __name__ == "__main__":
    port = int(sys.argv[1])
    directory = sys.argv[2]
    confine = "--confine" in sys.argv[3:]
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(directory, confine))
    srv.serve_forever()
