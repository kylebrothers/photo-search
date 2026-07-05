import os

IMMICH_URL = os.environ.get("IMMICH_URL", "http://immich-server:2283")
IMMICH_API_KEY = os.environ.get("IMMICH_API_KEY", "")

# Direct Postgres access — used only by landmark/match.py, which needs raw
# CLIP embeddings that Immich's public API does not expose.
IMMICH_DB_DSN = os.environ.get(
    "IMMICH_DB_DSN", "postgresql://postgres:postgres@immich-postgres:5432/immich"
)

LANDMARK_MATCH_THRESHOLD = float(os.environ.get("LANDMARK_MATCH_THRESHOLD", "0.75"))
LANDMARK_REFERENCE_STORE = os.environ.get(
    "LANDMARK_REFERENCE_STORE", "/data/landmark_references.json"
)
