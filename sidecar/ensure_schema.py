"""
Declarative, idempotent schema evolutions beyond the initial migration
(migrations/001_initial_schema.sql). Run this any time the schema needs to
catch up on a database that already has data -- safe to run repeatedly,
built on db.ensure_column()/ensure_table() (ALTER ... IF NOT EXISTS /
CREATE TABLE IF NOT EXISTS), so it never errors on an already-applied
change.

This is the fix for the "typed ALTER TABLE into psql by hand" gap flagged
in docs/sidecar-augmentation.md, "Schema evolution tooling." Add new
db.ensure_column()/ensure_table() calls to run() below as the schema grows,
rather than running one-off ALTER TABLE commands by hand.

Run inside search-api-dev:
    docker exec -it <search-api-dev container> python -m sidecar.ensure_schema
"""
import logging

from . import db

logger = logging.getLogger(__name__)


def run():
    # landmark_matches.source: added 2026-08 -- the table was designed
    # before it was clear landmark matches would come from two genuinely
    # different provenances (visual recognition vs. geospatial proximity).
    # See docs/sidecar-augmentation.md, "Landmark matching."
    db.ensure_column("landmark_matches", "source", "text")
    logger.info("ensured: landmark_matches.source")

    # landmark_matches.distance_meters: added 2026-08, second real dogfood
    # use of this tooling. Needed by overture_landmarks.py (proximity
    # matching) -- a 50m match and an 800m match mean very different things
    # for relevance, and this wasn't in the original schema since the
    # proximity source didn't exist yet when the table was designed.
    db.ensure_column("landmark_matches", "distance_meters", "double precision")
    logger.info("ensured: landmark_matches.distance_meters")

    # Add future schema evolutions here, e.g.:
    # db.ensure_column("resolved_geo", "some_new_field", "text")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
    print("Schema ensured.")
