"""
ONE-OFF SPIKE, not part of the enrichment pipeline. Run once to confirm
Overture's real division_area schema and test live lookups against a known
coordinate, before writing overture_geocode.py against assumed column names
or match strategy -- same discipline that caught the asset_exif/exif and
/map vs /api/map bugs earlier in this project.

Run inside search-api-dev:
    docker exec -it <search-api-dev container> python -m sidecar.spike_overture_schema

RELEASE is hardcoded to the most recent Overture release confirmed via
docs.overturemaps.org as of 2026-08 (2026-07-22.0). Overture ships a new
release roughly monthly; bump this string (or add real "latest release"
discovery) before this becomes a permanent enrichment, not just a spike.

2026-08 update: added a nearest-polygon (distance-based) query alongside the
original strict-containment one. The containment query returned ZERO matches,
even at country level, for the test coordinate -- most likely because that
point is in water (a fjord/channel), and pure ST_Contains can never match an
offshore point against any land polygon, however dense the data is. This is
the real reason immich-reversegeo's docs claim better coastline handling:
that only makes sense as nearest-polygon matching, not containment. This
spike checks whether that theory holds for our specific test coordinate.
"""
import duckdb

RELEASE = "2026-07-22.0"
DIVISION_AREA_PATH = (
    f"s3://overturemaps-us-west-2/release/{RELEASE}/"
    "theme=divisions/type=division_area/*"
)

# The 3 SE Alaska wilderness coordinates from the reverse-geocode smoke test
# (Immich's own GeoNames-backed geocoder returned country-only, no city/state
# -- confirmed via manual map check to be real wilderness, no nearby town).
TEST_LAT, TEST_LON = 57.501833, -132.844983

# Bounding-box prefilter for the nearest-polygon query, in degrees. Avoids a
# full-table scan of Overture's global division_area dataset for every
# ST_Distance computation -- only rows whose bbox is within this margin of
# the test point are distance-checked at all. ~0.5 degrees is roughly 35-55km
# at this latitude, generously wide for finding "the nearest land."
BBOX_MARGIN_DEGREES = 0.5


def main():
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")

    print(f"--- Schema of division_area (release {RELEASE}) ---")
    schema = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{DIVISION_AREA_PATH}', hive_partitioning=1) LIMIT 1;"
    ).fetchall()
    for col_name, col_type, *_ in schema:
        print(f"  {col_name}: {col_type}")

    print("\n--- 3 sample rows (id, subtype, names, country) ---")
    samples = con.execute(
        f"""
        SELECT id, subtype, names, country
        FROM read_parquet('{DIVISION_AREA_PATH}', hive_partitioning=1)
        LIMIT 3;
        """
    ).fetchall()
    for row in samples:
        print(f"  {row}")

    print(f"\n--- Strict containment (ST_Contains) for ({TEST_LAT}, {TEST_LON}) ---")
    contains_result = con.execute(
        f"""
        SELECT id, subtype, names, country
        FROM read_parquet('{DIVISION_AREA_PATH}', hive_partitioning=1)
        WHERE ST_Contains(geometry, ST_Point({TEST_LON}, {TEST_LAT}))
        ORDER BY CASE subtype
            WHEN 'locality' THEN 1
            WHEN 'county' THEN 2
            WHEN 'region' THEN 3
            WHEN 'country' THEN 4
            ELSE 5
        END;
        """
    ).fetchall()
    if contains_result:
        for row in contains_result:
            print(f"  MATCH: {row}")
    else:
        print("  No containing division found at any level (as before -- likely a water point).")

    print(f"\n--- Nearest polygon (ST_Distance, bbox-prefiltered) for ({TEST_LAT}, {TEST_LON}) ---")
    # Distance is in degrees (the geometry CRS is plain lon/lat, not
    # projected) -- approximate meters shown for readability only, using a
    # rough 111km/degree conversion. Good enough to judge "is this point
    # just offshore" vs. "something else is wrong"; not precise enough for
    # the real enrichment tool, which should use a proper spheroid distance
    # or reproject before trusting exact numbers.
    nearest_result = con.execute(
        f"""
        SELECT id, subtype, names.primary AS name, country,
               ST_Distance(geometry, ST_Point({TEST_LON}, {TEST_LAT})) AS degree_dist
        FROM read_parquet('{DIVISION_AREA_PATH}', hive_partitioning=1)
        WHERE bbox.xmin <= {TEST_LON} + {BBOX_MARGIN_DEGREES}
          AND bbox.xmax >= {TEST_LON} - {BBOX_MARGIN_DEGREES}
          AND bbox.ymin <= {TEST_LAT} + {BBOX_MARGIN_DEGREES}
          AND bbox.ymax >= {TEST_LAT} - {BBOX_MARGIN_DEGREES}
        ORDER BY degree_dist
        LIMIT 5;
        """
    ).fetchall()
    if nearest_result:
        for row in nearest_result:
            approx_km = row[-1] * 111
            print(f"  {row}  (~{approx_km:.1f} km)")
    else:
        print(f"  Nothing found within {BBOX_MARGIN_DEGREES} degrees either -- "
              f"widen BBOX_MARGIN_DEGREES and rerun, or something else is off.")


if __name__ == "__main__":
    main()
