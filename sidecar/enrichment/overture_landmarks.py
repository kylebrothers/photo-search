"""
Fills sidecar.landmark_matches with nearby named landmarks/attractions from
Overture's Places theme -- the geospatial-proximity half of the dual-source
landmark design (see docs/sidecar-augmentation.md, "Landmark matching").
Complementary to a future visual-recognition source (DINOv3, not yet
built), not a replacement for it -- both should be queried by the search
agent, not one preferred over the other.

Candidates are ALL coordinate-bearing photos, independent of
resolved_geo/enrichment_status for the geocoding tools -- landmark
proximity doesn't depend on whether reverse-geocoding succeeded.

Category filtering, confirmed via a live schema spike
(spike_overture_places_schema.py) against real Disney-area data, NOT
guessed: Overture's `basic_category` field is unreliable on its own (e.g.
a real resort came back basic_category='amusement_park', contradicted by
its own more-specific taxonomy.primary='resort'). taxonomy.primary is
checked first as the primary signal (Overture's own docs describe taxonomy
as the fix for basic_category's "structural inconsistencies, naming
ambiguity"); basic_category is used only as a secondary fallback signal.

LANDMARK_TAXONOMY_VALUES below is a CONFIRMED-REAL list, sourced directly
from live query results near a real, dense (Disney World) area:
amusement_park, amusement_attraction, museum, castle, mountain, island,
public_fountain, historic_site, landmark_and_historical_building, marina,
beach. Values beyond this (zoo, lighthouse, monument, cathedral, etc.) are
PLAUSIBLE EXTRAPOLATIONS, not confirmed against real data -- flagged
separately below, not silently merged into the confirmed list, so it's
honest about what's actually been verified. Add to either list as new
photo locations surface more real category values worth checking.

KNOWN ISSUE, confirmed 2026-08 against real test-set data (not just the
Disney spike): 'landmark_and_historical_building' is noisier than expected
-- Overture applies it to many ordinary named apartment/condo buildings,
not just genuine tourist landmarks (the original spike's "Windermere Cay
Apartments" false positive turned out to be the general pattern, not an
isolated case). PARTIALLY addressed 2026-08 via RESIDENTIAL_NAME_KEYWORDS
below -- a name-substring exclusion, not a structural fix. Checked against
the actual noise from a real test run: most of it (e.g. "800 Apartments",
"Dosker Manor", "Lofts of Broadway", "Jackson Tower Condominiums") contains
an unambiguous residential-real-estate term and gets caught. Some does NOT
(e.g. "Vue at 3rd", "WG Louisville", "Madrid Building") and slips through
regardless -- this is a heuristic, not a complete fix. Kept intentionally
narrow (apartments/condos/lofts/flats/manor/townhomes/residences only) to
avoid excluding genuine landmarks that happen to contain broader,
ambiguous words like "tower" or "square" (Eiffel Tower, Times Square).

CATEGORY_FILTER_VERSION is part of MODEL_VERSION specifically so refining
this list (e.g. adding zoo/lighthouse after verifying them, or tightening
the noisy landmark_and_historical_building case above) is a tracked
version bump, not a silent behavior change under the same model_version --
same reasoning as OBJECT_DETECT_VOCABULARY_VERSION.
"""
import logging
import math

import psycopg2
import duckdb

from .. import config
from .. import db as sidecar_db
from .. import test_set

logger = logging.getLogger(__name__)

TOOL = "overture_landmarks"

# Same release already used for Divisions/Places spikes -- keeping all
# Overture usage on one release avoids cross-theme version skew.
RELEASE = "2026-07-22.0"
PLACES_PATH = f"s3://overturemaps-us-west-2/release/{RELEASE}/theme=places/type=place/*"

CATEGORY_FILTER_VERSION = "landmark-categories-v2"
MODEL_VERSION = f"overture-places:{RELEASE}:{CATEGORY_FILTER_VERSION}"

# CONFIRMED against real data (see module docstring) -- checked first via
# taxonomy.primary. NOTE: landmark_and_historical_building is confirmed
# real but also confirmed NOISY (see module docstring "KNOWN ISSUE") --
# partially mitigated by RESIDENTIAL_NAME_KEYWORDS below, not fully fixed.
LANDMARK_TAXONOMY_VALUES = {
    "amusement_park", "amusement_attraction", "museum", "castle", "mountain",
    "island", "public_fountain", "historic_site",
    "landmark_and_historical_building", "marina", "beach",
}

