"""
Diagnostic report, NOT a formal enrichment tool (writes no sidecar DB rows).
Finds photos with GPS coordinates inside Walt Disney World, runs each
through gpu-ml's match_landmark task, and renders a single self-contained
HTML file: thumbnail + matched landmark name + similarity score (3 decimal
places) + a link back to the photo in Immich. Built to visually calibrate
"how much of a landmark needs to be in frame for a real match" -- see
docs/sidecar-augmentation.md, "Landmark matching -- DINOv3 implementation
progress."

min_similarity is passed through to match_landmark (see
gpu-ml/inference-service/tasks/match_landmark.py's infer() params) rather
than hardcoded, so a lower value can be used here specifically to inspect
weak/borderline matches that the task's own 0.7 default would otherwise
filter out entirely -- see build_report()'s min_similarity argument.

Landmark_id -> name resolution now lives in sidecar/landmark_labels.py,
shared with sidecar/enrichment/dinov3_landmarks.py (the real enrichment
client) -- factored out specifically so there aren't two copies of "how to
parse a display name from this CSV" that could drift apart.

Candidate photos are filtered to asset.type = 'IMAGE' (see
find_disney_photos()) -- an earlier version queried asset_exif alone,
which also pulled in videos sharing GPS coordinates with a paired photo;
PIL correctly can't open video bytes as an image, so every one of those
failed after a slow full-file download, wasting real time for zero data.
Live 2026-08 run against the real library: ~1,744 raw candidates before
this fix.

Given that raw-candidate count, --limit (default 500, see main() below)
doesn't just truncate -- it stratified-samples across the full date range
first (see _sample_spread_over_time()), so a run of any --limit still
covers the whole photo history rather than clustering around whichever
single visit happened to contribute the most photos.

Run inside search-api-dev (needs Immich Postgres + gpu-ml LAN access):
    python -m sidecar.dinov3_landmark_report

Output: sidecar/output/dinov3_disney_report.html (host-visible via the
existing ./sidecar bind mount in docker-compose.yml).
"""
import base64
import io
import json
import logging
import os
import random

import psycopg2
import requests

from . import config
from . import landmark_labels
from immich_client import ImmichClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Approximate bounding box covering the whole WDW property (Magic Kingdom,
# Epcot, Hollywood Studios, Animal Kingdom, resort hotels) -- generous
# margin, not the precise Reedy Creek administrative boundary.
DISNEY_LAT_MIN, DISNEY_LAT_MAX = 28.32, 28.44
DISNEY_LON_MIN, DISNEY_LON_MAX = -81.64, -81.50

TOP_K = 5
DEFAULT_MIN_SIMILARITY = 0.7  # matches match_landmark.py's own default -- override to inspect weaker candidates
DEFAULT_LIMIT = 500
MAX_LONG_EDGE = 1024  # for the copy sent to match_landmark, same reasoning as object_detect.py's _prepare_image_bytes

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "dinov3_disney_report.html")


def _get_immich_connection():
    return psycopg2.connect(**config.immich_db_kwargs())


def find_disney_photos():
    """
    Photos (not videos -- see module docstring) with GPS coordinates inside
    the WDW bounding box, sorted by capture date. Sorted (not just
    filtered) specifically so _sample_spread_over_time() can bucket by
    position in this list as a proxy for position in time.
    """
    query = (
        'SELECT ae."assetId", ae.latitude, ae.longitude, a."fileCreatedAt" '
        'FROM asset_exif ae '
        'JOIN asset a ON a.id = ae."assetId" '
        'WHERE ae.latitude BETWEEN %s AND %s AND ae.longitude BETWEEN %s AND %s '
        'AND a."deletedAt" IS NULL AND a.visibility = \'timeline\' '
        'AND a."isOffline" = false AND a.type = \'IMAGE\' '
        'ORDER BY a."fileCreatedAt"'
    )
    with _get_immich_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (DISNEY_LAT_MIN, DISNEY_LAT_MAX, DISNEY_LON_MIN, DISNEY_LON_MAX))
            return cur.fetchall()  # [(asset_id, lat, lon, created_at), ...], sorted by created_at


def _sample_spread_over_time(rows, limit, seed=None):
    """
    rows: assumed already sorted by capture date (see find_disney_photos()).
    Splits into `limit` equal-sized consecutive slices along that sorted
    order and picks ONE random row from each slice -- a stratified sample,
    not a plain random.sample(). Plain random sampling over a library with
    uneven visit frequency would still end up dominated by whichever visit
    contributed the most photos; bucketing by position-in-time first
    guarantees roughly even coverage across the whole date range, with
    randomness only within each narrow time slice.

    seed: optional, for a reproducible sample across reruns (e.g. to compare
    two different min_similarity values against the exact same 500 photos).
    """
    rnd = random.Random(seed)
    n = len(rows)
    if n <= limit:
        return rows
    bucket_size = n / limit
    selected = []
    for i in range(limit):
        start = int(i * bucket_size)
        end = max(int((i + 1) * bucket_size), start + 1)
        end = min(end, n)
        selected.append(rnd.choice(rows[start:end]))
    return selected


