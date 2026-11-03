#!/bin/sh
# Container entrypoint: make the data volume writable, then drop privileges.
#
# Managed platforms (Render, Fly.io, Railway, ...) mount volumes owned by root, so
# a container hard-wired to an unprivileged user could not write to /data. We
# start as root only long enough to fix that, then run rconsole as `rconsole`
# (uid 10001).
#
# RCONSOLE_RUN_AS_ROOT=1  keep root: admins' *host* shell is then real root in
#                         the container (apt install, etc.). Only sensible for a
#                         single-owner deployment — see DEPLOY.md.
set -e

# `rconsole update` keeps the release it installed in $DATA/update. A redeploy rebuilds the image from
# whatever code you deployed, which may be older: `updater.py sync` re-applies the newer release (and
# rolls back a new version that keeps failing to start). Never fatal: the server must still come up.
SYNC="python /app/updater.py sync"

DATA="${RCONSOLE_DATA_DIR:-/data}"
mkdir -p "$DATA"

# The host shell starts in /home/rconsole. Make that the persistent workspace ($DATA/workspace, which is
# what backups include) instead of a throw-away directory inside the image. Whatever the image put in the
# home directory (.bashrc, .profile, ...) is kept. Never fatal.
if [ "$(id -u)" = "0" ] && [ ! -L /home/rconsole ]; then
    mkdir -p "$DATA/workspace"
    if [ -d /home/rconsole ]; then
        cp -an /home/rconsole/. "$DATA/workspace/" 2>/dev/null || true
        rm -rf /home/rconsole
    fi
    ln -s "$DATA/workspace" /home/rconsole 2>/dev/null || echo "[rconsole] could not link /home/rconsole" >&2
fi

if [ "$(id -u)" = "0" ] && [ "${RCONSOLE_RUN_AS_ROOT:-0}" != "1" ]; then
    if [ "$(stat -c %u "$DATA")" != "10001" ]; then
        chown -R 10001:10001 "$DATA"
    fi
    export HOME=/home/rconsole
    setpriv --reuid=10001 --regid=10001 --init-groups $SYNC || echo "[rconsole] update sync skipped" >&2
    exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
fi

$SYNC || echo "[rconsole] update sync skipped" >&2
exec "$@"