# PLAUSIBLE but NOT verified against real query results -- included as a
# secondary basic_category fallback check only (lower confidence in
# correctness than the confirmed list above). Revisit once real photo
# locations surface these categories to confirm or drop them.
LANDMARK_BASIC_CATEGORY_FALLBACK = {
    "zoo", "lighthouse", "monument", "cathedral", "aquarium", "stadium",
    "national_park", "historical_landmark",
}

# Name-substring exclusion for the landmark_and_historical_building noise
# problem (see module docstring "KNOWN ISSUE"). Deliberately narrow --
# unambiguous residential-real-estate terms only. Checked case-insensitively
# as a substring anywhere in the name.
RESIDENTIAL_NAME_KEYWORDS = {
    "apartment", "apartments", "condominium", "condominiums", "condo", "condos",
    "loft", "lofts", "flat", "flats", "manor", "townhome", "townhomes",
    "residence", "residences",
}

# Overture's own existence-confidence score threshold -- their docs
# describe confidence as "the primary tool for separating reliable records
# from suspect ones." 0.7 is a reasonable-but-not-empirically-tuned
# starting point; revisit if real results show too much noise or too few
# matches.
MIN_CONFIDENCE = 0.7

# Search radius. 500m chosen as "close enough to plausibly be at or near
# the landmark," not empirically validated against real photos yet --
# revisit based on real match quality once this runs against real data.
MAX_DISTANCE_METERS = 500

