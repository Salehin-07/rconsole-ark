# Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| The dashboard warns that the factory password is in use | Change it under Dashboard → Account, or run `sudo cpass`. |
| `host access is restricted to admins` | Non-admins use the VM: `vm install`, then `vm shell`. |
| `vm install` fails: "proot cannot run" | Either the platform blocks `ptrace` (Rconsole already retries without seccomp acceleration — use a VPS then), or the proot is too old (`unknown program 'prooted-…'`): Rconsole fetches 5.5.0 on x86-64; elsewhere install a newer one. |
| `bash: cannot set terminal process group … no job control` | Fixed in current versions (the terminal now gets a controlling terminal). Update. |
| Kali prints "mkdir: cannot create directory '/dev/net'" or a "minimal installation" banner | Fixed: every VM gets a private writable `/dev/net` and the banner is hidden by `~/.hushlogin`. Update Rconsole; new VMs get it at install, existing ones on the next launch (the banner file: `touch ~/.hushlogin`). |
| `apt install` stops at "Please select the geographic area" | Fixed: VMs never ask questions (`DEBIAN_FRONTEND=noninteractive`). Update Rconsole. |
| `pip install psycopg2` fails to build ("pg_config executable not found") | Run `vm setup python` first (installs the compiler and `libpq-dev`/`postgresql-dev`). |
| A VM command hangs or "timed out" | `apt`/builds can exceed the 60–90 s limit for plain commands: use `vm shell` or add `&`. `vm doctor` shows if DNS/network is the cause. |
| "Choose a system" dropdown keeps appearing | You have several VMs and no default: pick one (it is remembered), or `vm use <name>`. |
| `vm install` fails to download | Server has no outbound HTTPS, or the mirror is blocked. Set `RCONSOLE_VM_ROOTFS_URL` to a reachable tarball. |
| `apt: not found` / `apk: not found` in the VM | Different systems, different tools: Alpine uses `apk add`, Debian/Ubuntu/Kali use `apt install`. `vm status` shows which one you have. |
| `vm install debian` says "already installed" | Switch with `vm reset --yes` (keeps `/root`), then `vm install debian`. |
| Logged out after every deploy | No persistent disk / `RCONSOLE_DATA_DIR`; the generated secrets were lost. Mount a disk, configure backups, or set `RCONSOLE_SECRET` and `RCONSOLE_PEPPER`. |
| `{"error":"cross-origin request blocked"}` after login | Your proxy rewrites `Host` without `X-Forwarded-Host`. The server log names the blocked host — add it to `RCONSOLE_ALLOWED_ORIGINS`. |
| "Too many failed attempts" | Login lockout; wait the stated seconds. |
| Hosted app exits immediately | The error shows the app's log; in VM mode install `python3`/`gunicorn` in the VM (`vm setup python`). `service logs <name>` shows its output. |
| Instance falls asleep / bots stop after a while | `keepalive` — is there a public URL and is it enabled? See [FREE_HOSTING.md](../FREE_HOSTING.md#stay-awake). |
| Everything is gone after a redeploy | Backups weren't configured (`sudo backup` shows what's missing) — see [FREE_HOSTING.md](../FREE_HOSTING.md). |
| `uv: command not found` (or `cargo`, `bun`, …) right after installing it in a VM | Fixed: plain commands now have `~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, `~/go/bin` on `PATH` and read `~/.profile`. Update Rconsole. (Interactive `vm shell` always worked because the installer's `source` line ran there.) |
| `expose` link gives 502 | Nothing is listening on that port yet (`expose list` shows `[down]`) — start your app. |
| Hosted app not reachable | Is it running (`service list`) and exposed (`expose list`)? Open `/<name>/`. |
| `rconsole update` says "this is a git checkout" | Update that install with `git pull` (or `git checkout vX.Y.Z`); `--force` overwrites its files from the release archive. |
| `rconsole update`: "GitHub refused the request" | Anonymous API limit or a private repo: set `RCONSOLE_UPDATE_TOKEN` (and `RCONSOLE_UPDATE_REPO` for a fork). |
| After a redeploy the version went back | The image was rebuilt from older code and no saved release could be re-applied (no persistent disk or backups): redeploy from the new release, or run `rconsole update` again. |
| Changes not taking effect | No auto-reload; restart the server. Hard-refresh for static assets. |
