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

DATA="${RCONSOLE_DATA_DIR:-/data}"
mkdir -p "$DATA"

if [ "$(id -u)" = "0" ] && [ "${RCONSOLE_RUN_AS_ROOT:-0}" != "1" ]; then
    if [ "$(stat -c %u "$DATA")" != "10001" ]; then
        chown -R 10001:10001 "$DATA"
    fi
    export HOME=/home/rconsole
    exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
fi

exec "$@"