EARTH_RADIUS_METERS = 6371000


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
    Candidates: all photos with coordinates, regardless of geocoding
    status -- landmark proximity is independent of whether reverse-geocode
    succeeded. Excludes photos with a 'done' enrichment_status row for this
    tool/model_version already.

    scope='test' (default): only the pinned test_set. scope='full': the
    entire library -- must be explicit (see run_overture_landmarks.py
    --scope).
    """
    query = (
        'SELECT "assetId", latitude, longitude FROM asset_exif '
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    )
    params = ()

    if scope == "test":
        test_asset_ids = [row[0] for row in test_set.get()]
        if not test_asset_ids:
            return []
        query += ' AND "assetId" = ANY(%s::uuid[])'
        params = (test_asset_ids,)
    elif scope != "full":
        raise ValueError(f"scope must be 'test' or 'full', got {scope!r}")

    with _get_immich_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            all_candidates = cur.fetchall()

    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asset_id FROM enrichment_status "
                "WHERE tool = %s AND model_version = %s AND status = 'done';",
                (TOOL, MODEL_VERSION),
            )
            already_done = {row[0] for row in cur.fetchall()}

    return [(aid, lat, lon) for (aid, lat, lon) in all_candidates if aid not in already_done]


def _looks_residential(name):
    """Case-insensitive substring check against RESIDENTIAL_NAME_KEYWORDS.
    A heuristic, not a guarantee -- see module docstring "KNOWN ISSUE"."""
    name_lower = name.lower()
    return any(keyword in name_lower for keyword in RESIDENTIAL_NAME_KEYWORDS)


def _is_landmark(name, taxonomy_primary, basic_category):
    """taxonomy.primary checked first (more reliable, per module docstring);
    basic_category only as a secondary fallback signal. Either match is then
    rejected if the name looks residential (see _looks_residential)."""
    if _looks_residential(name):
        return False
    if taxonomy_primary in LANDMARK_TAXONOMY_VALUES:
        return True
    if basic_category in LANDMARK_BASIC_CATEGORY_FALLBACK:
        return True
    return False


def find_nearby_landmarks(con, lat, lon):
    """
    Bbox-prefiltered (same performance reasoning as spike_overture_schema.py
    -- avoids scanning Overture's entire global places dataset), then exact
    Haversine distance computed in SQL and filtered to MAX_DISTANCE_METERS.
    Longitude margin is latitude-adjusted (divided by cos(latitude)) so the
    prefilter stays wide enough at high latitudes, where a degree of
    longitude covers less real distance -- this library has real photos as
    far north as ~57.5N (SE Alaska), where an unadjusted margin could
    silently miss real matches.

    Returns list of dicts: {name, distance_meters, confidence}.
    """
    base_margin_degrees = (MAX_DISTANCE_METERS / 111000) * 1.5  # 1.5x buffer before exact filtering
    lon_margin = base_margin_degrees / math.cos(math.radians(lat))
    lat_margin = base_margin_degrees

    # Bbox prefilter first (cheap), exact Haversine distance computed via a
    # CTE and filtered in the outer query -- can't filter on a SELECT-list
    # alias directly in the same query's WHERE clause (standard SQL
    # restriction), and QUALIFY is for window functions, not a plain
    # computed column, so a CTE is the correct tool here, not either of
    # those.
    #
    # Every interpolated coordinate value is explicitly cast to DOUBLE.
    # Without this, DuckDB infers a fixed-precision DECIMAL type from each
    # literal's exact digit count -- and depending on how many decimal
    # places a given lon_margin happens to compute to (varies by
    # latitude), the inferred precision sometimes has no room left for a
    # 2-3 digit longitude, overflowing. Confirmed via a live failure
    # 2026-08: "-84.885556" and "-122.087738" both overflowed a
    # DuckDB-inferred DECIMAL(18,17), while other coordinates in the same
    # run happened not to. Casting to DOUBLE sidesteps decimal-literal
    # inference entirely rather than trying to predict when it misfires.
    query = f"""
        WITH candidates AS (
            SELECT
                names.primary AS name,
                confidence,
                taxonomy.primary AS taxonomy_primary,
                basic_category,
                2 * {EARTH_RADIUS_METERS} * asin(sqrt(
                    pow(sin(radians(ST_Y(ST_Centroid(geometry)) - {lat}::DOUBLE) / 2), 2) +
                    cos(radians({lat}::DOUBLE)) * cos(radians(ST_Y(ST_Centroid(geometry)))) *
                    pow(sin(radians(ST_X(ST_Centroid(geometry)) - {lon}::DOUBLE) / 2), 2)
                )) AS distance_meters
            FROM read_parquet('{PLACES_PATH}', hive_partitioning=1)
            WHERE bbox.xmin <= {lon}::DOUBLE + {lon_margin}::DOUBLE
              AND bbox.xmax >= {lon}::DOUBLE - {lon_margin}::DOUBLE
              AND bbox.ymin <= {lat}::DOUBLE + {lat_margin}::DOUBLE
              AND bbox.ymax >= {lat}::DOUBLE - {lat_margin}::DOUBLE
              AND confidence >= {MIN_CONFIDENCE}
        )
        SELECT name, confidence, taxonomy_primary, basic_category, distance_meters
        FROM candidates
        WHERE distance_meters <= {MAX_DISTANCE_METERS}
        ORDER BY distance_meters;
    """
    rows = con.execute(query).fetchall()

    matches = []
    for name, confidence, taxonomy_primary, basic_category, distance_meters in rows:
        if name and _is_landmark(name, taxonomy_primary, basic_category):
            matches.append({
                "name": name,
                "confidence": confidence,
                "distance_meters": distance_meters,
            })
    return matches


def _already_done(asset_id):
    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM enrichment_status "
                "WHERE asset_id = %s AND tool = %s AND model_version = %s "
                "AND status = 'done';",
                (asset_id, TOOL, MODEL_VERSION),
            )
            return cur.fetchone() is not None


def _write_result(asset_id, matches, error=None):
    """
    matches: list of {name, confidence, distance_meters} dicts, or None on
    error. Writes one landmark_matches row per match, plus one
    enrichment_status row unconditionally (even zero nearby landmarks is a
    valid, correct result -- same pattern as every other enrichment here).
    """
    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            if error is None:
                for m in matches:
                    cur.execute(
                        "INSERT INTO landmark_matches "
                        "(asset_id, landmark_name, confidence, distance_meters, "
                        "source, model_version) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (asset_id, landmark_name, model_version) DO UPDATE SET "
                        "confidence = EXCLUDED.confidence, "
                        "distance_meters = EXCLUDED.distance_meters, "
                        "computed_at = now();",
                        (asset_id, m["name"], m["confidence"], m["distance_meters"],
                         "overture_places", MODEL_VERSION),
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
    Main entry point. No external service call needed -- queries Overture's
    data directly via DuckDB, same as overture_geocode.py.

    scope: 'test' (pinned test_set, default) or 'full' (entire library).
    skip_done: skip asset_ids already marked 'done' for this tool/
    model_version ('failed' rows are retried).
    """
    candidates = find_unresolved(scope=scope)
    logger.info(f"overture_landmarks: {len(candidates)} candidate photo(s) found (scope={scope})")

    con = _duckdb_connection()
    processed = 0
    for asset_id, lat, lon in candidates:
        if skip_done and _already_done(asset_id):
            continue
        try:
            matches = find_nearby_landmarks(con, lat, lon)
            _write_result(asset_id, matches)
            logger.info(f"overture_landmarks: {asset_id} -> {matches}")
        except Exception as e:
            logger.warning(f"overture_landmarks: {asset_id} failed: {e}")
            _write_result(asset_id, matches=None, error=e)
        processed += 1

    logger.info(f"overture_landmarks: {processed} photo(s) processed")
    return processed
