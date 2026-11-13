# Quick start

Pick one path. All three end at the same login screen and the same first steps.

## Path 1: try it on your own computer

You need [Python 3.14 or newer](compatibility.md) and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Salehin-07/rconsole-ark.git
cd rconsole-ark
uv sync
uv run python serve.py          # http://127.0.0.1:5000
```

`serve.py` uses gunicorn where it exists and a built-in server on Windows. Log in as **`ark` / `ark`**; change that
password at once (Dashboard → Account, or `sudo cpass`); the dashboard warns until you do. To skip the default, set
`RCONSOLE_ADMIN_PASSWORD` before the first start.

Linux systems (`vm`) need Linux. On Windows you get the host shell, editor and file manager only.

## Path 2: free hosting on Render

1. Create a **private** GitHub repository for backups (for example `rconsole-data`) and a fine-grained token with
   *Contents: read and write* on that repository only.
2. Choose a long backup passphrase and keep a copy outside Rconsole.
3. In Render: **New → Blueprint**, pick your copy of this repository. Render reads [`render.yaml`](../render.yaml), the
   **free** blueprint.
4. Fill in `RCONSOLE_ADMIN_PASSWORD`, `RCONSOLE_BACKUP_URL`, `RCONSOLE_BACKUP_TOKEN` and `RCONSOLE_BACKUP_PASSPHRASE`.
5. When the deploy is **Live**, open the `*.onrender.com` address and log in as `ark`.

The free plan has no disk, 512 MB of RAM and may restart at any time. Read the
[free hosting guide](../FREE_HOSTING.md) for what that means in practice.

## Path 3: a VPS with Docker Compose (automatic HTTPS)

Point a DNS name at the server first, then:

```bash
git clone https://github.com/Salehin-07/rconsole-ark.git
cd rconsole-ark/deploy
cp .env.example .env && nano .env     # RCONSOLE_DOMAIN and RCONSOLE_ADMIN_PASSWORD
docker compose up -d --build
```

This keeps data in a Docker volume at `/data`, so it survives redeploys without backups (backups are still a good
safety net). More options, other platforms and prerequisites: [Deploy anywhere](../DEPLOY.md).

## Your first ten minutes

```text
vm install debian                  # your own Linux system (about a minute)
vm setup python                    # Python, compiler and headers
vm shell                           # a root shell inside it; exit when done

service add hello --vm debian --cwd /root --expose 8000:hello python3 -m http.server 8000
service logs hello                 # see the output
expose list                        # shows [up] or [down]
```

The service now runs at `https://<your-domain>/hello/` for logged-in users, starts again after a restart and restarts
itself if it crashes. If backups are configured (`sudo backup`), the definition survives a redeploy too.

Next: [Backups](backup.md), [Configuration](configuration.md), [Commands](../COMMANDS.md).
