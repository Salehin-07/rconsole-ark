# Free hosting with rconsole

rconsole exists for one reason: **to give a person with no budget a real, always-on server they can run from
their phone.**

Free hosting tiers will happily serve a small website. What they never give away is the part you actually need
for a **bot**, a **scraper**, a **queue worker** or an **API with a background job**: a machine where you can run
*any process, all the time, with a shell*. rconsole turns **one free web service** (Render's free tier is the
reference) into exactly that — a small Linux box with a terminal, private Debian/Ubuntu/Kali/Alpine systems,
a process supervisor, and a way to put anything you run on the web — and then works around the free tier's
three catches:

| The catch on a free tier | What rconsole does about it |
|---|---|
| **It sleeps after ~15 min without visitors** (and your bot goes with it) | A built-in **keep-alive** pings its own public URL every 10 minutes. No external monitor needed. |
| **No persistent disk — every deploy/restart starts blank** | Encrypted **backups** to a free private git repo, **automatic restore** on boot, VMs and toolchains **rebuilt**, services **restarted**. |
| **Redeploying to fix something wipes the disk anyway** | `sudo reboot` restarts rconsole **in place** (files and VMs stay), no redeploy. |
| **You are on a phone** | A responsive UI you can **install as an app**; pickers and menus built for touch. |

> It is built for **one person or a small team** (default cap: 10 accounts), not for the public. Don't put
> strangers on it.

---

## What you get, honestly

* A Linux **terminal in the browser** with a real shell, editor and file manager.
* **Your own Linux systems** (`vm install debian ubuntu kali alpine`) where you are root and can `apt install`
  anything — no host privileges needed.
* **Hosting for websites** — static (`python3 -m http.server` under `service`) or dynamic (anything you can start: Flask, Django, Node, PHP…)
  published at `https://your-site/<name>/` with `expose`.
* **Bots and workers** that keep running: `service add` starts them at boot, restarts them when they crash.
* **Background jobs**, `&`, logs, `htop`, `git`, `pip`, `npm`, `cargo`…

What a free instance **cannot** give you (so plan around it):

