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

The landmark_id -> name CSV lives on the NAS (LABEL_CSV_PATH, below), not
downloaded fresh from s3.amazonaws.com on every run -- see LABEL_CSV_PATH's
comment for why.

Run inside search-api-dev (needs Immich Postgres + gpu-ml LAN access):
    python -m sidecar.dinov3_landmark_report

Output: sidecar/output/dinov3_disney_report.html (host-visible via the
existing ./sidecar bind mount in docker-compose.yml).
"""
import base64
import csv
import io
import json
import logging
import os
import urllib.parse
import urllib.request

import psycopg2
import requests

from . import config
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
MAX_LONG_EDGE = 1024  # for the copy sent to match_landmark, same reasoning as object_detect.py's _prepare_image_bytes

LABEL_CSV_URL = "https://s3.amazonaws.com/google-landmark/metadata/train_label_to_hierarchical.csv"
# Same NAS folder gpu-ml/inference-service mounts read-only for the
# reference embeddings (see gpu-ml/docker-compose.yml's
# gpu_ml_landmark_embeddings volume), mounted read-write here (see this
# project's own docker-compose.yml). Stored here rather than re-downloaded
# from S3 every run -- s3.amazonaws.com/google-landmark isn't ours to rely
# on staying reachable indefinitely, and this way the label mapping lives
# in the same durable, single NAS location as the reference embeddings
# themselves, not a second copy in a container-local cache that
# disappears on rebuild.
LABEL_CSV_PATH = "/reference-embeddings/train_label_to_hierarchical.csv"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "dinov3_disney_report.html")


def _get_immich_connection():
    return psycopg2.connect(**config.immich_db_kwargs())


def find_disney_photos():
    """Photos with GPS coordinates inside the WDW bounding box."""
    query = (
        'SELECT "assetId", latitude, longitude FROM asset_exif '
        "WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s"
    )
    with _get_immich_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (DISNEY_LAT_MIN, DISNEY_LAT_MAX, DISNEY_LON_MIN, DISNEY_LON_MAX))
            return cur.fetchall()  # [(asset_id, lat, lon), ...]


def _ensure_label_csv():
    """Download the GLDv2 landmark_id -> Wikimedia category mapping onto
    the shared NAS folder, but only if it isn't already there -- one-time
    per NAS, not per container/run. Requires the gpu_ml_landmark_embeddings
    mount to be writable from this host (see docker-compose.yml's comment
    on that volume) -- if the NAS export is locked read-only to gpu-ml
    specifically, this write will fail; the fix is on the NAS export config,
    not here."""
    if os.path.exists(LABEL_CSV_PATH):
        return
    logger.info(f"downloading landmark label mapping to {LABEL_CSV_PATH} (one-time)...")
    urllib.request.urlretrieve(LABEL_CSV_URL, LABEL_CSV_PATH)


def _load_label_map():
    """
    Returns {landmark_id: display_name}. display_name is derived from the
    Wikimedia category URL's trailing path segment (e.g.
    ".../Category:Parvis_Notre-Dame_-_place_Jean-Paul-II" ->
    "Parvis Notre-Dame - place Jean-Paul-II") -- a rough parse, not a
    curated name; expect some odd-looking results.
    """
    _ensure_label_csv()
    label_map = {}
    with open(LABEL_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            category_url = row.get("category", "")
            segment = category_url.rsplit(":", 1)[-1] if ":" in category_url else category_url
            name = urllib.parse.unquote(segment).replace("_", " ").strip()
            label_map[row["landmark_id"]] = name or f"landmark {row['landmark_id']}"
    logger.info(f"loaded {len(label_map)} landmark labels")
    return label_map


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


def build_report(min_similarity=DEFAULT_MIN_SIMILARITY):
    """
    min_similarity: passed through to match_landmark's own min_similarity
    param (default 0.7, same as the task's server-side default). Lower this
    to see weak/borderline matches that would otherwise come back as "no
    match" -- e.g. build_report(min_similarity=0.4) to inspect everything
    down to a much looser threshold.
    """
    immich = ImmichClient()
    label_map = _load_label_map()

    candidates = find_disney_photos()
    logger.info(f"{len(candidates)} Disney World photo(s) found by GPS")

    rows_html = []
    for asset_id, lat, lon in candidates:
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
                name = label_map.get(m["landmark_id"], f"landmark {m['landmark_id']}")
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
  h1 {{ font-size: 1.2em; }}
  .grid {{ display: flex; flex-wrap: wrap; gap: 1em; }}
  .card {{ width: 280px; background: #333; border-radius: 8px; overflow: hidden; }}
  .card img {{ width: 100%; display: block; }}
  .info {{ padding: 0.75em; font-size: 0.9em; }}
  .match {{ margin-bottom: 0.3em; }}
  .no-match {{ color: #888; }}
  .error {{ color: #e77; }}
  a {{ color: #7ad; }}
</style>
</head>
<body>
<h1>{len(candidates)} Disney World photo(s), matched against DINOv3 GLDv2 reference set (top {TOP_K}, min_similarity={min_similarity:.3f})</h1>
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
    args = parser.parse_args()
    build_report(min_similarity=args.min_similarity)
