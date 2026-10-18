"""Catalog of Linux systems the per-user VM can be built from.

Each entry knows how to *resolve* a root-filesystem tarball for the current CPU
architecture: the download URL plus the SHA-256 published by the distribution
itself (never a value baked in here, so releases keep working as they roll).

    vm install                 -> Alpine (default)
    vm install debian          -> Debian stable
    vm install debian:bookworm -> a specific Debian release
    vm install ubuntu:24.04    -> a specific Ubuntu release
    vm install kali            -> Kali Linux (minimal)

Sources
-------
alpine  dl-cdn.alpinelinux.org  latest-releases.yaml (sha256 included)
ubuntu  cdimage.ubuntu.com      ubuntu-base ``SHA256SUMS``
debian  images.linuxcontainers.org (mirror: uk.lxd.images.canonical.com)
kali    kali.download nethunter rootfs ``SHA256SUMS`` (LXC image as fallback)

The resolvers only talk HTTP through :func:`_get`, so tests can swap it out.
"""
import os
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

import requests


class DistroError(Exception):
    """Unknown distro/release, unsupported CPU, or the mirror is unreachable."""


@dataclass(frozen=True)
class Source:
    """A resolved root filesystem download."""
    url: str
    sha256: str
    filename: str
    label: str            # human readable, e.g. "Debian 13 (trixie)"


@dataclass(frozen=True)
class Distro:
    id: str
    name: str
    family: str           # "alpine" | "debian" — drives package-manager hints
    summary: str
    size: str             # approximate download
    default_release: str
    release_help: str
    pkg: str              # package manager name
    aliases: tuple = ()


# ---------------------------------------------------------------- HTTP helper
def _get(url, timeout=25):
    """GET ``url`` and return its text. Raises DistroError on any failure."""
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 404:
            raise DistroError(f"not found: {url}")
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        host = url.split("/")[2] if "//" in url else url
        raise DistroError(f"could not reach {host}: {e}") from e


def machine():
    return os.uname().machine.lower()


def _arch(table, distro):
    arch = table.get(machine())
    if not arch:
        raise DistroError(f"{distro} has no image for CPU '{machine()}'")
    return arch


_AMD64_ARM64 = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_ALPINE_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def parse_sums(text):
    """Parse a ``sha256sum`` file into ``{filename: hexdigest}``."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


# --------------------------------------------------------------------- alpine
def parse_alpine_release(text, flavor="alpine-minirootfs"):
    """Pick ``(file, sha256)`` for ``flavor`` from Alpine's latest-releases.yaml."""
    entry, entries = {}, []
    for line in text.splitlines():
        if line.strip() == "-":
            if entry:
                entries.append(entry)
            entry = {}
            continue
        m = re.match(r"\s+(\w+):\s*\"?([^\"]*?)\"?\s*$", line)
        if m:
            entry[m.group(1)] = m.group(2)
    if entry:
        entries.append(entry)
    for e in entries:
        if e.get("flavor") == flavor and e.get("file") and e.get("sha256"):
            return e["file"], e["sha256"]
    raise DistroError(f"no {flavor} entry in the Alpine release index")


def _resolve_alpine(release):
    arch = _arch(_ALPINE_ARCH, "Alpine")
    branch = "latest-stable" if release in ("latest", "stable") else "v" + release.lstrip("v")
    base = f"https://dl-cdn.alpinelinux.org/alpine/{branch}/releases/{arch}/"
    fname, digest = parse_alpine_release(_get(base + "latest-releases.yaml"))
    label = "Alpine Linux " + (re.search(r"minirootfs-([\d.]+)-", fname) or [None, release])[1]
    return Source(base + fname, digest, fname, label)


# --------------------------------------------------------------------- ubuntu
UBUNTU_LTS_CANDIDATES = ("26.04", "24.04")   # newest first; first that exists wins


def _resolve_ubuntu(release):
    arch = _arch(_AMD64_ARM64, "Ubuntu")
    candidates = UBUNTU_LTS_CANDIDATES if release == "lts" else (release,)
    last = None
    for rel in candidates:
        base = f"https://cdimage.ubuntu.com/ubuntu-base/releases/{rel}/release/"
        try:
            sums = parse_sums(_get(base + "SHA256SUMS"))
        except DistroError as e:
            last = e
            continue
        pat = re.compile(rf"^(ubuntu-base|[a-z]+)-.*base-{arch}\.tar\.gz$")
        names = sorted(n for n in sums if pat.match(n))
        if names:
            fname = names[-1]
            return Source(base + fname, sums[fname], fname, f"Ubuntu {rel}")
        last = DistroError(f"no {arch} image listed for Ubuntu {rel}")
    raise DistroError(f"Ubuntu {release}: {last}")


# --------------------------------------------------------------------- debian
_LXC_MIRRORS = ("https://images.linuxcontainers.org", "https://uk.lxd.images.canonical.com")


