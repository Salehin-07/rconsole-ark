# Using the console

## Line-mode commands

| Command | Description |
|---------|-------------|
| `help` | List commands. |
| `clear` / `cls` | Clear the screen. |
| `cd <dir>` | Change directory (stateful; `~` supported; confined to the VM in `vm` mode). |
| `history`, `rwhoami`, `alias`, `unalias`, `theme` | Console helpers. |
| `edit <file>` / `fm [path]` | Built-in editor / file manager (mobile friendly). |
| `service add\|list\|start\|stop\|restart\|logs\|rm` | Supervised bots/workers — see [COMMANDS.md](../COMMANDS.md#service). Everyone. |
| `keepalive` · `sudo backup` · `sudo reboot --yes` | Stay awake, back up/restore, restart in place — see [FREE_HOSTING.md](../FREE_HOSTING.md). |
| `expose <port> <name>` · `expose list` · `expose rm <name>` | Publish a server you started at `/<name>/` — see [Hosting web apps](hosting-apps.md#hosting-web-apps). Everyone. |
| `version` | Print the Rconsole version. |
| `rconsole update [--check\|--yes\|--rollback]` | Check for a new release and update to it (admins) — see [Updating rconsole](#updating-rconsole). |
| `vm …` | Your systems (`vm list`, `vm install`, `vm shell`, `vm use`, `vm doctor`, …) — see above. Available to everyone. |
| `neofetch` / `fetch` / `rfetch` | System summary: your VM's (in `vm` mode) or Rconsole's (`host` mode); `rfetch` is always Rconsole's. Fits your screen width. |
| `<cmd> &`, `jobs`, `jobout %n`, `jobtail %n`, `fg %n`, `kill %n` | Background jobs. |
| `logout` | Log out (anyone). |
| `sudo cpass` / `cuser` | Change a password / rename a user (state and VM move with the name). Every user can also change their own password under Dashboard → Account. |
| `sudo muser` / `ruser` | Create / delete a user (their VM and state are removed too). |
| `sudo suser` / `auser <u>` / `nauser <u>` | List users / grant / revoke admin. |
| `sudo rboot` | Factory reset: kill apps, wipe state and VMs, reset users. |
| `sudo kprocess [pid]` | Kill a process (or everything this session hosts). Refuses to kill the server itself. |
| `sudo env …`, `sudo agent`, `sudo opencode` | Per-user env vars, AI agent (page, sessions, providers), opencode launcher. |

Commands prefixed with `sudo` require an **admin** account; a non-admin who types
`sudo anything` is refused.

## Real commands

Anything that isn't a built-in runs in your current target through a real shell,
so pipes, redirects, `&&`, globbing and quoting behave as in any shell:

```text
$ ls -la | grep py > files.txt && wc -l files.txt
```

Interactive programs (`nano vim less top python bash ssh git log …`) open in a
real PTY window. Close it with the **✕ button in the top-right corner** (works on
phones, where there is no Esc key) or press **Esc**: either one ends the session and
returns you to the console. Other commands have a 60-second timeout —
append ` &` to run longer ones as a job.

## Database viewer (SQLite)

Open a `.db`, `.sqlite`, `.sqlite3`, `.db3`, `.s3db`, `.sl3` or `.sqlitedb` file in the file manager and a small **read-only**
database browser opens instead of the editor: a list of tables and views on the left (a dropdown on phones), then three tabs.

* **Browse** - pages of 100 rows, click a column heading to sort, type in the search box to filter. NULLs, blobs (size and first bytes)
  and long text (cut, with its length) are shown quietly.
* **Structure** - the columns, keys and the `CREATE` statement, plus the table's indexes and triggers.
* **Query** - a `SELECT` box (Ctrl/Cmd + Enter runs it). Only `SELECT`, `WITH`, `EXPLAIN` and read-only `PRAGMA` run; a statement that
  would change anything is refused.

The file is never modified: it is opened read-only, the reader runs in its own limited process (memory, CPU time, file size and
a time limit per query), and a damaged file can only cost one request. A database in WAL mode that is open in a running app is read
without its log (the header says so). Files that are not SQLite (an encrypted database, `Thumbs.db`) give a clear message and a download button.
