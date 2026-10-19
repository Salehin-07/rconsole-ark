"""``vm setup <profile>`` — install a ready-to-use toolchain inside a VM.

Getting ``pip install psycopg2`` (or Pillow, lxml, mysqlclient, cryptography, …)
to build needs a compiler *and* each library's development headers, and the
package names differ between Alpine and the Debian family. Profiles bundle the
right set for each:

    tools   everyday utilities (nano, vim, wget, ps, htop, zip, jq, ssh, …)
    build   compilers and build systems (gcc, make, cmake, autotools, pkg-config)
    python  Python 3 + pip + venv + build tools + headers for the usual suspects
            (PostgreSQL, MySQL/MariaDB, SQLite, libffi, OpenSSL, XML, JPEG/PNG, …)
    node    Node.js + npm
    rust    Rust + cargo (for wheels that compile from source)
    db      database command-line clients (psql, mysql, redis-cli, sqlite3)
    all     everything above

The generated script installs the whole set in one go and, if a single package is
not available on a given release, falls back to installing them one by one and
skips only the missing one — so one odd name can never sink the setup.
"""
import shlex

PROFILES = {
    "tools":  "Everyday tools: nano, vim, wget, curl, git, ps, htop, zip, jq, ssh, …",
    "build":  "Compilers and build systems: gcc, g++, make, cmake, autotools, pkg-config",
    "python": "Python 3, pip, venv + build tools + headers for psycopg2, mysqlclient, "
              "Pillow, lxml, cryptography, …",
    "node":   "Node.js and npm",
    "rust":   "Rust and cargo",
    "db":     "Database clients: psql, mysql, redis-cli, sqlite3",
    "all":    "All of the above",
}
ORDER = ["tools", "build", "python", "node", "rust", "db"]

# ---- package sets -----------------------------------------------------------
_DEBIAN = {
    "base":   "ca-certificates curl git pkg-config",
    "tools":  "nano vim-tiny less wget curl git procps htop unzip zip xz-utils bzip2 "
              "file tree jq rsync openssh-client iputils-ping dnsutils net-tools locales",
    "build":  "build-essential make cmake autoconf automake libtool pkg-config gdb",
    "python": "python3 python3-pip python3-venv python3-dev python3-setuptools "
              "python3-wheel libpq-dev default-libmysqlclient-dev libsqlite3-dev "
              "libffi-dev libssl-dev libxml2-dev libxslt1-dev zlib1g-dev libjpeg-dev "
              "libpng-dev libfreetype6-dev libwebp-dev libyaml-dev libldap2-dev "
              "libsasl2-dev libcurl4-openssl-dev libbz2-dev liblzma-dev libreadline-dev",
    "node":   "nodejs npm",
    "rust":   "rustc cargo",
    "db":     "postgresql-client default-mysql-client redis-tools sqlite3",
}
_ALPINE = {
    "base":   "ca-certificates curl git pkgconf",
    "tools":  "nano vim less wget curl git procps htop unzip zip xz bzip2 file tree jq "
              "rsync openssh-client iputils bind-tools net-tools",
    "build":  "build-base make cmake autoconf automake libtool pkgconf linux-headers gdb",
    "python": "python3 py3-pip py3-setuptools py3-wheel python3-dev postgresql-dev "
              "mariadb-dev sqlite-dev libffi-dev openssl-dev libxml2-dev libxslt-dev "
              "zlib-dev jpeg-dev libpng-dev freetype-dev libwebp-dev yaml-dev "
              "openldap-dev cyrus-sasl-dev curl-dev bzip2-dev xz-dev readline-dev",
    "node":   "nodejs npm",
    "rust":   "rust cargo",
    "db":     "postgresql-client mariadb-client redis sqlite",
}

# Profiles whose script needs the build tools too.
_NEEDS = {"python": ("build",), "rust": ("build",)}


def expand(profile):
    """Profile -> ordered list of the base profiles it consists of."""
    if profile == "all":
        return list(ORDER)
    out = []
    for dep in _NEEDS.get(profile, ()):
        out.append(dep)
    out.append(profile)
    return out


def packages(profile, family):
    """The package names for ``profile`` on ``family`` (deduplicated, ordered)."""
    table = _ALPINE if family == "alpine" else _DEBIAN
    seen, names = set(), []
    for part in ["base"] + expand(profile):
        for pkg in table[part].split():
            if pkg not in seen:
                seen.add(pkg)
                names.append(pkg)
    return names


def script(profile, family):
    """A POSIX ``sh`` script that installs ``profile`` on ``family``."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile '{profile}'")
    pkgs = " ".join(packages(profile, family))
    if family == "alpine":
        refresh = "apk update"
        install = ('inst() {\n'
                   '  apk add --no-cache "$@" || {\n'
                   '    for p in "$@"; do apk add --no-cache "$p" || echo "!! skipped $p (not available)"; done\n'
                   '  }\n}')
    else:
        refresh = "apt-get update"
        install = ('inst() {\n'
                   '  apt-get install -y --no-install-recommends "$@" || {\n'
                   '    for p in "$@"; do apt-get install -y --no-install-recommends "$p" \\\n'
                   '      || echo "!! skipped $p (not available on this release)"; done\n'
                   '  }\n}')
    checks = []
    parts = expand(profile)
    if "python" in parts:
        checks += ["python3 --version", "pip --version"]
    if "build" in parts:
        checks += ["gcc --version | head -n 1", "make --version | head -n 1"]
    if "node" in parts:
        checks += ["node --version", "npm --version"]
    if "rust" in parts:
        checks += ["rustc --version", "cargo --version"]
    if "db" in parts:
        checks += ["psql --version", "redis-cli --version"]
    check_lines = "\n".join(
        f'{c} 2>/dev/null || echo "(not available: {c.split()[0]})"' for c in checks)
    return f"""#!/bin/sh
# Generated by `vm setup {profile}` for a {family} system.
export DEBIAN_FRONTEND=noninteractive
echo "==> Setting up '{profile}' ({len(packages(profile, family))} packages) — this takes a few minutes"
{refresh} || {{ echo "!! package index update failed (no network?)"; exit 1; }}
{install}
inst {pkgs}
echo
echo "==> Installed. Versions:"
{check_lines}
echo
echo "==> Done. Try: pip install psycopg2   (builds from source with the headers installed)"
"""


def describe():
    """Human-readable profile list."""
    width = max(len(p) for p in PROFILES)
    return "\n".join(f"  {name:<{width}}  {text}" for name, text in PROFILES.items())


def quoted_path_cmd(path):
    """Shell command that runs the generated script at ``path`` (inside the VM)."""
    return f"sh {shlex.quote(path)}"
