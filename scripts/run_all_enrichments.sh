#!/usr/bin/env bash
# run_all_enrichments.sh — host-side cron wrapper for the sidecar enrichment
# pipeline (see docs/sidecar-augmentation.md, "Incremental updates — cron
# job"). Runs on the PI HOST (cron doesn't run inside a container), calling
# each enrichment's entry point inside search-api-dev via `docker exec`.
#
# Order is deliberate: cheap enrichments first, dinov3_landmarks LAST.
# reverse_geocode/overture_geocode/object_detect are all fast per-photo;
# dinov3_landmarks is the only one doing a full-resolution download + GPU
# inference round trip per candidate, and can run for hours at --scope full
# on a real library. Putting it last means the other three are always done
# even if dinov3_landmarks is still grinding (or gets interrupted).
#
# --scope full on every run, relying on skip_done (the default -- no
# --no-skip-done flag anywhere here) to make this idempotent and cheap for
# already-processed photos. This IS the incremental-update mechanism: no
# separate "new photos only" mode exists or is needed -- skip_done already
# makes a full rerun cost roughly nothing for photos that haven't changed.
#
# Install via crontab -e on the Pi:
#   0 2 * * * /home/kyle/photo-search/scripts/run_all_enrichments.sh >> /home/kyle/photo-search/logs/enrichment/cron.log 2>&1
#
# Adjust CONTAINER below if the container name ever changes (docker compose
# derives it from the project directory name).

set -euo pipefail

CONTAINER="photo-search-search-api-dev-1"
LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/logs/enrichment"
mkdir -p "$LOG_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/enrichment_${TIMESTAMP}.log"

echo "=== Enrichment run started $(date) ===" | tee -a "$LOG_FILE"

run_step() {
    local module="$1"
    echo "--- $module ---" | tee -a "$LOG_FILE"
    if ! docker exec "$CONTAINER" python -m "$module" --scope full 2>&1 | tee -a "$LOG_FILE"; then
        echo "!!! $module exited non-zero -- continuing to the next step, see log above !!!" | tee -a "$LOG_FILE"
    fi
}

run_step sidecar.run_reverse_geocode
run_step sidecar.run_overture_geocode
run_step sidecar.run_object_detect
run_step sidecar.run_overture_landmarks
run_step sidecar.run_dinov3_landmarks

echo "=== Enrichment run finished $(date) ===" | tee -a "$LOG_FILE"
