"""
Fills sidecar.resolved_geo for photos with GPS coordinates but no
reverse-geocoded city, by calling Immich's own GET /map/reverse-geocode
(see sidecar-augmentation.md, option A) rather than reimplementing geocoding.

MODEL_VERSION identifies this as an API-backed enrichment (not a local ML
model) so the schema stays honest if a different geocoder is swapped in later.
"""
import logging
import psycopg2

from .. import config
from .. import db as sidecar_db
from .. import test_set

logger = logging.getLogger(__name__)

TOOL = "reverse_geocode"
MODEL_VERSION = "immich-api-v1"


def _get_immich_connection():
    return psycopg2.connect(**config.immich_db_kwargs())


def find_unresolved(scope="test"):
    """
    Query Immich's own database for photos with coordinates but no city.

    scope='test' (default): only candidates within the pinned test_set (see
    sidecar/test_set.py) -- keeps dev runs comparable across approaches/
    reruns. scope='full': every unresolved candidate in the library --
    should be a deliberate, explicit choice (see run_reverse_geocode.py
    --scope), never the silent default.

    CONFIRMED against the live instance on 2026-07 via `\d+ asset_exif`:
    table is `asset_exif` (the design doc's original assumption was right;
    an intermediate correction to `exif` based on a GitHub discussion was
    wrong for this Immich version). Columns assetId/latitude/longitude/city
    all present as used below.
    """
    query = (
        'SELECT "assetId", latitude, longitude FROM asset_exif '
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND city IS NULL"
    )
    params = ()

    if scope == "test":
        test_asset_ids = [row[0] for row in test_set.get()]
        if not test_asset_ids:
            return []
        # Explicit ::uuid[] cast -- without it, psycopg2's array adaptation
        # of a Python list of UUID objects does not reliably produce a
        # uuid[] array, so Postgres compares "assetId" (uuid) against a
        # text[] array and raises "operator does not exist: uuid = text".
        # Confirmed via a live failure 2026-08.
        query += ' AND "assetId" = ANY(%s::uuid[])'
        params = (test_asset_ids,)
    elif scope != "full":
        raise ValueError(f"scope must be 'test' or 'full', got {scope!r}")

    with _get_immich_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()  # [(asset_id, lat, lon), ...]


def _already_done(asset_id):
    """
    True only if a prior run succeeded for this asset/tool/model_version.
    Deliberately does NOT match 'failed' rows -- those should be retried by
    skip_done, per run()'s docstring. (Bug found 2026-08: an earlier version
    matched on row existence alone, so a failed first attempt permanently
    blocked all retries -- surfaced by "0 photo(s) processed" on a rerun
    with 5 real candidates and no error output.)
    """
    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM enrichment_status "
                "WHERE asset_id = %s AND tool = %s AND model_version = %s "
                "AND status = 'done';",
                (asset_id, TOOL, MODEL_VERSION),
            )
            return cur.fetchone() is not None


def _write_result(asset_id, geo, error=None):
    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            if error is None:
                cur.execute(
                    "INSERT INTO resolved_geo (asset_id, city, state, country, source) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (asset_id) DO UPDATE SET "
                    "city = EXCLUDED.city, state = EXCLUDED.state, "
                    "country = EXCLUDED.country, source = EXCLUDED.source, "
                    "computed_at = now();",
                    (asset_id, geo.get("city"), geo.get("state"), geo.get("country"),
                     "immich_reverse_geocode"),
                )
                cur.execute(
                    "INSERT INTO enrichment_status "
                    "(asset_id, tool, model_version, status) VALUES (%s, %s, %s, 'done') "
                    "ON CONFLICT (asset_id, tool, model_version) "
                    "DO UPDATE SET status = 'done', error_detail = NULL, computed_at = now();",
                    (asset_id, TOOL, MODEL_VERSION),
                )
            else:
                cur.execute(
                    "INSERT INTO enrichment_status "
                    "(asset_id, tool, model_version, status, error_detail) "
                    "VALUES (%s, %s, %s, 'failed', %s) "
                    "ON CONFLICT (asset_id, tool, model_version) "
                    "DO UPDATE SET status = 'failed', error_detail = EXCLUDED.error_detail, "
                    "computed_at = now();",
                    (asset_id, TOOL, MODEL_VERSION, str(error)),
                )
        conn.commit()


def run(immich_client, scope="test", skip_done=True):
    """
    Main entry point. immich_client: an ImmichClient instance (see
    search-api/immich_client.py, .reverse_geocode(lat, lon)).

    scope: 'test' (pinned test_set, default) or 'full' (entire library).
    skip_done: skip asset_ids already marked 'done' for this tool/model_version
    (idempotent reruns -- 'failed' rows are retried).
    """
    candidates = find_unresolved(scope=scope)
    logger.info(f"reverse_geocode: {len(candidates)} candidate photo(s) found (scope={scope})")

    processed = 0
    for asset_id, lat, lon in candidates:
        if skip_done and _already_done(asset_id):
            continue
        try:
            geo = immich_client.reverse_geocode(lat, lon)
            _write_result(asset_id, geo)
            logger.info(f"reverse_geocode: {asset_id} -> {geo}")
        except Exception as e:
            logger.warning(f"reverse_geocode: {asset_id} failed: {e}")
            _write_result(asset_id, geo=None, error=e)
        processed += 1

    logger.info(f"reverse_geocode: {processed} photo(s) processed")
    return processed
