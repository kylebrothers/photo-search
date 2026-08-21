"""
One-off / cron entry point for the Overture Places landmark-proximity
enrichment. Run inside search-api-dev, as a module from /app (same
reasoning as the other enrichment entry points -- sidecar/ is a package
with relative internal imports):

    docker exec -it <search-api-dev container> python -m sidecar.run_overture_landmarks --scope test

--scope defaults to 'test' (the pinned ~100-photo test_set) -- running
against the full library is a deliberate, explicit choice:

    docker exec -it <search-api-dev container> python -m sidecar.run_overture_landmarks --scope full

No external service call needed -- queries Overture's open dataset
directly via DuckDB, same as run_overture_geocode.py.
"""
import argparse
import logging

from .enrichment import overture_landmarks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=["test", "full"], default="test",
                         help="'test' = pinned test_set only (default), 'full' = entire library")
    parser.add_argument("--no-skip-done", action="store_true",
                         help="Reprocess photos already marked done for this tool/model_version")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    processed = overture_landmarks.run(scope=args.scope, skip_done=not args.no_skip_done)
    print(f"Done. {processed} photo(s) processed.")


if __name__ == "__main__":
    main()
