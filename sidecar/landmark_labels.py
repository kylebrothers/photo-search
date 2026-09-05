"""
Shared landmark_id -> display-name lookup for GLDv2 landmark IDs. Used by
both sidecar/enrichment/dinov3_landmarks.py (the real enrichment client)
and sidecar/dinov3_landmark_report.py (the diagnostic report) -- factored
out here specifically so there is exactly one place parsing this CSV, not
two copies that could quietly drift apart (a risk flagged explicitly in
gpu-ml/inference-service/tasks/match_landmark.py's module docstring).

See docs/sidecar-augmentation.md, "Landmark matching -- DINOv3
implementation progress," point 6, for the full story on why this exists
and its real limitation: a meaningful number of GLDv2 landmark IDs have
thin, missing, or non-English category names in the dataset's own
metadata -- a data-quality gap in GLDv2 itself, not a bug here. See
"Landmark name overrides" in that doc for the planned fix (manual
correction, keyed on landmark_id, layered on top of this).
"""
import csv
import logging
import os
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

LABEL_CSV_URL = "https://s3.amazonaws.com/google-landmark/metadata/train_label_to_hierarchical.csv"
# Same NAS folder gpu-ml/inference-service mounts read-only for the
# reference embeddings (see gpu-ml/docker-compose.yml's
# gpu_ml_landmark_embeddings volume), mounted read-write here (see this
# project's own docker-compose.yml). Stored here rather than re-downloaded
# from S3 on every use -- s3.amazonaws.com/google-landmark isn't ours to
# rely on staying reachable indefinitely, and this way the label mapping
# lives in the same durable, single NAS location as the reference
# embeddings themselves.
LABEL_CSV_PATH = "/reference-embeddings/train_label_to_hierarchical.csv"


def ensure_label_csv():
    """Download the CSV onto the shared NAS folder, only if it isn't
    already there -- one-time per NAS, not per container/run. Requires the
    gpu_ml_landmark_embeddings mount to be writable from this host; if the
    NAS export is locked read-only to gpu-ml specifically, this write will
    fail -- the fix is on the NAS export config, not here."""
    if os.path.exists(LABEL_CSV_PATH):
        return
    logger.info(f"downloading landmark label mapping to {LABEL_CSV_PATH} (one-time)...")
    urllib.request.urlretrieve(LABEL_CSV_URL, LABEL_CSV_PATH)


def load_label_map():
    """
    Returns {landmark_id: display_name}. display_name is derived from the
    Wikimedia category URL's trailing path segment (e.g.
    ".../Category:Parvis_Notre-Dame_-_place_Jean-Paul-II" ->
    "Parvis Notre-Dame - place Jean-Paul-II") -- a rough parse, not a
    curated name; expect some odd-looking results. IDs missing from the
    CSV, or with an empty/unparseable category, are simply absent from the
    returned dict -- callers should use resolve_name() below rather than
    assuming every ID resolves.
    """
    ensure_label_csv()
    label_map = {}
    with open(LABEL_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            category_url = row.get("category", "")
            segment = category_url.rsplit(":", 1)[-1] if ":" in category_url else category_url
            name = urllib.parse.unquote(segment).replace("_", " ").strip()
            if name:
                label_map[row["landmark_id"]] = name
    logger.info(f"loaded {len(label_map)} landmark labels")
    return label_map


def resolve_name(landmark_id, label_map):
    """Standard fallback for an ID with no usable name in label_map -- see
    module docstring for why this happens for a real, non-trivial fraction
    of GLDv2's landmark IDs."""
    return label_map.get(landmark_id, f"landmark {landmark_id}")
