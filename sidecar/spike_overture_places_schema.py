"""
ONE-OFF SPIKE, not part of the enrichment pipeline. Confirms Overture's real
Places theme schema before writing overture_landmarks.py -- same discipline
as spike_overture_schema.py for the Divisions theme.

IMPORTANT, confirmed via docs.overturemaps.org 2026-08: the places schema's
`categories` property is DEPRECATED and will be REMOVED in the September
2026 release -- one release after RELEASE below. Building against
`categories` would be building against a field about to disappear.
`basic_category` and `taxonomy` are the replacement properties (available
alongside `categories` for a transition period) -- this spike targets those,
not `categories`.

Run inside search-api-dev:
    docker exec -it <search-api-dev container> python -m sidecar.spike_overture_places_schema
"""
import duckdb

# Same release already used for Divisions (overture_geocode.py) -- keeping
# both themes on one release avoids any cross-theme version skew.
RELEASE = "2026-07-22.0"
PLACES_PATH = f"s3://overturemaps-us-west-2/release/{RELEASE}/theme=places/type=place/*"

# One of the Disney/Bay Lake, FL coordinates already resolved in the test
# set (overture_geocode.py's earlier run) -- real, known-good test point
# with landmarks genuinely nearby (Magic Kingdom attractions).
TEST_LAT, TEST_LON = 28.4177, -81.5812  # approximate Magic Kingdom area
BBOX_MARGIN_DEGREES = 0.02  # roughly 1.5-2km at this latitude -- tight, deliberately


def main():
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")

    print(f"--- Schema of places/place (release {RELEASE}) ---")
    schema = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{PLACES_PATH}', hive_partitioning=1) LIMIT 1;"
    ).fetchall()
    for col_name, col_type, *_ in schema:
        print(f"  {col_name}: {col_type}")

    print(f"\n--- Nearby places (bbox-prefiltered) near Magic Kingdom "
          f"({TEST_LAT}, {TEST_LON}) ---")
    nearby = con.execute(
        f"""
        SELECT names.primary AS name, confidence, basic_category, categories
        FROM read_parquet('{PLACES_PATH}', hive_partitioning=1)
        WHERE bbox.xmin <= {TEST_LON} + {BBOX_MARGIN_DEGREES}
          AND bbox.xmax >= {TEST_LON} - {BBOX_MARGIN_DEGREES}
          AND bbox.ymin <= {TEST_LAT} + {BBOX_MARGIN_DEGREES}
          AND bbox.ymax >= {TEST_LAT} - {BBOX_MARGIN_DEGREES}
        ORDER BY confidence DESC
        LIMIT 20;
        """
    ).fetchall()
    if nearby:
        for row in nearby:
            print(f"  {row}")
    else:
        print("  Nothing found -- check coordinates or widen BBOX_MARGIN_DEGREES.")

    print(f"\n--- Distinct basic_category values in this area (for landmark filtering) ---")
    categories = con.execute(
        f"""
        SELECT DISTINCT basic_category
        FROM read_parquet('{PLACES_PATH}', hive_partitioning=1)
        WHERE bbox.xmin <= {TEST_LON} + {BBOX_MARGIN_DEGREES}
          AND bbox.xmax >= {TEST_LON} - {BBOX_MARGIN_DEGREES}
          AND bbox.ymin <= {TEST_LAT} + {BBOX_MARGIN_DEGREES}
          AND bbox.ymax >= {TEST_LAT} - {BBOX_MARGIN_DEGREES}
        LIMIT 50;
        """
    ).fetchall()
    for row in categories:
        print(f"  {row}")


if __name__ == "__main__":
    main()
