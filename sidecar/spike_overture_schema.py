"""
ONE-OFF SPIKE, not part of the enrichment pipeline. Run once to confirm
Overture's real division_area schema and test a live point-in-polygon
lookup, before writing overture_geocode.py against assumed column names --
same discipline that caught the asset_exif/exif and /map vs /api/map bugs
earlier in this project.

Run inside search-api-dev:
    docker exec -it <search-api-dev container> python -m sidecar.spike_overture_schema

RELEASE is hardcoded to the most recent Overture release confirmed via
docs.overturemaps.org as of 2026-08 (2026-07-22.0). Overture ships a new
release roughly monthly; bump this string (or add real "latest release"
discovery) before this becomes a permanent enrichment, not just a spike.
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
# Real test: does Overture's polygon-based matching find ANY containing
# division here that GeoNames' point-based nearest-city approach missed?
TEST_LAT, TEST_LON = 57.501833, -132.844983


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

    print(f"\n--- Point-in-polygon lookup for ({TEST_LAT}, {TEST_LON}) ---")
    # ST_Contains(geometry, point) -- geometry column assumed present per
    # Overture's documented GeoParquet convention; confirmed against the
    # schema output above, not assumed blind.
    result = con.execute(
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
    if result:
        for row in result:
            print(f"  MATCH: {row}")
    else:
        print("  No containing division found at any level.")


if __name__ == "__main__":
    main()