def _prepare_image_bytes(raw_bytes):
    """Downscale to MAX_LONG_EDGE if needed, re-encode as JPEG -- same
    reasoning as object_detect.py's _prepare_image_bytes."""
    from PIL import Image
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    width, height = img.size
    long_edge = max(width, height)
    if long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        img = img.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _call_match_landmark(image_bytes, min_similarity):
    response = requests.post(
        f"{config.INFERENCE_SERVICE_URL}/v1/infer/match_landmark",
        files={"image": ("photo.jpg", image_bytes, "image/jpeg")},
        data={"params": json.dumps({"top_k": TOP_K, "min_similarity": min_similarity})},
        timeout=120,  # first call lazy-loads ~2.4GB of reference embeddings
    )
    response.raise_for_status()
    return response.json()


def _html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_report(min_similarity=DEFAULT_MIN_SIMILARITY, limit=DEFAULT_LIMIT, seed=None):
    """
    min_similarity: passed through to match_landmark's own min_similarity
    param (default 0.7, same as the task's server-side default). Lower this
    to see weak/borderline matches that would otherwise come back as "no
    match".
    limit: max photos to process. If the raw candidate count exceeds this,
    stratified-samples across the full date range (see
    _sample_spread_over_time()) rather than truncating to however
    find_disney_photos() happened to order them. Pass None to process every
    candidate (slow -- ~1,744 raw candidates as of 2026-08, at highly
    variable per-photo time).
    seed: optional int, for a reproducible sample across reruns.
    """
    immich = ImmichClient()
    label_map = landmark_labels.load_label_map()

    all_candidates = find_disney_photos()
    logger.info(f"{len(all_candidates)} Disney World photo(s) found by GPS (asset.type = IMAGE)")

    if limit is not None:
        candidates = _sample_spread_over_time(all_candidates, limit, seed=seed)
        logger.info(f"sampled {len(candidates)} photo(s), spread across the full date range "
                    f"({all_candidates[0][3]} to {all_candidates[-1][3]})")
    else:
        candidates = all_candidates

    rows_html = []
    for asset_id, lat, lon, created_at in candidates:
        try:
            thumb_bytes, thumb_content_type = immich.thumbnail_response(asset_id)
            raw = immich.original_stream(asset_id).content
            prepared = _prepare_image_bytes(raw)
            result = _call_match_landmark(prepared, min_similarity)
            matches = result["result"]["matches"]
        except Exception as e:
            logger.warning(f"{asset_id} failed: {e}")
            matches = None
            thumb_bytes, thumb_content_type = None, None

        thumb_b64 = base64.b64encode(thumb_bytes).decode("ascii") if thumb_bytes else None
        view_url = immich.view_url(asset_id)

        if matches is None:
            match_html = '<span class="error">inference failed</span>'
        elif not matches:
            match_html = f'<span class="no-match">no match (below {min_similarity:.3f} threshold)</span>'
        else:
            lines = []
            for m in matches:
                name = landmark_labels.resolve_name(m["landmark_id"], label_map)
                lines.append(
                    f'<div class="match"><strong>{_html_escape(name)}</strong> '
                    f'&mdash; {m["similarity"]:.3f}</div>'
                )
            match_html = "".join(lines)

        img_tag = (
            f'<img src="data:{thumb_content_type};base64,{thumb_b64}" />'
            if thumb_b64 else '<span class="error">thumbnail unavailable</span>'
        )

        rows_html.append(f"""
        <div class="card">
          {img_tag}
          <div class="info">
            <div class="date">{created_at}</div>
            {match_html}
            <a href="{view_url}" target="_blank">open in Immich</a>
          </div>
        </div>
        """)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>DINOv3 Landmark Match -- Disney World test set</title>
<style>
  body {{ font-family: sans-serif; background: #222; color: #eee; margin: 2em; }}
  h1 {{ font-size: 1.1em; }}
  .grid {{ display: flex; flex-wrap: wrap; gap: 1em; }}
  .card {{ width: 280px; background: #333; border-radius: 8px; overflow: hidden; }}
  .card img {{ width: 100%; display: block; }}
  .info {{ padding: 0.75em; font-size: 0.9em; }}
  .date {{ color: #999; font-size: 0.8em; margin-bottom: 0.4em; }}
  .match {{ margin-bottom: 0.3em; }}
  .no-match {{ color: #888; }}
  .error {{ color: #e77; }}
  a {{ color: #7ad; }}
</style>
</head>
<body>
<h1>{len(candidates)} of {len(all_candidates)} Disney World photo(s) (sampled, spread across full date range) &mdash;
DINOv3 GLDv2 match, top {TOP_K}, min_similarity={min_similarity:.3f}</h1>
<div class="grid">
{"".join(rows_html)}
</div>
</body>
</html>"""

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-similarity", type=float, default=DEFAULT_MIN_SIMILARITY,
                         help=f"Minimum cosine similarity to count as a match (default {DEFAULT_MIN_SIMILARITY})")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                         help=f"Max photos to process, stratified-sampled across the full date range "
                              f"if the raw candidate count exceeds this (default {DEFAULT_LIMIT}). "
                              f"Pass 0 to process every candidate.")
    parser.add_argument("--seed", type=int, default=None,
                         help="Optional seed for a reproducible sample across reruns")
    args = parser.parse_args()
    build_report(
        min_similarity=args.min_similarity,
        limit=None if args.limit == 0 else args.limit,
        seed=args.seed,
    )
