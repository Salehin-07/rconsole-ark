#!/usr/bin/env python3
"""Write the GitHub release notes for a version to stdout.

    python scripts/release_notes.py 1.1.0 [repo-slug]

The body is the version's CHANGELOG section followed by a short "host it yourself"
guide, so the release page is useful on its own.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def changelog_section(version, text):
    """The text under ``## <version>`` up to the next ``## `` heading."""
    m = re.search(rf"^## {re.escape(version)}\b[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not m:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    return m.group(1).strip()


def footer(version, slug):
    owner, _, repo = slug.partition("/")
    image = f"ghcr.io/{owner.lower()}/{repo.lower()}"
    return f"""
---

## Host it yourself

Everything you need is in this release. Pick one:

**1. Docker Compose with automatic HTTPS (any VPS)**
```bash
curl -L https://github.com/{slug}/releases/download/v{version}/rconsole-{version}.tar.gz | tar xz
cd rconsole-{version}/deploy
cp .env.example .env && nano .env        # set RCONSOLE_DOMAIN and RCONSOLE_ADMIN_PASSWORD
docker compose up -d --build
```

**2. Just the container**
```bash
docker run -d -p 5000:5000 -v rconsole-data:/data \\
  -e RCONSOLE_ADMIN_PASSWORD='choose-a-strong-one' {image}:{version}
```
(The image is built for amd64 and arm64. If `docker pull` is denied, the package is private: build from the
source archive instead, or make the package public in the repository's *Packages* settings.)

**3. No Docker** — extract the archive, `uv sync`, then `uv run gunicorn -c gunicorn.conf.py app:app`
(see `deploy/rconsole.service` for systemd).

Deploying to Render, Railway, Fly.io, Kubernetes, AWS/GCP/Azure or a Raspberry Pi? Follow **`DEPLOY.md`**.
All commands are documented in **`COMMANDS.md`**.

### Assets
| File | What |
|---|---|
| `rconsole-{version}.tar.gz` / `.zip` | the complete source: app, Dockerfile, `deploy/` configs, docs, tests |
| `docker-compose.yml`, `Caddyfile`, `.env.example` | the compose + HTTPS quick-start files on their own |
| `SHA256SUMS` | checksums — verify with `sha256sum -c SHA256SUMS` |

First login is `ark` with the password you set in `RCONSOLE_ADMIN_PASSWORD` (or `ark`/`ark`, which forces a change).
"""


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    version = argv[0].lstrip("v")
    slug = argv[1] if len(argv) > 1 else "Salehin-07/rconsole-ark"
    body = changelog_section(version, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    print(f"# rconsole {version}\n\n{body}\n{footer(version, slug)}")


if __name__ == "__main__":
    main(sys.argv[1:])
