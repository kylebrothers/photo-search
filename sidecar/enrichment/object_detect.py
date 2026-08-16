"""
Fills sidecar.object_counts by calling gpu-ml's generic inference service
(POST /v1/infer/object_detect) with a resized copy of each photo's full-
quality original. See gpu-ml/inference-service/tasks/object_detect.py for
the YOLO-World model/vocabulary-caching side of this.

Unlike the geocoding enrichments, candidates are ALL image assets, not just
ones with coordinates -- object detection doesn't depend on location data.

Image sizing: sends the real original (via ImmichClient.original_stream()),
downscaled only if its longest edge exceeds MAX_LONG_EDGE. Decision basis
(2026-08): the LAN link to gpu-ml is fast and, once the existing backlog is
processed, new-photo volume is a small daily trickle -- so bandwidth isn't
the constraint. The real ceiling is what YOLO-family models actually use
internally: they resize/letterbox every input to a fixed internal
resolution (roughly 640-1280px) before inference regardless of input size,
so sending more pixels than that gains nothing. 2048px was chosen as
comfortably above that range (no accuracy lost) while still cutting a
24MP+ phone photo down substantially before it goes over the network.

UNVERIFIED, flag for real-world testing: whether HEIC or other formats
present in the library open cleanly via PIL without an extra plugin
(pillow-heif). If Image.open() fails on a real photo, that will surface as
a 'failed' enrichment_status row with the real error in error_detail --
not a silent skip -- so the gap will be visible, not hidden.
"""
import io
import json
import logging

import psycopg2
import requests
from PIL import Image

from .. import config
from .. import db as sidecar_db
from .. import test_set

logger = logging.getLogger(__name__)

TOOL = "object_detect"

# MUST match gpu-ml/inference-service/tasks/object_detect.py's MODEL_WEIGHTS
# constant exactly. Duplicated deliberately (a small, rarely-changed
# string) rather than discovered at runtime via GET /v1/tasks -- this way
# find_unresolved()/_already_done() can determine the current
# model_version WITHOUT a network round trip. A drift check in run()
# verifies gpu-ml's actual response agrees; a mismatch means the two repos
# fell out of sync and gets logged loudly, not silently accepted.
MODEL_WEIGHTS = "yolov8s-worldv2.pt"

MAX_LONG_EDGE = 2048
DEFAULT_CONFIDENCE = 0.25


def current_model_version():
    return f"{MODEL_WEIGHTS}:{config.OBJECT_DETECT_VOCABULARY_VERSION}"


def _get_immich_connection():
    return psycopg2.connect(**config.immich_db_kwargs())


def find_unresolved(scope="test"):
    """
    Candidates: real, visible, non-deleted image assets (same filter as
    populate_test_set.pick_random() -- deletedAt IS NULL, visibility=
    'timeline', isOffline=false, type='IMAGE') with no 'done'
    enrichment_status row yet for this tool/model_version. No coordinate
    requirement, unlike the geocoding enrichments.

    scope='test' (default): only the pinned test_set. scope='full': the
    entire library -- must be explicit (see run_object_detect.py --scope).
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

    model_version = current_model_version()
    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asset_id FROM enrichment_status "
                "WHERE tool = %s AND model_version = %s AND status = 'done';",
                (TOOL, model_version),
            )
            already_done = {row[0] for row in cur.fetchall()}

    return [aid for aid in all_candidates if aid not in already_done]


def _prepare_image_bytes(raw_bytes):
    """Downscale to MAX_LONG_EDGE only if needed; re-encode as JPEG for a
    consistent, small upload regardless of the original's format."""
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    width, height = img.size
    long_edge = max(width, height)
    if long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        img = img.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _call_inference_service(image_bytes):
    """
    POST to gpu-ml's generic /v1/infer/object_detect. Sends the full
    vocabulary + version every call (cheap -- a JSON list, not the model
    itself); the service caches whether it actually needs to re-apply it
    (see tasks/object_detect.py's set_classes() caching, added specifically
    to dodge a documented Ultralytics bug on repeated set_classes() calls).
    """
    params = {
        "vocabulary": config.object_detect_class_list(),
        "vocabulary_version": config.OBJECT_DETECT_VOCABULARY_VERSION,
        "confidence": DEFAULT_CONFIDENCE,
    }
    response = requests.post(
        f"{config.INFERENCE_SERVICE_URL}/v1/infer/object_detect",
        files={"image": ("photo.jpg", image_bytes, "image/jpeg")},
        data={"params": json.dumps(params)},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def _already_done(asset_id, model_version):
    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM enrichment_status "
                "WHERE asset_id = %s AND tool = %s AND model_version = %s "
                "AND status = 'done';",
                (asset_id, TOOL, model_version),
            )
            return cur.fetchone() is not None


