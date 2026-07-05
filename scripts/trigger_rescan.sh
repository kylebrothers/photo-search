#!/bin/bash
# Manually triggers a rescan of the Dropbox external library in Immich.
#
# UNVERIFIED: /api/libraries/{id}/scan is Immich's documented endpoint shape
# as of the version this was written against — confirm against your running
# instance's API docs (usually at $IMMICH_URL/api/doc) before relying on it.
#
# Usage: ./trigger_rescan.sh <library_id>
# Find your library ID: curl -H "x-api-key: $IMMICH_API_KEY" $IMMICH_URL/api/libraries

set -e

if [ -z "$1" ]; then
  echo "Usage: $0 <library_id>"
  echo "List libraries: curl -H \"x-api-key: \$IMMICH_API_KEY\" \$IMMICH_URL/api/libraries"
  exit 1
fi

LIBRARY_ID="$1"
: "${IMMICH_URL:?Set IMMICH_URL, e.g. http://localhost:2283}"
: "${IMMICH_API_KEY:?Set IMMICH_API_KEY}"

curl -X POST "${IMMICH_URL}/api/libraries/${LIBRARY_ID}/scan" \
  -H "x-api-key: ${IMMICH_API_KEY}" \
  -H "Content-Type: application/json" \
  -w "\nHTTP %{http_code}\n"
