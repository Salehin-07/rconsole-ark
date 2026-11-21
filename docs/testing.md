# Testing

```bash
uv run python -m unittest discover -s tests          # about 500 tests
RCONSOLE_VM_PROOT=/path/to/proot RCONSOLE_TEST_BUSYBOX=/path/to/busybox \
  uv run python -m unittest discover -s tests -v     # include real-VM end-to-end tests
uv run ruff check .
```

The suite covers hashing/migration, throttling, path-confinement (symlink and
`..` escapes), authorization of every entry point, CSRF, the file APIs, the
proxy, and — when proot and a static busybox are present — a real VM built from
a locally served root filesystem (download → checksum → extract → run).
