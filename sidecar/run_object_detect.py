"""
One-off / cron entry point for the object-detection enrichment. Run inside
search-api-dev, as a module from /app (same reasoning as the geocode entry
points -- sidecar/ is a package with relative internal imports):

    docker exec -it <search-api-dev container> python -m sidecar.run_object_detect --scope test

--scope defaults to 'test' (the pinned ~100-photo test_set) -- running
against the full library is a deliberate, explicit choice:

    docker exec -it <search-api-dev container> python -m sidecar.run_object_detect --scope full

Requires INFERENCE_SERVICE_URL pointing at gpu-ml's inference-service
(see sidecar/config.py) and gpu-ml's inference-service container running.
"""
import argparse
import logging

from immich_client import ImmichClient
from .enrichment import object_detect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=["test", "full"], default="test",
                         help="'test' = pinned test_set only (default), 'full' = entire library")
    parser.add_argument("--no-skip-done", action="store_true",
                         help="Reprocess photos already marked done for this tool/model_version")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = ImmichClient()
    processed = object_detect.run(client, scope=args.scope, skip_done=not args.no_skip_done)
    print(f"Done. {processed} photo(s) processed.")


if __name__ == "__main__":
    main()
