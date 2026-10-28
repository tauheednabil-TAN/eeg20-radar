#!/bin/sh
# Starts as root only to sort out /app/output, then runs the tool without
# privileges. If the folder is mounted from the host, the tool runs as that
# folder's owner so the report files belong to the host user. If Docker
# created the folder (owned by root), it is handed to the radar user.
set -eu

if [ "$(id -u)" != "0" ]; then
    exec python -m eeg20_radar "$@"
fi

mkdir -p /app/output
uid=$(stat -c %u /app/output)
gid=$(stat -c %g /app/output)
if [ "$uid" = "0" ]; then
    chown radar:radar /app/output
    uid=$(id -u radar)
    gid=$(id -g radar)
fi

exec setpriv --reuid="$uid" --regid="$gid" --clear-groups python -m eeg20_radar "$@"
