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
    docker  Docker CLI + compose + buildx (see the note below: no local daemon in a VM)
    kali    the command-line Kali toolset (``kali-linux-headless``) — Kali VMs only
    all     tools, build, python, node, rust and db (not docker, not kali)

``docker`` installs the client side only: a VM runs under proot, which has no namespaces,
cgroups or netfilter, so ``dockerd`` cannot start inside it. Point the CLI at a remote
engine with ``DOCKER_HOST``. ``kali`` refuses to run in any other system.

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
    "docker": "Docker CLI, compose and buildx (client only: dockerd can't run inside a VM — "
              "use DOCKER_HOST for a remote engine)",
    "kali":   "Kali VM only: the command-line Kali toolset (kali-linux-headless — several GB, "
              "a long install)",
    "all":    "tools, build, python, node, rust and db (not docker or kali)",
}
ORDER = ["tools", "build", "python", "node", "rust", "db"]
# Profiles that only make sense on one system (profile -> distro id); anything else is refused.
ONLY_ON = {"kali": "kali"}

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
    "docker": "docker.io docker-cli docker-compose docker-buildx",
    # Kali: the base the toolset needs; the toolset itself is the kali-linux-headless metapackage.
    "kali":   "kali-defaults wget nano vim-tiny less procps net-tools iputils-ping dnsutils "
              "python3 python3-pip wordlists",
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
    "docker": "docker docker-cli docker-cli-compose docker-cli-buildx",
}
KALI_METAPACKAGE = "kali-linux-headless"
KALI_FALLBACK = "kali-tools-top10"

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


def check_target(profile, distro_id):
    """``None`` if ``profile`` may run on the system ``distro_id``, else why not."""
    need = ONLY_ON.get(profile)
    if need and distro_id != need:
        return (f"the '{profile}' profile is only for {need.capitalize()} VMs "
                f"(this one is {distro_id or 'unknown'}). Install it with: vm install {need}")
    return None


def packages(profile, family):
    """The package names for ``profile`` on ``family`` (deduplicated, ordered)."""
    table = _ALPINE if family == "alpine" else _DEBIAN
    if profile not in table and profile not in PROFILES:
        raise ValueError(f"unknown profile '{profile}'")
    if profile == "kali" and family == "alpine":
        raise ValueError("the 'kali' profile needs a Debian-family (Kali) system")
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
    if profile == "kali":
        return _kali_script(family)
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
    if "docker" in parts:
        checks += ["docker --version", "docker compose version"]
    check_lines = "\n".join(
        f'{c} 2>/dev/null || echo "(not available: {c.split()[0]})"' for c in checks)
    if profile == "docker":
        closing = _DOCKER_NOTE
    else:
        closing = 'echo "==> Done. Try: pip install psycopg2   (builds from source with the headers installed)"'
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
{closing}
"""


_DOCKER_NOTE = """echo "==> Done. The Docker client is installed."
echo
echo "NOTE: this system runs under proot, which has no namespaces, cgroups or netfilter,"
echo "      so the Docker daemon (dockerd) cannot start inside it. Use the CLI against a"
echo "      remote engine instead, for example:"
echo "        export DOCKER_HOST=ssh://user@your-server      # or tcp://host:2376 with TLS"
echo "        docker ps"
"""


def _kali_script(family):
    """The Kali toolset: ``kali-linux-headless`` (all the command-line tools, no desktop)."""
    if family == "alpine":
        raise ValueError("the 'kali' profile needs a Debian-family (Kali) system")
    base = " ".join(packages("kali", family))
    return f"""#!/bin/sh
# Generated by `vm setup kali` for a kali system.
export DEBIAN_FRONTEND=noninteractive
echo "==> Setting up Kali: {KALI_METAPACKAGE} (the command-line toolset, no desktop)."
echo "    This is several GB and can take a long while; it keeps going if you leave."
# There is no init system in a VM: never let a package try to start a service.
printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d
apt-get update || {{ echo "!! package index update failed (no network?)"; rm -f /usr/sbin/policy-rc.d; exit 1; }}
inst() {{
  apt-get install -y --no-install-recommends "$@" || {{
    for p in "$@"; do apt-get install -y --no-install-recommends "$p" \\
      || echo "!! skipped $p (not available on this release)"; done
  }}
}}
inst {base}
if apt-get install -y {KALI_METAPACKAGE}; then
  echo "==> {KALI_METAPACKAGE} installed."
else
  echo "!! {KALI_METAPACKAGE} could not be installed completely; trying {KALI_FALLBACK} instead"
  apt-get install -y {KALI_FALLBACK} || echo "!! {KALI_FALLBACK} failed too: check the messages above, then re-run"
fi
rm -f /usr/sbin/policy-rc.d
echo
echo "==> Installed. A few of the tools:"
for t in nmap sqlmap hydra john nikto gobuster aircrack-ng msfconsole; do
  command -v "$t" >/dev/null 2>&1 && echo "  ok  $t" || echo "  --  $t (not found)"
done
echo
echo "==> Done. Want more? apt install kali-linux-large   (bigger still: kali-linux-everything)"
echo "    Use these tools only on systems you are authorized to test."
"""


def describe():
    """Human-readable profile list."""
    width = max(len(p) for p in PROFILES)
    return "\n".join(f"  {name:<{width}}  {text}" for name, text in PROFILES.items())


def quoted_path_cmd(path):
    """Shell command that runs the generated script at ``path`` (inside the VM)."""
    return f"sh {shlex.quote(path)}"
