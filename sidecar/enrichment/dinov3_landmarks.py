"""
Fills sidecar.landmark_matches with visually-recognized landmarks, via
gpu-ml's match_landmark task (DINOv3 embedding + cosine similarity against
the GLDv2-clean reference set) -- the visual-recognition half of the
dual-source landmark design (see docs/sidecar-augmentation.md, "Landmark
matching"). Complementary to overture_landmarks.py (geospatial proximity),
not a replacement -- both should be queried by the search agent.

Candidate scope (agreed in docs/sidecar-augmentation.md, "Next steps" #4):
ALL real image assets EXCEPT ones overture_landmarks.py already found a
proximity match for (source='overture_places' in landmark_matches) -- this
single condition naturally covers both no-coordinate photos
(overture_landmarks never runs on those at all) and coordinate photos
where proximity search came up empty, without needing two separate query
branches: both cases just mean "no overture_places row exists for this
asset."

Real findings from a 500-photo real-data test (see
sidecar/dinov3_landmark_report.py, docs/sidecar-augmentation.md point 7):
min_similarity=0.7 (DEFAULT_MIN_SIMILARITY below) is a good cutoff -- real
negatives mostly score below it, real positives mostly above -- and real
high-confidence correct matches were found. A separate, real gap: a
meaningful number of correct matches resolve to a bare "landmark <id>"
fallback name because GLDv2's own metadata is thin for that ID -- see
sidecar/landmark_labels.py and "Landmark name overrides" in the design doc
for the planned fix. That's a naming problem, not a matching-accuracy
problem, and doesn't block writing these rows now.

landmark_id is stored alongside landmark_name (see ensure_schema.py's
landmark_matches.landmark_id column, added specifically for this) --
deliberately not relying on landmark_name alone, since name is exactly what
"Landmark name overrides" is meant to let a user manually correct later,
while landmark_id is GLDv2's own stable identifier and won't change under
an override.
"""
import io
import json
import logging

import psycopg2
import requests
from PIL import Image

from .. import config
from .. import db as sidecar_db
from .. import landmark_labels
from .. import test_set

logger = logging.getLogger(__name__)

TOOL = "dinov3_landmarks"

# MUST match gpu-ml/inference-service/tasks/embed_image.py's underlying
# model exactly -- duplicated deliberately (a small, rarely-changed
# string), same reasoning as object_detect.py's MODEL_WEIGHTS constant. A
# drift check in run() verifies gpu-ml's actual response agrees.
DINOV3_MODEL_ID = "facebook/dinov3-vits16plus-pretrain-lvd1689m"
MODEL_VERSION = f"match_landmark+{DINOV3_MODEL_ID}"

DEFAULT_MIN_SIMILARITY = 0.7  # confirmed good cutoff against real data, see module docstring
# 1, not match_landmark's own top_k=5 default used by the diagnostic
# report -- for a STORED enrichment row, the single best visual match is
# what's meaningful; storing 5 candidates per photo (most of them noise
# below the real landmark) would just clutter landmark_matches.
TOP_K = 1
MAX_LONG_EDGE = 1024  # matches dinov3_landmark_report.py's choice


def _get_immich_connection():
    return psycopg2.connect(**config.immich_db_kwargs())


def find_unresolved(scope="test"):
    """
    Candidates: real, visible, non-deleted image assets (same base filter
    as object_detect.py's find_unresolved()) MINUS any asset that already
    has an overture_places landmark_matches row -- see module docstring for
    why this single condition covers both no-coordinate and
    zero-proximity-match photos.

    scope='test' (default): only the pinned test_set. scope='full': the
    entire library -- must be explicit (see run_dinov3_landmarks.py --scope).
    """
    query = (
        'SELECT id FROM asset '
        'WHERE "deletedAt" IS NULL AND visibility = \'timeline\' '
        'AND "isOffline" = false AND type = \'IMAGE\''
    )
    params = ()

    if scope == "test":
        test_asset_ids = [row[0] for row in test_set.get()]
        if not test_asset_ids:
            return []
        query += ' AND id = ANY(%s::uuid[])'
        params = (test_asset_ids,)
    elif scope != "full":
        raise ValueError(f"scope must be 'test' or 'full', got {scope!r}")

    with _get_immich_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            all_candidates = [row[0] for row in cur.fetchall()]

    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT asset_id FROM landmark_matches WHERE source = 'overture_places';"
            )
            covered_by_proximity = {row[0] for row in cur.fetchall()}

    return [aid for aid in all_candidates if aid not in covered_by_proximity]


