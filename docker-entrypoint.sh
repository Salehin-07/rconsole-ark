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
