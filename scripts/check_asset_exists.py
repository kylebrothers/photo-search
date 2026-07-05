#!/usr/bin/env python3
"""
Standalone existence-check utility — same check search-api's /proxy/download
route performs before serving a file (see README, point 2), exposed here for
manual/ad-hoc use: e.g. spot-checking whether specific asset IDs are still
reachable without going through the web UI.

Usage:
    IMMICH_URL=http://localhost:2283 IMMICH_API_KEY=... ./check_asset_exists.py <asset_id> [asset_id ...]

Exit codes: 0 = all exist, 1 = at least one missing, 2 = a request error occurred.
"""
import os
import sys
import requests


def asset_exists(base_url, api_key, asset_id):
    r = requests.get(
        f"{base_url}/api/assets/{asset_id}",
        headers={"x-api-key": api_key},
    )
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    r.raise_for_status()


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <asset_id> [asset_id ...]")
        sys.exit(1)

    base_url = os.environ.get("IMMICH_URL")
    api_key = os.environ.get("IMMICH_API_KEY")
    if not base_url or not api_key:
        print("Set IMMICH_URL and IMMICH_API_KEY environment variables.")
        sys.exit(1)

    exit_code = 0
    for asset_id in sys.argv[1:]:
        try:
            exists = asset_exists(base_url, api_key, asset_id)
        except requests.HTTPError as e:
            print(f"{asset_id}: ERROR ({e})")
            exit_code = 2
            continue
        print(f"{asset_id}: {'exists' if exists else 'MISSING'}")
        if not exists:
            exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