| Limit | Reality |
|---|---|
| **RAM / CPU** | Render free: **512 MB RAM, 0.1 CPU**. A Telegram/Discord bot or a small Flask/Node app is fine; a big build or a headless browser is not. Install **Alpine** (`RCONSOLE_DEFAULT_SYSTEM=alpine`) if you are tight on memory, and avoid Kali's big tool metapackages. |
| **Uptime hours** | Render's free plan gives ~750 instance-hours per month — enough for **one** service running 24/7, but not two. |
| **Disk** | Ephemeral and small. Backups cover **files** (your workspace, each VM's `/root`, settings), **not** running processes or the (re-downloadable) system files. |
| **Restarts** | The platform may restart your instance whenever it likes. After one, rconsole restores your data and restarts your services, but there is a **gap of a few minutes** while Debian is re-downloaded and re-set-up. |
| **Terms of service** | Keep-alive pings are common practice and Render documents the spin-down behaviour, but **read your provider's current terms**. If a provider forbids it, switch it off (`RCONSOLE_KEEPALIVE=0`) and accept the sleep. |
| **Security** | The VM is a compatibility layer, **not** a hard sandbox. This is for people who trust each other. |

If you need rock-solid persistence, an **Oracle Cloud "Always Free" VM** (a real disk and several ARM cores) is a
better home for rconsole than a PaaS free tier — see [DEPLOY.md](DEPLOY.md). The commands and features are the same.

---

## Setup in ~10 minutes (Render free)

### 1. A private repo to hold your backups
1. On GitHub create a **private** repository, e.g. `rconsole-data` (empty is fine).
2. Create a token that can write to **only that repo**: *Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate*. Repository access: only `rconsole-data`. Permissions: **Contents: Read and write**.
   Copy the token.
3. Invent a long **backup passphrase** (e.g. four random words) and **save a copy somewhere else** (a password
   manager). Everything in the backup is encrypted with it — without it, nobody (including you) can read a backup.

### 2. Deploy
1. Fork or use this repository, then in Render: **New → Blueprint** and pick it. Render reads
   [`render.yaml`](render.yaml): a **free** Docker web service with a health check.
2. When prompted, fill in:

| Variable | Value |
|---|---|
| `RCONSOLE_ADMIN_PASSWORD` | your first password for the `ark` admin (8+ characters) |
| `RCONSOLE_BACKUP_URL` | `https://github.com/YOU/rconsole-data.git` |
| `RCONSOLE_BACKUP_TOKEN` | the token from step 1 |
| `RCONSOLE_BACKUP_PASSPHRASE` | your passphrase |

3. Wait for the deploy to go **Live** and open the `https://….onrender.com` address.

### 3. First minutes
```text
login: ark / <your password>
sudo backup            # shows the target, "encrypted: yes" and any problem
sudo backup now        # take the first backup right away
keepalive              # confirms it pings https://….onrender.com/healthz every 10 min
vm install debian      # (or alpine) — your own Linux system, ~1 minute
sudo muser             # add a teammate if you want one
```
You are done: the site stays awake, and a redeploy no longer costs you your work.

---

## Run things

Everything below keeps working across restarts once backups are on. Work in `/root` of a VM (or the workspace on
the host): that is what gets saved.

### A static website
```text
vm shell                               # a root shell in your Debian
mkdir -p /root/site && echo '<h1>Hello</h1>' > /root/site/index.html
exit
```
Then run it as a service and publish it with `expose`:
```text
service add site --vm debian --cwd /root/site --expose 8000:site python3 -m http.server 8000
```
→ `https://your-site.onrender.com/site/`

### A dynamic website (Flask, Django, Node, …)
```text
vm setup python                        # Python + compiler + headers (psycopg2, Pillow, …); live progress
vm shell
  cd /root && git clone https://github.com/you/app && cd app && pip install -r requirements.txt
  exit
service add app --vm debian --cwd /root/app --expose 8000:app gunicorn -b 127.0.0.1:8000 app:app
```
It starts at boot, restarts if it crashes, and lives at `/app/`. For apps that build absolute links (`/static/...`)
add `--rewrite` to a manual `expose 8000 app --rewrite`, or configure the framework's prefix
(`X-Forwarded-Prefix` is sent). See [COMMANDS.md](COMMANDS.md#expose).

### A Telegram or Discord bot
```text
vm setup python
vm shell
  mkdir -p /root/bot && cd /root/bot && pip install python-telegram-bot
  nano bot.py          # your bot
  exit
service add tgbot --vm debian --cwd /root/bot --env BOT_TOKEN=123:abc python3 bot.py
service logs tgbot
```
`service list` shows `● tgbot — running 3h · debian · restarts 0`. If it crashes it comes back by itself
(2 s, 4 s, … up to 1 min). After a redeploy it starts again once your Debian is rebuilt.

> Tokens in `--env` are stored in `services.json`, which is **only ever backed up encrypted**.

### A scheduled/background worker
Any loop works — a service is just a command that must stay up:
```text
service add scraper --vm debian --cwd /root/scraper sh -c 'while true; do python3 scrape.py; sleep 3600; done'
```

---

## How it survives a redeploy

```text
 running         deploy / restart                 new blank instance                 minutes later
 ─────────  ─►  SIGTERM: final backup  ─►  boot: no users.json → restore  ─►  VMs reinstalled + `vm setup`
 services        (≤ 25 s, encrypted,        backup (secrets first!)             re-applied, services restarted,
 + files         pushed to your repo)                                           keep-alive resumes
```
* Backups run **every 15 minutes if something changed**, plus once at shutdown.
* The repo only ever holds **one** snapshot (each push replaces the branch), so it stays small.
* Restore never overwrites files that already exist, and refuses anything outside the expected folders.
* Not saved: system files (re-downloaded), caches, `node_modules`/virtualenvs, files over 20 MB, running processes
  (services bring those back), logs. If a snapshot would exceed 40 MB, `sudo backup now` tells you which folders
  are too big — keep large data elsewhere.

Check it any time with `sudo backup`. **Test your backup once**: deploy a second throwaway service with the same
variables and confirm it restores, or run `sudo backup restore --yes` on a spare instance.

---

## Stay awake

On Render, `RCONSOLE_KEEPALIVE` is on by default and `RENDER_EXTERNAL_URL` is detected, so there is nothing to
configure. Anywhere else set:
```text
RCONSOLE_PUBLIC_URL=https://your-site.example
RCONSOLE_KEEPALIVE=1
```
* The ping goes to the **public URL**, so it counts as a real visitor to the platform's proxy (`localhost` would not).
* Ping other free sites you own too: `RCONSOLE_KEEPALIVE_EXTRA=https://other.onrender.com,https://third.example`.
* `keepalive` shows the last result; `keepalive now` tests it; `sudo keepalive off|on` toggles it until restart.
* Want a second opinion? Point a free uptime monitor (UptimeRobot, cron-job.org, …) at `/healthz` as well.
  That also tells you when the whole thing is down.

---

## Fix things without redeploying

```text
sudo reboot --yes          # restart rconsole in place: final backup, stop everything, reload.
                           # Files and VMs stay; services start again; the page reconnects.
sudo reboot --hard --yes   # exit the process; the platform starts a fresh one (may be a blank disk → restore)
```
Changed an environment variable on the host (e.g. the backup passphrase)? Update it in the dashboard, then
`sudo reboot --yes`. A stuck terminal or job? `service restart <name>`, `kill %n`, or reboot.

---

## Updating to a new release

```text
rconsole update --check    # anyone: is there a newer release?
rconsole update            # admins: check, confirm, update, restart in place
```
The console is locked while it runs (nothing else can be done), the release is checksum-verified and tested before it
replaces anything, and your data is not touched. On the free plan the disk is wiped on every restart, so the verified
release is also part of the **encrypted backup**: after the restore at boot rconsole applies it again and restarts once.
That needs backups to be configured (step 1). A release that changes the `Dockerfile` (system packages) needs a normal
redeploy; the update says so. Prefer the dashboard? Point the service at the new release and redeploy: same result.

---

## Working from your phone

* Open the site → browser menu → **Add to Home screen / Install app**. It opens full-screen like an app.
* On a narrow screen the tab bar only has your tabs, **+**, **📁** and **⚙**; the **theme** button and the
  **system picker** live under ⚙.
* Every interactive window (nano, vim, `vm shell`) has a **✕** in the corner — no Esc key needed.
* `neofetch`, `vm list` and friends reformat to your screen width.
* Use `service logs`, `expose list` and `jobs` to check on things; commands are short on purpose.
* Long installs run as **jobs** (`vm setup …` streams live and survives closing the tab).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `sudo backup` says **PROBLEMS** | Follow the list: usually a missing `RCONSOLE_BACKUP_PASSPHRASE`, a typo in the URL, or a token without **Contents: write**. |
| Backup fails: "authentication failed" | Wrong/expired token, or the token isn't scoped to that repository. |
| After a redeploy you are asked for the default password | Restore didn't run: check the boot log for `[rconsole] restore:` and that `RCONSOLE_BACKUP_*` are set **before** deploying. Run `sudo backup restore --yes`, then `sudo reboot --yes`. |
| "cannot decrypt the backup" | The passphrase differs from the one used to create it. |
| Instance still sleeps | `keepalive` — is there a public URL and is it enabled? Is the service within its monthly hours? |
| Out of memory / instance restarts | 512 MB is tight. Use Alpine, stop unused services (`service stop <name>`), install one thing at a time. |
| Service keeps restarting | `service logs <name>` shows the last output and exit code; fix the command, then `service restart <name>`. |
| Debian rebuild takes a while after a restore | Expected: the system is re-downloaded (~1 min) and `vm setup` profiles are re-applied (a few minutes). Services retry until the VM is ready. |
