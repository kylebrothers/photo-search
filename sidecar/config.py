import os

# Dev sidecar DB. Separate from Immich's own Postgres (see search-api/config.py
# SQL_READONLY_DSN) and separate from any future production sidecar DB.
# Wipe-and-redevelop freely; the prod search-api container never sets or reads
# this variable.
SIDECAR_DB_DSN = os.environ.get("SIDECAR_DB_DSN", "")

# Row cap for ad-hoc reads against the sidecar (mirrors search-api's
# SQL_ROW_CAP pattern) — not yet wired to a query tool, kept for when the
# sidecar is exposed to the search agent.
SIDECAR_ROW_CAP = int(os.environ.get("SIDECAR_ROW_CAP", "100"))

# Immich's own database — read-only access, needed by enrichment jobs that
# find their own work (e.g. reverse_geocode.py scanning for coords-without-
# city photos) rather than being handed a list. Same DSN search-api/db.py
# already uses; duplicated here so sidecar/ has no import dependency on
# search-api/.
IMMICH_DB_DSN = os.environ.get("IMMICH_DB_DSN", "")
