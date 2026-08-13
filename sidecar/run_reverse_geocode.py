"""
One-off / cron entry point for the reverse-geocode enrichment. Run inside
search-api-dev (has IMMICH_URL, IMMICH_DB_DSN, SIDECAR_DB_DSN, and /app on
PYTHONPATH). Must be run as a module (-m), from /app (the container's
WORKDIR), NOT as a bare script -- sidecar/ is a package now (see
sidecar/__init__.py), and its internal imports (config, db) are relative,
so they only resolve correctly when Python treats sidecar/ as a package
rather than putting it directly on sys.path:

    docker exec -it <search-api-dev container> python -m sidecar.run_reverse_geocode --limit 5

Start with a small --limit for the first smoke test before running unbounded.
"""
import argparse
import logging

from immich_client import ImmichClient
from .enrichment import reverse_geocode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                         help="Max photos to process this run (omit for no limit)")
    parser.add_argument("--no-skip-done", action="store_true",
                         help="Reprocess photos already marked done for this tool/model_version")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = ImmichClient()
    processed = reverse_geocode.run(client, limit=args.limit, skip_done=not args.no_skip_done)
    print(f"Done. {processed} photo(s) processed.")


if __name__ == "__main__":
    main()