def _already_done(asset_id):
    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM enrichment_status "
                "WHERE asset_id = %s AND tool = %s AND model_version = %s "
                "AND status = 'done';",
                (asset_id, TOOL, MODEL_VERSION),
            )
            return cur.fetchone() is not None


def _prepare_image_bytes(raw_bytes):
    """Downscale to MAX_LONG_EDGE if needed, re-encode as JPEG -- same
    reasoning as object_detect.py's _prepare_image_bytes."""
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    width, height = img.size
    long_edge = max(width, height)
    if long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        img = img.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _call_match_landmark(image_bytes):
    response = requests.post(
        f"{config.INFERENCE_SERVICE_URL}/v1/infer/match_landmark",
        files={"image": ("photo.jpg", image_bytes, "image/jpeg")},
        data={"params": json.dumps({"top_k": TOP_K, "min_similarity": DEFAULT_MIN_SIMILARITY})},
        timeout=120,  # first call on a fresh container lazy-loads ~2.4GB of reference embeddings
    )
    response.raise_for_status()
    return response.json()


def _write_result(asset_id, matches, label_map, error=None):
    """
    matches: list of {landmark_id, similarity} dicts (from match_landmark's
    response), or None on error. label_map: from
    landmark_labels.load_label_map(), passed in rather than reloaded per
    photo -- it's a real file read + CSV parse over ~129K rows, not free.
    """
    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            if error is None:
                for m in matches:
                    landmark_id = m["landmark_id"]
                    name = landmark_labels.resolve_name(landmark_id, label_map)
                    cur.execute(
                        "INSERT INTO landmark_matches "
                        "(asset_id, landmark_id, landmark_name, confidence, distance_meters, "
                        "source, model_version) "
                        "VALUES (%s, %s, %s, %s, NULL, %s, %s) "
                        "ON CONFLICT (asset_id, landmark_name, model_version) DO UPDATE SET "
                        "landmark_id = EXCLUDED.landmark_id, "
                        "confidence = EXCLUDED.confidence, "
                        "computed_at = now();",
                        (asset_id, landmark_id, name, m["similarity"], "dinov3_visual", MODEL_VERSION),
                    )
                cur.execute(
                    "INSERT INTO enrichment_status "
                    "(asset_id, tool, model_version, status) VALUES (%s, %s, %s, 'done') "
                    "ON CONFLICT (asset_id, tool, model_version) "
                    "DO UPDATE SET status = 'done', error_detail = NULL, computed_at = now();",
                    (asset_id, TOOL, MODEL_VERSION),
                )
            else:
                cur.execute(
                    "INSERT INTO enrichment_status "
                    "(asset_id, tool, model_version, status, error_detail) "
                    "VALUES (%s, %s, %s, 'failed', %s) "
                    "ON CONFLICT (asset_id, tool, model_version) "
                    "DO UPDATE SET status = 'failed', error_detail = EXCLUDED.error_detail, "
                    "computed_at = now();",
                    (asset_id, TOOL, MODEL_VERSION, str(error)),
                )
        conn.commit()


def run(immich_client, scope="test", skip_done=True):
    """
    Main entry point. immich_client: an ImmichClient instance (uses
    .original_stream(asset_id) to fetch the real full-quality image).

    scope: 'test' (pinned test_set, default) or 'full' (entire library).
    skip_done: skip asset_ids already marked 'done' for this tool/
    model_version ('failed' rows are retried).
    """
    candidates = find_unresolved(scope=scope)
    logger.info(f"dinov3_landmarks: {len(candidates)} candidate photo(s) found (scope={scope})")

    label_map = landmark_labels.load_label_map()

    processed = 0
    for asset_id in candidates:
        if skip_done and _already_done(asset_id):
            continue
        try:
            raw = immich_client.original_stream(asset_id).content
            prepared = _prepare_image_bytes(raw)
            response = _call_match_landmark(prepared)

            if response.get("model_version") != MODEL_VERSION:
                logger.warning(
                    f"dinov3_landmarks: model_version mismatch -- expected "
                    f"{MODEL_VERSION!r}, gpu-ml returned "
                    f"{response.get('model_version')!r}. DINOV3_MODEL_ID may be "
                    f"out of sync with gpu-ml's actual model; results are still "
                    f"stored under the LOCAL version string for query "
                    f"consistency, but this should be investigated."
                )

            matches = response["result"]["matches"]
            _write_result(asset_id, matches, label_map)
            logger.info(f"dinov3_landmarks: {asset_id} -> {matches}")
        except Exception as e:
            logger.warning(f"dinov3_landmarks: {asset_id} failed: {e}")
            _write_result(asset_id, matches=None, label_map=label_map, error=e)
        processed += 1

    logger.info(f"dinov3_landmarks: {processed} photo(s) processed")
    return processed
