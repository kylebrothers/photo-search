"""
One-off / cron entry point for the Overture Divisions geocode enrichment.
Run inside search-api-dev, as a module from /app (same reasoning as
run_reverse_geocode.py -- sidecar/ is a package with relative internal
imports):

    docker exec -it <search-api-dev container> python -m sidecar.run_overture_geocode --scope test

--scope defaults to 'test' (the pinned ~100-photo test_set) -- running
against the full library is a deliberate, explicit choice:

    docker exec -it <search-api-dev container> python -m sidecar.run_overture_geocode --scope full

No Immich API client needed here (unlike run_reverse_geocode.py) -- this
queries Overture's open dataset directly via DuckDB.
"""
import argparse
import logging

from .enrichment import overture_geocode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=["test", "full"], default="test",
                         help="'test' = pinned test_set only (default), 'full' = entire library")
    parser.add_argument("--no-skip-done", action="store_true",
                         help="Reprocess photos already marked done for this tool/model_version")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    processed = overture_geocode.run(scope=args.scope, skip_done=not args.no_skip_done)
    print(f"Done. {processed} photo(s) processed.")


if __name__ == "__main__":
    main()
