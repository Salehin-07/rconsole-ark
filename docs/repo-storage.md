# Repo storage: keep your files in GitHub

Free hosts give you a small disk that is wiped on every deploy. With **repo storage** your files live in your private
GitHub repository and the host's disk only **caches** them. Switch it on in the dashboard (**Repo storage**) or with
`RCONSOLE_STORAGE=github`.

## What is stored

| Stored in the repo | Stays on the host |
|---|---|
| the **workspace** (the host shell's home, `/home/rconsole`) | `/root`, `/etc`, `/proc`, `/app`, `/usr`, `/var` and the other system folders |
| each Linux system's `/home`, `/srv` and `/data` | `node_modules`, virtualenvs, `__pycache__` and other caches (they are rebuilt) |

## How it works

* **Pieces.** Every file is cut into pieces of at most `RCONSOLE_STORAGE_CHUNK_MB` (default 50, maximum 95) because
  GitHub rejects files over 100 MB. The pieces are joined again when you open the file. There is no limit on the
  number of pieces, so any file size works. Each piece is stored once under its content hash: identical data is not
  duplicated, and appending to a log re-sends only its last piece.
* **What triggers a sync.** The upload button (files and folders), the editor, the file manager actions and any
  command that changes a file. Changes are picked up within `RCONSOLE_STORAGE_INTERVAL` seconds (20 by default) and
  again at shutdown.
* **After a restart or a fresh deploy.** Only the repo is needed. The folder tree comes back at once, small files with
  their content, big files (from `RCONSOLE_STORAGE_STUB_MB`, default 4) as small **placeholder files** that say where
  the data is. A placeholder is replaced by the real file the moment the file manager (open, edit, download, preview,
  compress) or a command that names it needs it.
* **Cache.** Above `RCONSOLE_STORAGE_CACHE_MB` (default 1024) the least recently used big files that are safely in the
  repo become placeholders again. `storage pin <path>` keeps a file on the disk for good.
* **Encryption.** On by default: pieces and the list of file names are AES-256-GCM encrypted with a key derived from
  `RCONSOLE_BACKUP_PASSPHRASE`. Turn it off in the dashboard to store everything as plain text. "Rewrite everything"
  converts files that are already stored. **Keep a copy of the passphrase elsewhere.**
* **Fallback.** If the repo is not configured, the passphrase is missing while encryption is on, or the mode is
  `host`, nothing changes: files stay on the host disk exactly as before.

## Setting it up

1. Create a **private** repository and a token with *Contents: read and write* on it.
2. Dashboard → **Repo storage** → enter the URL, token and passphrase → **Save** → **Test connection**.
3. Choose **In the GitHub repo** → **Apply**. The first upload runs in the background (watch the status card).

The same repository is used by [backups](backup.md) on another branch (`rconsole-backup`; storage uses
`rconsole-storage`). On a host with no persistent disk keep `RCONSOLE_BACKUP_URL`, `RCONSOLE_BACKUP_TOKEN` and
`RCONSOLE_BACKUP_PASSPHRASE` as environment variables, so a blank instance can find the repo.

## Good to know

* A program that opens a big file you did **not** name on its command line sees the placeholder text. Run
  `storage get <path>` first (or `storage pin`). Interactive windows do not fetch automatically.
* `ls -l` and `du` show the placeholder's size for a placeholder; the file manager shows the real size.
* The repo is rewritten as a single commit on every push, so it never accumulates history, but GitHub recommends
  keeping repositories under a few GB.
* Uses plain `git`; it works with any git host.

## Every option from the dashboard

The **Settings** page edits every environment option. Values are saved in `data/settings.json` (private) and win over
the environment variable of the same name; **Reset** returns to it. Most changes apply immediately; the ones that
cannot (port, threads, log level, proxy trust, supervisor) are marked *restart* and the page offers a restart button.
Tokens and passphrases are write-only.
