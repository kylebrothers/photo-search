# Base image is tracked at :release; pin to a specific tag if it ever breaks
# unexpectedly (see README, "core stack" versioning note).
FROM ghcr.io/immich-app/immich-server:release

RUN apt-get update && apt-get install -y rclone fuse3 && rm -rf /var/lib/apt/lists/*

COPY entrypoint-wrapper.sh /entrypoint-wrapper.sh
RUN chmod +x /entrypoint-wrapper.sh

# rclone.conf is mounted at runtime via docker-compose.yml, NOT copied here.
# It contains Dropbox OAuth credentials — baking it into an image layer would
# leave secrets in the image history even if later removed. See
# rclone.conf.example and .gitignore.

ENTRYPOINT ["/entrypoint-wrapper.sh"]
