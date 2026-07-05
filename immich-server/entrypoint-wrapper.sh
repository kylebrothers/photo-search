#!/bin/bash
# Mounts Dropbox via rclone inside the immich-server container, then hands
# off to Immich's own startup process.
#
# UNVERIFIED: the final `exec` line below assumes Immich's real entrypoint is
# /usr/src/app/start.sh launched via tini. Confirm this against the actual
# base image before relying on it — e.g.:
#   docker run --rm --entrypoint sh ghcr.io/immich-app/immich-server:release \
#     -c "cat /entrypoint.sh 2>/dev/null || echo 'check image docs for real entrypoint'"
# and adjust the exec line to match whatever Immich actually runs.

set -e

mkdir -p /mnt/dropbox
rclone mount dropbox: /mnt/dropbox \
  --config /root/.config/rclone/rclone.conf \
  --vfs-cache-mode writes --vfs-cache-max-size 20G \
  --daemon

sleep 2  # let the mount settle before Immich starts scanning it

exec /bin/tini -- /usr/src/app/start.sh
