"""
Fills sidecar.resolved_geo using Overture Maps' open Divisions dataset
(polygon-based place boundaries) for photos still unresolved after
reverse_geocode.py's pass against Immich's own GeoNames-backed geocoder.

Chains off reverse_geocode.py rather than duplicating its work: candidates
are photos with coordinates that have NO row yet in sidecar.resolved_geo at
all (any source), not just asset_exif.city IS NULL -- so this only spends
network/CPU time on genuinely unresolved photos.

Match strategy, confirmed via a live spike (sidecar/spike_overture_schema.py)
against a real SE Alaska coordinate:
  1. Strict point-in-polygon containment (ST_Contains), bbox-prefiltered for
     performance -- without the prefilter this scans Overture's ENTIRE
     global division_area dataset with no spatial index (confirmed to
     appear to hang on the Pi).
  2. If containment finds nothing (a genuine water/offshore point), fall
     back to nearest-polygon (ST_Distance) within a bounded margin.
Preference order among matches: locality > county > region > country, so
the most specific available level populates city/county/state/country.

county is populated here (Overture's polygon data has real county-level
boundaries) even though Immich's own geocoder never provides one -- see
migrations/001_initial_schema.sql for why that matters for rural/
unincorporated areas.

MODEL_VERSION is tied to the Overture release string, so switching releases
is a distinct, trackable version -- same idempotency/retry pattern as
reverse_geocode.py.
"""
import logging
import psycopg2
import duckdb

from .. import config
from .. import db as sidecar_db
from .. import test_set

logger = logging.getLogger(__name__)

TOOL = "overture_geocode"

# Confirmed most recent Overture release via docs.overturemaps.org as of
# 2026-08. Overture ships a new release roughly monthly; bump this (and
# MODEL_VERSION follows automatically) when upgrading.
RELEASE = "2026-07-22.0"
MODEL_VERSION = f"overture-divisions-{RELEASE}"

DIVISION_AREA_PATH = (
    f"s3://overturemaps-us-west-2/release/{RELEASE}/"
    "theme=divisions/type=division_area/*"
)

# Bounding-box prefilter margin (degrees) for both the containment query
# (tight, exact point) and the nearest-polygon fallback (wider, for finding
# "the nearest land" near a water point). See spike_overture_schema.py.
NEAREST_BBOX_MARGIN_DEGREES = 0.5

SUBTYPE_TO_FIELD = {
    "locality": "city",
    "county": "county",
    "region": "state",
    "country": "country",
}
SUBTYPE_PREFERENCE = ["locality", "county", "region", "country"]


def _get_immich_connection():
    return psycopg2.connect(**config.immich_db_kwargs())


def _duckdb_connection():
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")
    return con


def find_unresolved(scope="test"):
    """
    Candidates: photos with coordinates in Immich, with NO row yet in
    sidecar.resolved_geo (any source) -- i.e. genuinely never resolved by
    any geocoder, not just still-null in Immich's own asset_exif.city.

    scope='test' (default): only candidates within the pinned test_set.
    scope='full': every unresolved candidate in the library -- must be
    explicit (see run_overture_geocode.py --scope), never the default.

    No cross-database JOIN is possible (Immich's DB and the sidecar DB are
    separate Postgres databases), so already-resolved asset_ids are fetched
    from the sidecar DB first and filtered out in Python. Fine at this
    scale (test_set-sized or single-library-sized, not web-scale).
    """
    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT asset_id FROM resolved_geo;")
            already_resolved = {row[0] for row in cur.fetchall()}

    query = (
        'SELECT "assetId", latitude, longitude FROM asset_exif '
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    )
    params = ()

    if scope == "test":
        test_asset_ids = [row[0] for row in test_set.get()]
        if not test_asset_ids:
            return []
        query += ' AND "assetId" = ANY(%s)'
        params = (test_asset_ids,)
    elif scope != "full":
        raise ValueError(f"scope must be 'test' or 'full', got {scope!r}")

    with _get_immich_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            all_candidates = cur.fetchall()

    return [(aid, lat, lon) for (aid, lat, lon) in all_candidates if aid not in already_resolved]


