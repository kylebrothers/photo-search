"""
One-off entry point to select and pin the dev test set: ~100 random photos
plus hand-picked hard cases (see docs/sidecar-augmentation.md, "Dev test
set"). Run inside search-api-dev:

    docker exec -it <search-api-dev container> python -m sidecar.populate_test_set

Idempotent -- see test_set.populate() for conflict handling. Safe to rerun;
won't duplicate the random sample, and will refresh hard-case labels if
they've changed here.
"""
import logging
import psycopg2

from . import config
from . import test_set

logger = logging.getLogger(__name__)

RANDOM_SAMPLE_SIZE = 100

# The 5 photos from the reverse-geocode smoke test (2026-08): 2 resolved to
# real cities (Horizon West, Celebration -- the original motivating Disney
# gap), 3 correctly null in remote SE Alaska wilderness (confirmed via a
# manual coordinate/map check to be a real GeoNames coverage gap, not a bug).
# Kept as hard cases both for coverage and as a regression check that the
# sidecar join keeps returning the right answer as the schema evolves.
KNOWN_GEO_HARD_CASES = {
    "17f3b966-5226-4725-bf90-b6d2019cc369": "hard_case:coords_no_city_resolved",
    "73986625-31c6-4c76-9597-2985e1fa088b": "hard_case:coords_no_city_resolved",
    "903dd60a-d2b4-4674-aabe-161039b394d4": "hard_case:coords_no_city_null",
    "4244c524-d270-4059-88d8-00c7303690b4": "hard_case:coords_no_city_null",
    "10bfa853-cdc8-4387-b751-7c90fb948955": "hard_case:coords_no_city_null",
}


def _immich_connection():
    return psycopg2.connect(**config.immich_db_kwargs())


def pick_random(n=RANDOM_SAMPLE_SIZE):
    """
    Random sample of real, visible, non-deleted photos (images, not videos).
    WHERE clause matches an existing Immich index
    (asset_id_timeline_notDeleted_idx: "deletedAt" IS NULL AND
    visibility='timeline'), so this stays fast as the library grows.
    Confirmed against the live schema 2026-08: `type` is a free-text column,
    not an enum -- observed values are 'IMAGE'/'VIDEO', checked via a live
    query rather than assumed.
    """
    query = (
        'SELECT id FROM asset '
        'WHERE "deletedAt" IS NULL AND visibility = \'timeline\' '
        'AND "isOffline" = false AND type = \'IMAGE\' '
        'ORDER BY random() LIMIT %s;'
    )
    with _immich_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (n,))
            return [str(row[0]) for row in cur.fetchall()]


def pick_multi_face():
    """
    The single photo with the most detected faces -- a real hard case for
    person-count queries (e.g. "Kevin alone in frame", the motivating
    example from the design doc). Restricted to the same
    real/visible/non-deleted/image filter as pick_random(). asset_face's
    "assetId" column confirmed via the live schema's FK constraint
    (asset_face_assetId_fkey), not assumed.
    """
    query = (
        'SELECT af."assetId", COUNT(*) AS face_count '
        'FROM asset_face af '
        'JOIN asset a ON a.id = af."assetId" '
        'WHERE a."deletedAt" IS NULL AND a.visibility = \'timeline\' '
        'AND a."isOffline" = false AND a.type = \'IMAGE\' '
        'GROUP BY af."assetId" '
        'ORDER BY face_count DESC LIMIT 1;'
    )
    with _immich_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone()
            return str(row[0]) if row else None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    random_ids = pick_random()
    logger.info(f"picked {len(random_ids)} random photo(s)")

    hard_cases = dict(KNOWN_GEO_HARD_CASES)

    multi_face_id = pick_multi_face()
    if multi_face_id:
        hard_cases[multi_face_id] = "hard_case:multi_face"
        logger.info(f"picked multi-face hard case: {multi_face_id}")
    else:
        logger.warning("no multi-face candidate found (asset_face table empty?)")

    test_set.populate(random_ids, hard_cases)
    logger.info(f"test set populated: {len(random_ids)} random + {len(hard_cases)} hard case(s)")


if __name__ == "__main__":
    main()
