# Deploying Rconsole

> **No budget?** rconsole's main purpose is to run bots, websites and workers on a **free** tier. Follow
> **[FREE_HOSTING.md](FREE_HOSTING.md)** first (Render free + a private backup repo). This guide covers every other
> platform and paid setups.

This guide takes you from "it runs on my laptop" to a public, HTTPS, persistent
rconsole on **any** platform — a one-click PaaS, a $5 VPS, Kubernetes, or a
Raspberry Pi at home. Pick your platform in the [table](#2-choose-a-platform),
jump to its section, then finish with the [post-deploy checklist](#14-after-you-deploy).

> **Honesty note.** The config files in [`deploy/`](deploy/) are syntax-checked
> (nginx with `nginx -t`, YAML/TOML parsed, the image is built and health-checked
> in CI), and the VM was tested against a real Ubuntu root filesystem. Platform
> dashboards change often, so treat the click-paths below as guidance and trust
> each provider's current docs when they differ.

---

## 1. What rconsole needs from a host

rconsole is a stateful, long-running web app with websockets. Any platform that
satisfies these five rules works; the rest of this guide is just *how* on each one.

| # | Rule | Why |
|---|------|-----|
| 1 | **Exactly one instance** (one container/process) | Sessions, terminals, jobs and hosted apps live in that process's memory. Two instances = users bounce between them and lose terminals. |
| 2 | **Persistent storage: a volume, or backups** | *Architecture A:* a persistent volume with `RCONSOLE_DATA_DIR` pointing at it; data survives redeploys. *Architecture B:* no volume (e.g. Render free), but [backups](docs/backup.md) to a private repo: the instance restores itself after every restart, with minutes of downtime and loss of anything since the last backup. With neither, each deploy resets everything. |
| 3 | **Websockets + long idle timeouts** | The browser terminal is a websocket. Proxies that cut idle connections after 30–60 s will drop terminals. |
| 4 | **HTTPS in front** | Login cookies are `Secure`; never expose the console over plain HTTP on the internet. |
| 5 | **Outbound internet** | `vm install` downloads proot 5.5.0 (from GitHub, checksum-verified, once) and the system image (Alpine/Debian/Ubuntu/Kali mirrors), and `apk`/`apt` fetch packages. |

Rough starting sizes (estimates, not guaranteed minimums):

| Use | Suggested start |
|---|---|
| One person, a terminal and a bot or two | 512 MB–1 GB RAM (an Alpine system suits 512 MB) |
| A few users, a few hosted apps | 1 GB RAM, 1 vCPU |
| Several web apps, package builds, compilers | 2 GB+ RAM |
| Several users compiling in parallel | 2–4 GB+ RAM |

A large package install can use several hundred MB at once; Rconsole sets no per-user memory or CPU limit. Each VM costs ~10–150 MB of disk depending on the
system you pick, plus whatever the user installs — and a user can keep several, so
budget disk for roughly *users × systems*.

The container image does the right thing automatically:

* runs **one** gunicorn worker with many threads (`gunicorn.conf.py`);
* starts as root only to make the volume writable, then drops to an unprivileged
  user (set `RCONSOLE_RUN_AS_ROOT=1` to keep root — see
  [root vs. unprivileged](#13-root-vs-unprivileged));
* answers `GET /healthz` for platform health checks;
* detects Render, Railway, Fly.io, Koyeb, Heroku, Cloud Run and Azure and turns on
  proxy trust and secure cookies for them.

---

### Getting the files

Every [GitHub release](../../releases) contains what you need to host rconsole yourself: the complete source
(`rconsole-<version>.tar.gz` / `.zip`, including the Dockerfile and `deploy/` configs), a ready-to-use
`docker-compose.yml` + `Caddyfile` + `env.example` (GitHub drops the leading dot; in the repo it is
`deploy/.env.example`; rename it to `.env`), and `SHA256SUMS` (verify with `sha256sum -c SHA256SUMS`).
A multi-arch container image is published to `ghcr.io/<owner>/<repo>` with each release. Or just clone the repo.

## 2. Choose a platform

> **Provider terms last verified: 5 November 2026** (Koyeb and Railway checked against their own published pages;
> others are described from their documented behaviour). Free tiers, prices and signup rules change often: check the
> provider's current page before you commit. **"Works" means Rconsole runs there, not that it is free.**

| Platform | Works? | Free for new users? | Persistent disk | Effort | Notes |
|----------|:------:|---------------------|-----------------|:------:|-------|
| [**Render**](#4-render) | ✅ | Free plan (no disk; 512 MB, restarts, idle suspension) | Paid plans only (disk) | ⭐ | One-click blueprint (`render.yaml`). The free plan has no disk: it restores from [backups](docs/backup.md) after every restart and suspends when idle. |
| [**Railway**](#5-railway) | ✅ | One-time trial credit, then a small monthly free credit; always-on use needs a paid plan | Volumes | ⭐ | Auto-detects the Dockerfile and the volume path. |
| [**Fly.io**](#6-flyio) | ✅ | No: machines and volumes are billed | Volumes (one machine) | ⭐⭐ | Real microVMs; good global regions (e.g. Singapore). |
| [**Koyeb / Northflank**](#7-koyeb-and-northflank) | ✅ | **Koyeb: no** for new accounts (paid plans only). Northflank: check its current terms | Volumes (paid) | ⭐⭐ | Docker deploy from GitHub. |
| [**Any VPS** (Hetzner, DigitalOcean Droplet, Vultr, Linode, OVH, Oracle Cloud)](#8-any-vps-docker-compose--caddy) | ✅✅ | Paid (Oracle Cloud's Always Free offer exists but capacity and verification vary by region and account) | Local disk | ⭐⭐ | **Best value and most control.** Docker Compose + Caddy gives automatic HTTPS. |
| [**Bare metal / VM without Docker**](#9-linux-server-without-docker-systemd) | ✅✅ | Your own hardware | Local disk | ⭐⭐⭐ | `uv` + systemd + nginx. |
| [**AWS**](#10-aws-google-cloud-azure) | ✅ | No (trial credits may apply) | EBS / EFS | ⭐⭐⭐ | Use **Lightsail/EC2** (simplest) or ECS+EFS. App Runner has no volumes. |
| [**Google Cloud**](#10-aws-google-cloud-azure) | ⚠️ | No (trial credits may apply) | Compute Engine disk | ⭐⭐⭐ | Use a **Compute Engine VM**. Cloud Run is stateless — demo only. |
| [**Azure**](#10-aws-google-cloud-azure) | ✅ | No (trial credits may apply) | Azure Files / disk | ⭐⭐⭐ | VM, or Container Apps with an Azure Files volume and max 1 replica. |
| [**Kubernetes**](#11-kubernetes) | ✅ | Depends on the cluster | PVC | ⭐⭐⭐ | `deploy/k8s.yaml`: 1 replica, `Recreate` strategy. |
| [**Home server / Raspberry Pi / Termux**](#12-home-server-raspberry-pi-or-phone) | ✅ | Your own hardware | Local disk | ⭐⭐ | Reach it privately with Tailscale or a Cloudflare Tunnel. |
| **Heroku** | ⚠️ | No | ❌ none | – | Ephemeral filesystem: everything resets at least daily. Not recommended. |
| **PythonAnywhere, Vercel, Netlify, Cloudflare Pages/Workers, GitHub Pages** | ❌ | – | – | No long-running processes/websockets. They cannot run rconsole. |

**Not sure? Use a VPS** (section 8). Everything works exactly as designed there,
including the VM, and it is usually the cheapest way to get a persistent disk.

---

## 3. Settings you will touch

Full list: [README → Configuration](README.md#configuration). The ones that matter
for deployment:

| Variable | Set it to |
|----------|-----------|
| `RCONSOLE_ADMIN_PASSWORD` | A strong password for the first `ark` login. **Required in practice** — otherwise the login is `ark`/`ark` until you change it. |
| `RCONSOLE_DATA_DIR` | The mount path of your persistent volume (e.g. `/data`). |
| `RCONSOLE_ALLOWED_ORIGINS` | Your public hostname(s), comma separated, if you see `cross-origin request blocked`. Auto-detected on most platforms. |
| `RCONSOLE_TRUST_PROXY` / `RCONSOLE_COOKIE_SECURE` | `1` behind your own proxy (Caddy, nginx). On by default on detected platforms. |
| `RCONSOLE_RUN_AS_ROOT` | `1` to keep root inside the container (see section 13). |
| `RCONSOLE_SECRET`, `RCONSOLE_PEPPER` | Optional. If unset they are generated once and stored (`0600`) in the data dir. |
| `PORT` | Set automatically by most platforms; defaults to `5000`. |

---

## 4. Render

**Best for:** the fastest start. The **free plan runs Rconsole** (512 MB RAM, a small CPU share, roughly 750
instance-hours a month, no disk, idle suspension and provider-driven restarts; keep-alive and backups are built in,
see [FREE_HOSTING.md](FREE_HOSTING.md)) but it is for experiments and light workloads, not a substitute for a VPS; a **paid instance** adds a persistent disk. [`render.yaml`](render.yaml) is the
free blueprint, [`render.paid.yaml`](render.paid.yaml) the one with a disk.

1. Push the repo to GitHub.
2. Render dashboard → **New → Blueprint** → pick the repo. Render reads
   [`render.yaml`](render.yaml): a Docker web service, a health check on
   `/healthz`, generated `RCONSOLE_SECRET`/`RCONSOLE_PEPPER` and **no disk**: `/data` is ephemeral and recovery relies on your
   backup repository. (The 5 GB disk at `/data` belongs to [`render.paid.yaml`](render.paid.yaml), the paid blueprint.)
3. When prompted, enter **`RCONSOLE_ADMIN_PASSWORD`**.
4. Wait for the deploy to go **Live**, open the `*.onrender.com` URL, log in as `ark`.
5. Run `vm list`, then `vm install <name>`.

**Prefer clicking through manually?** New → *Web Service* → *Docker* runtime, then:

* **Free:** instance type *Free*, no disk. Set `RCONSOLE_DATA_DIR=/data`, `RCONSOLE_ADMIN_PASSWORD`,
  `RCONSOLE_BACKUP_URL`, `RCONSOLE_BACKUP_TOKEN`, `RCONSOLE_BACKUP_PASSPHRASE`, and a long random `RCONSOLE_SECRET` and
  `RCONSOLE_PEPPER` (the blueprint generates these for you). Health Check Path `/healthz`. Then check recovery with
  [the backup test](docs/backup.md#test-it-once).
* **Paid:** a paid instance type, *Advanced → Add Disk* with **mount path `/data`**, the variables from
  [section 3](#3-settings-you-will-touch), Health Check Path `/healthz`.

**Using the native Python runtime instead of Docker?** It works, with limits (no
`apt` on the host shell; the VM still works because proot is downloaded at
runtime):

* Build command: `pip install -r requirements.txt`
* Start command: `gunicorn -c gunicorn.conf.py app:app`
* Environment: `PYTHON_VERSION` = a 3.14 release, plus the variables above.

**Custom domain:** *Settings → Custom Domains*, add the CNAME at your DNS host.
The new hostname is picked up from `X-Forwarded-Host`; if you still get a 403 after
login, add it to `RCONSOLE_ALLOWED_ORIGINS`.

**Free plan:** works for trying it out, but there is no disk (everything resets on
each deploy) and the service sleeps when idle, which also ends terminals.

---

## 5. Railway

Railway bills by usage. New accounts get a one-time trial credit, after which only a small monthly credit is free;
an always-on service with a volume generally needs a paid plan. Check [Railway's pricing](https://docs.railway.com/pricing/plans) first.

1. **New Project → Deploy from GitHub repo.** Railway detects the `Dockerfile`.
2. **Add a Volume** (service → *Settings → Volumes* or the command palette) and
   mount it at **`/data`**. rconsole also auto-detects Railway's volume path if you
   forget `RCONSOLE_DATA_DIR`.
3. **Variables:** `RCONSOLE_ADMIN_PASSWORD`, `RCONSOLE_DATA_DIR=/data`.
4. **Settings → Networking → Generate Domain** (or add your own).
5. Keep **replicas at 1**. Open the URL, log in, `vm install`.

The container fixes the volume's ownership itself, so you do not need
`RAILWAY_RUN_UID`.

---

## 6. Fly.io

Fly.io bills for machine time and for volumes, so this is **not** a permanent free deployment. Check
[Fly.io's pricing](https://fly.io/docs/about/pricing/) before you create the machine and volume.

Fly runs your image in a real microVM close to your users (try `sin` for South
Asia). Install `flyctl`, then from the repo root:

```bash
cp deploy/fly.toml fly.toml                 # edit `app = "..."` to a unique name
fly apps create <your-app-name>
fly volumes create rconsole_data --size 3 --region sin
fly secrets set RCONSOLE_ADMIN_PASSWORD='a-long-password'
fly deploy
fly scale count 1                           # volumes are per-machine: keep one
fly open
```

`auto_stop_machines = "off"` is deliberate: a stopped machine would kill open
terminals. The `*.fly.dev` hostname is trusted automatically; add custom domains
with `fly certs add console.example.com` and `RCONSOLE_ALLOWED_ORIGINS`.

---

## 7. Koyeb and Northflank

Both build straight from your GitHub repo's `Dockerfile`.

> **Koyeb:** since February 2026 Koyeb is part of Mistral AI and **new organisations must subscribe to a paid plan**
> (Pro or above); the free Starter plan only continues for organisations that already had it ([announcement](https://www.koyeb.com/blog/koyeb-is-joining-mistral-ai-to-build-the-future-of-ai-infrastructure)).
> Do not rely on Koyeb as a free option. Northflank: confirm that your plan includes a volume and long-running
> websocket connections before you commit.

* **Koyeb:** *Create Service → GitHub →* builder **Dockerfile**, port **5000**
  (HTTP), health check **`/healthz`**, add a **Volume** mounted at `/data` (paid
  instance types), instances **min = max = 1**, and the environment variables above.
* **Northflank:** *Create service → Combined service* from the repo with the
  Dockerfile build, add a **volume** at `/data`, port **5000** public HTTP,
  replicas **1**.

If a platform's free tier has no volume, Rconsole still runs, but its disk is wiped on every redeploy or restart
unless you configure [backups](docs/backup.md): then it restores itself (minutes of downtime, and anything since the
last backup is lost).

---

## 8. Any VPS: Docker Compose + Caddy

The recommended production setup. Works on **Hetzner, DigitalOcean Droplets,
Vultr, Linode/Akamai, OVH, Oracle Cloud (Always Free ARM works), AWS Lightsail,
a Contabo box** — anything with Docker.

1. **Create a server** (Ubuntu 24.04, 1 vCPU/1 GB is enough to start) and point a
   DNS **A record** (`console.example.com`) at its IP.
2. **Install Docker** (official script):
   ```bash
   curl -fsSL https://get.docker.com | sh   # needs Docker 24+ with the Compose plugin; x86-64 or arm64
   ```
3. **Get the code and configure:**
   ```bash
   git clone https://github.com/<you>/rconsole-ark.git && cd rconsole-ark/deploy
   cp .env.example .env && nano .env      # set RCONSOLE_DOMAIN and RCONSOLE_ADMIN_PASSWORD
   ```
4. **Open ports 80 and 443** in the provider's firewall (and `ufw allow 80,443/tcp`).
5. **Start it:**
   ```bash
   docker compose up -d --build
   docker compose logs -f        # Ctrl+C to leave
   ```
6. Open `https://console.example.com`. Caddy gets the certificate on its own.

Everything persists in the `rconsole-data` Docker volume. Update with
`git pull && docker compose up -d --build`, or from inside rconsole with `rconsole update` (admins; locks the console,
verifies and tests the release, keeps your data, restarts in place). The volume keeps the downloaded release, so the
update survives container re-creation; a changed `Dockerfile` still needs the rebuild.

**Prefer nginx over Caddy?** Use [`deploy/nginx.conf`](deploy/nginx.conf) and get
a certificate with `certbot --nginx`; it already contains the websocket headers
and the long timeouts.

---

## 9. Linux server without Docker (systemd)

For a plain Ubuntu/Debian machine where you want to run it directly. The admin's
*host* shell is then the server's real shell, so only do this on a machine you
dedicate to rconsole.

```bash
sudo apt update && sudo apt install -y git curl proot nginx certbot python3-certbot-nginx
curl -LsSf https://astral.sh/uv/install.sh | sh            # installs uv

sudo useradd --system --create-home --home-dir /opt/rconsole rconsole
sudo -u rconsole git clone https://github.com/<you>/rconsole-ark /opt/rconsole/app
cd /opt/rconsole/app && sudo -u rconsole /root/.local/bin/uv sync --frozen   # adjust uv's path

sudo cp deploy/rconsole.env.example /etc/rconsole.env && sudo chmod 600 /etc/rconsole.env
sudo nano /etc/rconsole.env                                # password + your domain
sudo mkdir -p /var/lib/rconsole && sudo chown rconsole /var/lib/rconsole

sudo cp deploy/rconsole.service /etc/systemd/system/
sudo systemctl enable --now rconsole
sudo systemctl status rconsole
```

Then proxy it: copy [`deploy/nginx.conf`](deploy/nginx.conf) to
`/etc/nginx/conf.d/rconsole.conf`, change the hostname, run
`sudo certbot --nginx -d console.example.com`, and `sudo nginx -t && sudo systemctl reload nginx`.

---

## 10. AWS, Google Cloud, Azure

The big clouds all work, but for a single stateful app the **plain VM route is the
simplest** — then follow [section 8](#8-any-vps-docker-compose--caddy) on it.

### AWS
* **Easiest:** *Lightsail* instance (Ubuntu) or *EC2* `t3.small`/`t4g.small` →
  Docker Compose + Caddy (section 8). Open ports 80/443 in the security group and
  attach an Elastic IP.
* **Containers:** *ECS on Fargate* works with **EFS** mounted at `/data`, desired
  count **1**, an ALB with **idle timeout raised to 3600 s** and a `/healthz`
  health check. (*App Runner* has no persistent volumes — don't use it.)

### Google Cloud
* **Use a Compute Engine VM** (e2-small or larger) → section 8.
* **Cloud Run** is stateless and scales to zero, which breaks terminals and loses
  the data dir. If you only want a demo: `--min-instances=1 --max-instances=1
  --no-cpu-throttling --timeout=3600`, understanding the VM and users reset on every
  revision.

### Azure
* **Use a VM** → section 8, or **Container Apps** with an *Azure Files* volume at
  `/data`, **min and max replicas = 1**, ingress target port **5000**, and the
  environment variables from section 3.

On any of them, `RCONSOLE_TRUST_PROXY=1` and `RCONSOLE_COOKIE_SECURE=1` are set
automatically when the platform is recognised; otherwise set them yourself.

---

## 11. Kubernetes

Apply [`deploy/k8s.yaml`](deploy/k8s.yaml): a PVC, a **single-replica** Deployment
with the `Recreate` strategy (the volume attaches to one pod), a Service, and an
Ingress with websocket-friendly timeouts for ingress-nginx.

```bash
docker build -t ghcr.io/<you>/rconsole:latest . && docker push ghcr.io/<you>/rconsole:latest
kubectl create secret generic rconsole --from-literal=admin-password='a-long-password'
# edit image:, the host, and the TLS secret in deploy/k8s.yaml, then:
kubectl apply -f deploy/k8s.yaml
```

Never set `replicas` above 1, and do not use an autoscaler.

---

## 12. Home server, Raspberry Pi or phone

* **Raspberry Pi / home PC (arm64 or x86-64):** run it with Docker Compose
  (section 8, skip Caddy) or `uv run python serve.py` (gunicorn on Linux; a built-in fallback on Windows). On ARM,
  install **proot 5.5.0 or newer** yourself, since the prebuilt download is x86-64 only. The package manager's
  version (`sudo apt install proot`) may be older and then cannot run Ubuntu 26.04's Rust coreutils: check
  `proot --version`, and point `RCONSOLE_VM_PROOT` at a newer build if needed. The Docker image includes the
  distribution's proot (also check its version).
* **Reach it from outside without opening ports:**
  * **Tailscale:** install on the server and your devices, then browse to
    `http://<machine-name>:5000`. Private by default — the safest choice for a
    home server.
  * **Cloudflare Tunnel:** `cloudflared tunnel --url http://localhost:5000` for a
    quick public HTTPS URL, or a named tunnel for your own domain. Add the public
    hostname to `RCONSOLE_ALLOWED_ORIGINS` and set `RCONSOLE_COOKIE_SECURE=1`.
* **Android (Termux, untested):** `pkg install python git proot`, clone the repo, `pip install -r requirements.txt`,
  run `python main.py`. Rconsole needs **Python 3.14+**: check `python --version` first. Android may kill background
  processes, which stops service supervision. This is for local use. Use Tailscale or a
  tunnel to open it from other devices.

---

## 13. Root vs. unprivileged

By default the container runs rconsole as an unprivileged user (uid 10001):

* admins' **host shell** is that user — it cannot `apt install` on the host;
* **everyone's VM** (`vm install`) still gives a root shell with `apk`/`apt`, which
  covers nearly every need and works on locked-down platforms like Render.

If you are the **only** user and want real root on the container's own shell too,
set **`RCONSOLE_RUN_AS_ROOT=1`**. Know the trade-off: the server process is then
root, so every account with admin rights is root in that container, and VM guests
(not a hard boundary, see [README → Security model](README.md#security-model))
would run with real root privileges. Fine for a private box; do not combine it with
accounts you don't trust. The container itself remains your isolation boundary.

---

## 14. After you deploy

Do this once, in order:

1. **Log in** as `ark` with `RCONSOLE_ADMIN_PASSWORD`. If you did not set one,
   you are forced to run `sudo cpass` first.
2. **Create real accounts** (`sudo muser`) instead of sharing `ark`.
3. **Check the data survives:** run `vm list`, `vm install debian` (or any),
   create a file in `/root`, then trigger a redeploy/restart and confirm the VM and
   file are still there. On a volume, if not, the volume or `RCONSOLE_DATA_DIR` is wrong. **On a disk-less host**
   this only proves the *restore* works: also run `sudo backup` and follow [the backup test](docs/backup.md#test-it-once)
   (encrypted snapshot arrives, passphrase decrypts it, a clean instance restores it, services start).
4. **Test the terminal:** `vm shell` should show a prompt and accept typing. If it
   connects then drops after a minute, your proxy's idle timeout is too short
   (see troubleshooting).
5. **Restrict access** if it should not be world-reachable: put it behind
   Tailscale, Cloudflare Access, or an IP allow-list at your proxy.
6. **Back up** `/data` (see below).

### Updating

* PaaS with auto-deploy: push to `main`.
* Docker Compose: `git pull && docker compose up -d --build`.
* systemd: `git pull && uv sync --frozen && sudo systemctl restart rconsole`.

Open terminal tabs come back (same folder, history kept) as **fresh terminals**: the programs that ran in them,
background jobs and hand-started apps are stopped and must be started again. `service`s start again by themselves.

### Backups and restore

Everything lives in the data directory (users, secrets, state, VMs).

```bash
# Docker volume -> tarball
docker run --rm -v rconsole-ark_rconsole-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/rconsole-$(date +%F).tar.gz -C /data .

# Restore into an empty volume
docker run --rm -v rconsole-ark_rconsole-data:/data -v "$PWD":/backup alpine \
  tar xzf /backup/rconsole-YYYY-MM-DD.tar.gz -C /data
```

(The volume name is `<folder>_rconsole-data`; check with `docker volume ls`.) On
PaaS, use the provider's disk snapshot feature. The `data/vm/<user>/home` folders
hold users' files; the `rootfs` folders can be recreated with `vm install`.

---

## 15. Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `{"error":"cross-origin request blocked"}` | The proxy rewrites `Host`. Check the server log line starting `blocked cross-origin` for the host, then set `RCONSOLE_ALLOWED_ORIGINS=<that host>`. Make sure your proxy sends `X-Forwarded-Host`. |
| Login works but you are logged out immediately | `RCONSOLE_COOKIE_SECURE=1` while you browse over plain HTTP. Use HTTPS, or unset it for local testing only. |
| Logged out after every deploy | No persistent volume, or `RCONSOLE_DATA_DIR` not on it. The generated secrets live in the data dir. |
| Terminal connects, then drops after ~60 s | Proxy idle timeout. nginx: `proxy_read_timeout 3600s`. AWS ALB: raise idle timeout. Cloudflare: use the proxy's websocket support and avoid free-plan idle limits via a tunnel. |
| `vm install` fails with "could not reach …" | No outbound internet, or the mirror is blocked. Set `RCONSOLE_VM_ROOTFS_URL` to a reachable tarball (and `RCONSOLE_VM_ROOTFS_SHA256`). |
| `vm install` fails: "proot cannot run" | The platform blocks `ptrace`. Use a VPS/microVM (Fly.io, any VM) instead. |
| `vm update` says it needs free disk, or rolled back | An update needs ~200 MB free on the data disk (the snapshot is hard links, so it is small) and network access to the distro mirror. A rollback leaves the VM exactly as before; `vm update <name> --log` shows why. A restart in the middle is rolled back automatically at the next start. |
| `vm doctor` says "pid ns: NOT isolated" | The host forbids user namespaces (plain Docker's seccomp profile does). VMs still work but share the server's PID space: add `--security-opt seccomp=unconfined` (compose: `security_opt: [seccomp=unconfined]`) if you want isolation, or only create accounts for people you trust. `vm ps` / `vm kill` give a scoped view meanwhile. |
| After a redeploy a service is "waiting" | Its VM is `restoring`: the system, `vm setup` profiles and the packages recorded in `deps.json` are reinstalled first (`vm status` shows the step, `vm deps` the last result). Services start by themselves afterwards. Put your project under `/root`: files elsewhere in the VM are not backed up. |
| A VM misbehaves and you don't know why | Run **`vm doctor <name>`**: it checks shell, programs, root, write access, DNS, internet and the package manager, and prints which step fails. Paste its output when asking for help. |
| `coreutils: unknown program 'prooted-…'` | A proot older than 5.5 under Ubuntu 26.04. rconsole downloads 5.5.0 automatically on x86-64 — make sure the server can reach github.com; on ARM install a newer proot. |
| `vm install` says unsupported CPU | Prebuilt proot is x86-64. On ARM install it: `apt install proot` / `apk add proot`. The Docker image has it. |
| `apt update` inside a Debian/Ubuntu/Kali VM prints sandbox warnings | Harmless; rconsole already sets `APT::Sandbox::User "root"`. |
| 502 from a hosted app | The app crashed on start: `service logs <name>` shows its output. In VM mode install Python and gunicorn in the VM (`vm setup python`, then `pip install gunicorn`). |
| `Permission denied` writing `/data` | Only if you bypassed the entrypoint. Run the container as root once, or `chown -R 10001 /data`. |
| Two people see different state | More than one instance is running. Scale to **1**. |
| Container killed / out of memory | Raise the instance memory; Kali and big `apt install`s need a few hundred MB. |

---

## 16. Security hardening checklist

* Strong `RCONSOLE_ADMIN_PASSWORD`; create individual accounts; remove unused ones.
* Serve **only over HTTPS**; keep `RCONSOLE_COOKIE_SECURE=1`.
* Limit who can reach the login page (Tailscale, Cloudflare Access, IP allow-list)
  if it is not meant for the public.
* Remember **admins are root-equivalent on the container** and the VM is a
  convenience sandbox, **not** a hard boundary — only create accounts for people you
  trust.
* Keep rconsole updated (`rconsole update`, or `git pull` + redeploy) and enable Dependabot (already in
  `.github/dependabot.yml`).
* Back up `/data`, and never commit `data/` or `.env` files.

Everything about how rconsole decides what a user may do is in
[README → Security model](README.md#security-model).