def _best_match_per_subtype(rows):
    """
    rows: list of (subtype, name, country) tuples. Returns
    {city, county, state, country}, keeping the first (only) value seen per
    subtype -- Overture can return multiple polygons at the same subtype
    (e.g. overlapping/historical boundaries); first-seen is good enough
    here, not a claim of picking the "best" one.
    """
    result = {"city": None, "county": None, "state": None, "country": None}
    for subtype, name, country_code in rows:
        field = SUBTYPE_TO_FIELD.get(subtype)
        if field and result[field] is None:
            result[field] = name
        if result["country"] is None and country_code:
            result["country"] = country_code
    return result


def lookup(con, lat, lon):
    """
    Returns {city, county, state, country} (fields may be None), or None if
    nothing found even via the nearest-polygon fallback.
    """
    contains_rows = con.execute(
        f"""
        SELECT subtype, names.primary AS name, country
        FROM read_parquet('{DIVISION_AREA_PATH}', hive_partitioning=1)
        WHERE bbox.xmin <= {lon} AND bbox.xmax >= {lon}
          AND bbox.ymin <= {lat} AND bbox.ymax >= {lat}
          AND ST_Contains(geometry, ST_Point({lon}, {lat}));
        """
    ).fetchall()

    if contains_rows:
        return _best_match_per_subtype(contains_rows)

    # Fallback: genuine water/offshore point (or a real gap) -- nearest
    # polygon within a bounded margin, same bbox-prefilter technique as the
    # spike script.
    nearest_rows = con.execute(
        f"""
        SELECT subtype, names.primary AS name, country
        FROM read_parquet('{DIVISION_AREA_PATH}', hive_partitioning=1)
        WHERE bbox.xmin <= {lon} + {NEAREST_BBOX_MARGIN_DEGREES}
          AND bbox.xmax >= {lon} - {NEAREST_BBOX_MARGIN_DEGREES}
          AND bbox.ymin <= {lat} + {NEAREST_BBOX_MARGIN_DEGREES}
          AND bbox.ymax >= {lat} - {NEAREST_BBOX_MARGIN_DEGREES}
        ORDER BY ST_Distance(geometry, ST_Point({lon}, {lat}))
        LIMIT 10;
        """
    ).fetchall()

    if not nearest_rows:
        return None
    return _best_match_per_subtype(nearest_rows)


def _already_done(asset_id):
    """Mirrors reverse_geocode.py's fix: only 'done' rows count as skippable."""
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
                    "INSERT INTO resolved_geo (asset_id, city, county, state, country, source) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (asset_id) DO UPDATE SET "
                    "city = EXCLUDED.city, county = EXCLUDED.county, state = EXCLUDED.state, "
                    "country = EXCLUDED.country, source = EXCLUDED.source, "
                    "computed_at = now();",
                    (asset_id, geo.get("city"), geo.get("county"), geo.get("state"),
                     geo.get("country"), "overture_divisions"),
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


def run(scope="test", skip_done=True):
    """
    Main entry point. No client object needed (unlike reverse_geocode.py) --
    queries Overture's data directly via DuckDB, no Immich API call involved.

    scope: 'test' (pinned test_set, default) or 'full' (entire library).
    skip_done: skip asset_ids already marked 'done' (idempotent reruns;
    'failed' rows are retried).
    """
    candidates = find_unresolved(scope=scope)
    logger.info(f"overture_geocode: {len(candidates)} candidate photo(s) found (scope={scope})")

    con = _duckdb_connection()
    processed = 0
    for asset_id, lat, lon in candidates:
        if skip_done and _already_done(asset_id):
            continue
        try:
            geo = lookup(con, lat, lon)
            if geo is None:
                geo = {"city": None, "county": None, "state": None, "country": None}
            _write_result(asset_id, geo)
            logger.info(f"overture_geocode: {asset_id} -> {geo}")
        except Exception as e:
            logger.warning(f"overture_geocode: {asset_id} failed: {e}")
            _write_result(asset_id, geo=None, error=e)
        processed += 1

    logger.info(f"overture_geocode: {processed} photo(s) processed")
    return processed
