# Deploying rconsole

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
| 2 | **A persistent volume** mounted somewhere, with `RCONSOLE_DATA_DIR` pointing at it | Users, passwords, secrets, saved tabs and **every user's VM** live there. Without it, each deploy resets everything. |
| 3 | **Websockets + long idle timeouts** | The browser terminal is a websocket. Proxies that cut idle connections after 30–60 s will drop terminals. |
| 4 | **HTTPS in front** | Login cookies are `Secure`; never expose the console over plain HTTP on the internet. |
| 5 | **Outbound internet** | `vm install` downloads proot (GitHub) and the system image (Alpine/Debian/Ubuntu/Kali mirrors), and `apk`/`apt` fetch packages. |

Recommended size: **1 vCPU / 1 GB RAM** for a few users; add RAM if people build
software or run hosted apps. Each VM costs ~10–150 MB of disk depending on the
system you pick, plus whatever the user installs.

The container image does the right thing automatically:

* runs **one** gunicorn worker with many threads (`gunicorn.conf.py`);
* starts as root only to make the volume writable, then drops to an unprivileged
  user (set `RCONSOLE_RUN_AS_ROOT=1` to keep root — see
  [root vs. unprivileged](#13-root-vs-unprivileged));
* answers `GET /healthz` for platform health checks;
* detects Render, Railway, Fly.io, Koyeb, Heroku, Cloud Run and Azure and turns on
  proxy trust and secure cookies for them.

---

## 2. Choose a platform

| Platform | Works? | Persistent disk | Effort | Notes |
|----------|:------:|-----------------|:------:|-------|
| [**Render**](#4-render) | ✅ | Paid plans only (disk) | ⭐ | One-click blueprint (`render.yaml`). Free plan resets on every deploy and sleeps when idle. |
| [**Railway**](#5-railway) | ✅ | Volumes | ⭐ | Auto-detects the Dockerfile and the volume path. |
| [**Fly.io**](#6-flyio) | ✅ | Volumes (one machine) | ⭐⭐ | Real microVMs; good global regions (e.g. Singapore). |
| [**Koyeb / Northflank**](#7-koyeb-and-northflank) | ✅ | Volumes (paid) | ⭐⭐ | Docker deploy from GitHub. |
| [**Any VPS** (Hetzner, DigitalOcean Droplet, Vultr, Linode, OVH, Oracle Cloud)](#8-any-vps-docker-compose--caddy) | ✅✅ | Local disk | ⭐⭐ | **Best value and most control.** Docker Compose + Caddy gives automatic HTTPS. |
| [**Bare metal / VM without Docker**](#9-linux-server-without-docker-systemd) | ✅✅ | Local disk | ⭐⭐⭐ | `uv` + systemd + nginx. |
| [**AWS**](#10-aws-google-cloud-azure) | ✅ | EBS / EFS | ⭐⭐⭐ | Use **Lightsail/EC2** (simplest) or ECS+EFS. App Runner has no volumes. |
| [**Google Cloud**](#10-aws-google-cloud-azure) | ⚠️ | Compute Engine disk | ⭐⭐⭐ | Use a **Compute Engine VM**. Cloud Run is stateless — demo only. |
| [**Azure**](#10-aws-google-cloud-azure) | ✅ | Azure Files / disk | ⭐⭐⭐ | VM, or Container Apps with an Azure Files volume and max 1 replica. |
| [**Kubernetes**](#11-kubernetes) | ✅ | PVC | ⭐⭐⭐ | `deploy/k8s.yaml`: 1 replica, `Recreate` strategy. |
| [**Home server / Raspberry Pi / Termux**](#12-home-server-raspberry-pi-or-phone) | ✅ | Local disk | ⭐⭐ | Reach it privately with Tailscale or a Cloudflare Tunnel. |
| **Heroku** | ⚠️ | ❌ none | – | Ephemeral filesystem: everything resets at least daily. Not recommended. |
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

**Best for:** the fastest start. **Needs a paid instance** for a persistent disk.

1. Push the repo to GitHub.
2. Render dashboard → **New → Blueprint** → pick the repo. Render reads
   [`render.yaml`](render.yaml): a Docker web service, a health check on
   `/healthz`, generated `RCONSOLE_SECRET`/`RCONSOLE_PEPPER`, and a 5 GB disk at `/data`.
3. When prompted, enter **`RCONSOLE_ADMIN_PASSWORD`**.
4. Wait for the deploy to go **Live**, open the `*.onrender.com` URL, log in as `ark`.
5. Run `vm list`, then `vm install <name>`.

**Prefer clicking through manually?** New → *Web Service* → *Docker* runtime →
*Instance type* a paid one → *Advanced → Add Disk* with **mount path `/data`** →
add the environment variables from [section 3](#3-settings-you-will-touch) and set
**Health Check Path** to `/healthz`.

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

* **Koyeb:** *Create Service → GitHub →* builder **Dockerfile**, port **5000**
  (HTTP), health check **`/healthz`**, add a **Volume** mounted at `/data` (paid
  instance types), instances **min = max = 1**, and the environment variables above.
* **Northflank:** *Create service → Combined service* from the repo with the
  Dockerfile build, add a **volume** at `/data`, port **5000** public HTTP,
  replicas **1**.

If a platform's free tier has no volume, rconsole still runs but forgets everything
on redeploy.

---

## 8. Any VPS: Docker Compose + Caddy

The recommended production setup. Works on **Hetzner, DigitalOcean Droplets,
Vultr, Linode/Akamai, OVH, Oracle Cloud (Always Free ARM works), AWS Lightsail,
a Contabo box** — anything with Docker.

1. **Create a server** (Ubuntu 24.04, 1 vCPU/1 GB is enough to start) and point a
   DNS **A record** (`console.example.com`) at its IP.
2. **Install Docker** (official script):
   ```bash
   curl -fsSL https://get.docker.com | sh
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
`git pull && docker compose up -d --build`.

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
  (section 8, skip Caddy) or `uv run gunicorn -c gunicorn.conf.py app:app`. On ARM,
  install proot from the package manager (`sudo apt install proot`), since the
  prebuilt download is x86-64 only; the Docker image already includes it.
* **Reach it from outside without opening ports:**
  * **Tailscale:** install on the server and your devices, then browse to
    `http://<machine-name>:5000`. Private by default — the safest choice for a
    home server.
  * **Cloudflare Tunnel:** `cloudflared tunnel --url http://localhost:5000` for a
    quick public HTTPS URL, or a named tunnel for your own domain. Add the public
    hostname to `RCONSOLE_ALLOWED_ORIGINS` and set `RCONSOLE_COOKIE_SECURE=1`.
* **Android (Termux):** `pkg install python git proot`, clone the repo,
  `pip install -r requirements.txt`, run `python main.py`. Use Tailscale or a
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
   file are still there. If not, the volume or `RCONSOLE_DATA_DIR` is wrong.
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

Open terminals reconnect after a restart; background jobs and hosted apps are
stopped and must be started again.

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
| `vm install` says unsupported CPU | Prebuilt proot is x86-64. On ARM install it: `apt install proot` / `apk add proot`. The Docker image has it. |
| `apt update` inside a Debian/Ubuntu/Kali VM prints sandbox warnings | Harmless; rconsole already sets `APT::Sandbox::User "root"`. |
| 502 from a hosted app | The app crashed on start: `sudo sprocess <port>` shows its log. In VM mode install Python and gunicorn in the VM (`vm run …`, the error message prints the exact command). |
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
* Keep the image updated (`git pull` + redeploy) and enable Dependabot (already in
  `.github/dependabot.yml`).
* Back up `/data`, and never commit `data/` or `.env` files.

Everything about how rconsole decides what a user may do is in
[README → Security model](README.md#security-model).
