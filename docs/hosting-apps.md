# Hosting web apps

Two ways, both behind Rconsole's login (unless you use `expose --public`):

**`expose` — you start the program, Rconsole publishes it.** Works for anything that serves HTTP, on the host
or inside any VM:

```text
$ python main.py &              # or: flask run -p 8000, npm run dev, uvicorn app:app --port 8000, …
$ expose 8000 myapp             # -> https://<your-domain>/myapp/
$ expose list                   # [up]/[down] status
$ expose rm myapp
```
Apps that don't know they live under a path prefix just work: root-relative links in HTML/CSS (`/static/x.css`) are
fixed automatically, no flag needed (`--no-rewrite` turns that off for prefix-aware apps). Admins can make an app open to everyone with `--public` — on `expose`, or on `service add … --expose PORT:NAME --public`. The proxy sends `X-Forwarded-Prefix`, so prefix-aware frameworks work natively. Details and
limits (no WebSockets) are in [COMMANDS.md](../COMMANDS.md#expose).

**`service` — Rconsole starts and keeps the app running** (this replaces `sudo serve` / `sudo gunicorn`,
removed in 1.2.2):

```text
$ service add site --vm debian --cwd /root/site --expose 8000:site python3 -m http.server 8000
$ service add app  --vm debian --cwd /root/app  --expose 8000:app  gunicorn -b 127.0.0.1:8000 app:app
$ service logs app
```

- Apps bind `127.0.0.1:<port>` and are reachable only through the proxy at
  `https://<your-domain>/<name>/…`, and only by logged-in users.
- The proxy never forwards Rconsole's own session cookie to your app, and
  passes multiple `Set-Cookie` headers through intact (Django's `csrftoken` +
  `sessionid` work).