def _lxc_latest(distro, release, arch, label):
    """Newest build of an LXC image: checks each mirror until one answers."""
    last = None
    for mirror in _LXC_MIRRORS:
        base = f"{mirror}/images/{distro}/{release}/{arch}/default/"
        try:
            page = _get(base)
            builds = sorted(
                unquote(h) for h in re.findall(r'href="([^"/?]+)/"', page)
                if re.fullmatch(r"\d{8}_\d{2}(?:%3A|:)\d{2}", h, re.I))
            if not builds:
                raise DistroError(f"no builds listed at {base}")
            build = builds[-1]
            dirurl = base + quote(build) + "/"
            sums = parse_sums(_get(dirurl + "SHA256SUMS"))
            if "rootfs.tar.xz" not in sums:
                raise DistroError(f"rootfs.tar.xz not in {dirurl}SHA256SUMS")
            safe = re.sub(r"[^0-9A-Za-z]", "", build)
            fname = f"{distro}-{release}-{arch}-{safe}.tar.xz"
            return Source(dirurl + "rootfs.tar.xz", sums["rootfs.tar.xz"], fname,
                          f"{label} ({release})")
        except DistroError as e:
            last = e
    raise last


def _resolve_debian(release):
    arch = _arch(_AMD64_ARM64, "Debian")
    return _lxc_latest("debian", release, arch, "Debian")


# ------------------------------------------------------------------------ kali
def _resolve_kali(variant):
    arch = _arch(_AMD64_ARM64, "Kali")
    base = "https://kali.download/nethunter-images/current/rootfs/"
    try:
        sums = parse_sums(_get(base + "SHA256SUMS"))
        for pat in (rf"^kali-nethunter-rootfs-{variant}-{arch}\.tar\.xz$",
                    rf"^kalifs-{arch}-{variant}\.tar\.xz$"):
            hit = sorted(n for n in sums if re.match(pat, n))
            if hit:
                return Source(base + hit[0], sums[hit[0]], hit[0], f"Kali Linux ({variant})")
        have = ", ".join(sorted(n for n in sums if n.endswith(".tar.xz"))[:6]) or "nothing"
        raise DistroError(f"Kali variant '{variant}' not found for {arch} (have: {have})")
    except DistroError:
        if variant != "minimal":
            raise
    # Fall back to the community-built LXC image of the rolling release.
    return _lxc_latest("kali", "current", arch, "Kali Linux")


# --------------------------------------------------------------------- catalog
_RESOLVERS = {
    "alpine": _resolve_alpine,
    "debian": _resolve_debian,
    "ubuntu": _resolve_ubuntu,
    "kali": _resolve_kali,
}

CATALOG = {
    "alpine": Distro(
        "alpine", "Alpine Linux", "alpine",
        "tiny and the fastest to set up (default)", "~3 MB", "latest",
        "latest, or a branch like 3.21 / edge", "apk", ("alp",)),
    "debian": Distro(
        "debian", "Debian", "debian",
        "the universal OS: huge package archive, rock solid", "~90 MB", "trixie",
        "a codename: trixie (13, default), bookworm (12), sid", "apt", ("deb",)),
    "ubuntu": Distro(
        "ubuntu", "Ubuntu", "debian",
        "familiar apt system, newest LTS by default", "~35 MB", "lts",
        "lts (default), or 24.04 / 22.04 …", "apt", ("ubu",)),
    "kali": Distro(
        "kali", "Kali Linux", "debian",
        "security-testing distro (authorized use only)", "~140 MB", "minimal",
        "minimal (default), nano, full (1.7 GB, too big)", "apt",
        ("kali-linux", "kalilinux", "কালী")),
}

DEFAULT_DISTRO = "alpine"


def _alias_map():
    m = {}
    for d in CATALOG.values():
        m[d.id] = d
        for a in d.aliases:
            m[a.lower()] = d
    return m


def lookup(name):
    """Find a distro by id or alias (case-insensitive)."""
    d = _alias_map().get((name or "").strip().lower())
    if not d:
        ids = ", ".join(CATALOG)
        raise DistroError(f"unknown system '{name}'. Available: {ids} (see `vm list`)")
    return d


def parse_choice(text):
    """``"debian:bookworm"`` -> ``(Distro, "bookworm")``; release defaults apply."""
    text = (text or DEFAULT_DISTRO).strip()
    name, _, release = text.partition(":")
    d = lookup(name)
    release = release.strip().lower() or d.default_release
    if not re.fullmatch(r"[a-z0-9._-]{1,32}", release):
        raise DistroError(f"bad release name '{release}'")
    return d, release


def resolve(distro, release):
    """Resolve the download for ``distro`` (a :class:`Distro`) and ``release``."""
    return _RESOLVERS[distro.id](release)


# ---------------------------------------------------------------- user guidance
def install_hint(family, packages_alpine, packages_debian=None):
    """Shell line that installs packages on this family (for error messages)."""
    if family == "alpine":
        return f"apk add {packages_alpine}"
    return f"apt update && apt install -y {packages_debian or packages_alpine}"


def python_setup_hint(family):
    """Command that prepares a VM to host a WSGI app with gunicorn."""
    return install_hint(family, "python3 py3-pip", "python3 python3-pip") + \
        " && pip install --break-system-packages gunicorn"