def _write_result(asset_id, class_counts, model_version, error=None):
    """
    class_counts: {class_name: {"count": int, "avg_confidence": float}, ...}
    or None on error. model_version: ALWAYS the locally-computed
    current_model_version() -- never gpu-ml's raw response string directly
    -- so every row written here is guaranteed queryable by
    find_unresolved()/_already_done(), which use the same function. Writes
    one object_counts row per detected class, plus one enrichment_status
    row unconditionally (even for zero detections -- a real, correct
    result, distinguished from "never processed" by this row's mere
    existence, same pattern as the geocoding enrichments).
    """
    with sidecar_db.get_connection() as conn:
        with conn.cursor() as cur:
            if error is None:
                for cls_name, stats in class_counts.items():
                    cur.execute(
                        "INSERT INTO object_counts "
                        "(asset_id, class, count, avg_confidence, model_version) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (asset_id, class, model_version) DO UPDATE SET "
                        "count = EXCLUDED.count, avg_confidence = EXCLUDED.avg_confidence, "
                        "computed_at = now();",
                        (asset_id, cls_name, stats["count"], stats["avg_confidence"], model_version),
                    )
                cur.execute(
                    "INSERT INTO enrichment_status "
                    "(asset_id, tool, model_version, status) VALUES (%s, %s, %s, 'done') "
                    "ON CONFLICT (asset_id, tool, model_version) "
                    "DO UPDATE SET status = 'done', error_detail = NULL, computed_at = now();",
                    (asset_id, TOOL, model_version),
                )
            else:
                cur.execute(
                    "INSERT INTO enrichment_status "
                    "(asset_id, tool, model_version, status, error_detail) "
                    "VALUES (%s, %s, %s, 'failed', %s) "
                    "ON CONFLICT (asset_id, tool, model_version) "
                    "DO UPDATE SET status = 'failed', error_detail = EXCLUDED.error_detail, "
                    "computed_at = now();",
                    (asset_id, TOOL, model_version, str(error)),
                )
        conn.commit()


def run(immich_client, scope="test", skip_done=True):
    """
    Main entry point. immich_client: an ImmichClient instance (uses
    .original_stream(asset_id) to fetch the real full-quality image).

    scope: 'test' (pinned test_set, default) or 'full' (entire library).
    skip_done: skip asset_ids already marked 'done' for the CURRENT
    vocabulary version specifically (a vocabulary bump makes previously
    'done' photos eligible again under the new model_version).
    """
    model_version = current_model_version()
    candidates = find_unresolved(scope=scope)
    logger.info(f"object_detect: {len(candidates)} candidate photo(s) found (scope={scope})")

    processed = 0
    for asset_id in candidates:
        if skip_done and _already_done(asset_id, model_version):
            continue
        try:
            raw = immich_client.original_stream(asset_id).content
            prepared = _prepare_image_bytes(raw)
            response = _call_inference_service(prepared)

            if response.get("model_version") != model_version:
                logger.warning(
                    f"object_detect: model_version mismatch -- expected "
                    f"{model_version!r}, gpu-ml returned "
                    f"{response.get('model_version')!r}. sidecar's MODEL_WEIGHTS "
                    f"constant may be out of sync with gpu-ml's; results are "
                    f"still stored under the LOCAL version string for query "
                    f"consistency, but this should be investigated."
                )

            _write_result(asset_id, response["result"], model_version)
            logger.info(f"object_detect: {asset_id} -> {response['result']}")
        except Exception as e:
            logger.warning(f"object_detect: {asset_id} failed: {e}")
            _write_result(asset_id, class_counts=None, model_version=model_version, error=e)
        processed += 1

    logger.info(f"object_detect: {processed} photo(s) processed")
    return processed
