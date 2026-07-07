#!/bin/bash
# Mounts Dropbox via rclone inside the immich-server container, then hands
# off to Immich's own startup process.
#
# VERIFIED (2026-07-07) against ghcr.io/immich-app/immich-server:release via:
#   docker inspect --format='Entrypoint: {{json .Config.Entrypoint}}{{"\n"}}Cmd: {{json .Config.Cmd}}' ...
# which returned:
#   Entrypoint: ["tini","--","/bin/bash","-c"]
#   Cmd: ["start.sh"]
# i.e. the base image runs `start.sh` as a bare command resolved via $PATH,
# not an absolute path — so we replicate that exact invocation rather than
# hardcoding a path, since PATH is already set correctly by the base image.


set -e


mkdir -p /mnt/dropbox
rclone mount dropbox: /mnt/dropbox \
  --config /root/.config/rclone/rclone.conf \
  --vfs-cache-mode writes --vfs-cache-max-size 20G \
  --daemon


sleep 2  # let the mount settle before Immich starts scanning it


exec tini -- /bin/bash -c "start.sh"
