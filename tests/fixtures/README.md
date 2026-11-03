Tiny real RAR archives used by `tests/test_fileops.py` (extraction through bsdtar).

They are test fixtures from the libarchive project
(<https://github.com/libarchive/libarchive>, `libarchive/test/test_read_format_rar*.rar.uu`,
BSD 2-clause licence), decoded from uuencode:

| file | libarchive fixture | content |
|---|---|---|
| `rar5_stored.rar` | `test_read_format_rar5_stored.rar` | `helloworld.txt` (29 bytes), RAR5, stored |
| `rar5_compressed.rar` | `test_read_format_rar5_compressed.rar` | `test.bin` (1200 bytes), RAR5, compressed |
| `rar3.rar` | `test_read_format_rar_noeof.rar` | `test.txt` (20 bytes), RAR 3, no end-of-archive marker |

RAR archives cannot be created without the proprietary `rar` tool, so real ones are kept here.
