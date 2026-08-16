import os

# Shared Postgres connection parameters -- one physical Postgres instance
# hosts both Immich's own database and the sidecar databases. Kept as
# individual host/port/user/password components rather than a single DSN
# string: DSN strings require percent-encoding special characters in the
# password, and the real password here contains characters (%, !) that broke
# a plain DSN string on first real use (psycopg2 tried to parse "%C" as a
# percent-encoded escape). psycopg2.connect(**kwargs) handles special
# characters in passwords natively, with no encoding step required, ever.
DB_HOST = os.environ.get("DB_HOST", "postgres")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

# Database names on that shared instance.
IMMICH_DB_NAME = os.environ.get("IMMICH_DB_NAME", "immich")
SIDECAR_DB_NAME = os.environ.get("SIDECAR_DB_NAME", "sidecar_dev")

# Row cap for ad-hoc reads against the sidecar (mirrors search-api's
# SQL_ROW_CAP pattern) -- not yet wired to a query tool, kept for when the
# sidecar is exposed to the search agent.
SIDECAR_ROW_CAP = int(os.environ.get("SIDECAR_ROW_CAP", "100"))

# gpu-ml's generic inference service (see gpu-ml/inference-service). Points
# at the gpu-ml device's actual LAN address -- set the real value in this
# container's .env, not here (mirrors the IMMICH_MACHINE_LEARNING_URL
# pattern already used for that same device elsewhere in this project).
INFERENCE_SERVICE_URL = os.environ.get("INFERENCE_SERVICE_URL", "")


def sidecar_db_kwargs():
    """psycopg2.connect(**kwargs) for the sidecar database."""
    return dict(host=DB_HOST, port=DB_PORT, user=DB_USER,
                password=DB_PASSWORD, dbname=SIDECAR_DB_NAME)


def immich_db_kwargs():
    """psycopg2.connect(**kwargs) for Immich's own database (read-only use)."""
    return dict(host=DB_HOST, port=DB_PORT, user=DB_USER,
                password=DB_PASSWORD, dbname=IMMICH_DB_NAME)


# --- Object-detection vocabulary (YOLO-World enrichment) ---
#
# Lives here, not on the gpu-ml inference side, so it's easy to edit/version
# without touching the inference service itself (decision made 2026-08).
# YOLO-World is open-vocabulary/prompt-driven, so this list IS the model's
# effective class set -- no COCO-style fixed taxonomy involved.
#
# Deliberately broad rather than guessed from anticipated search queries
# (the person's own judgment, 2026-08): narrow prediction of future searches
# was judged less reliable than just covering plausible categories broadly.
# Focus is objects CLIP-based smart_search is structurally weak on --
# EXACT COUNTS, ABSENCE/NEGATION queries ("dogs and no people"), and small/
# incidental objects that don't dominate a holistic image embedding -- not
# re-detecting things smart_search already finds fine.
#
# Bumping OBJECT_DETECT_VOCABULARY_VERSION on any real edit to the list
# below is REQUIRED -- object_counts' primary key includes model_version
# (tool + this version string), so a vocabulary change without a version
# bump would silently mix results from different vocabularies under one
# version, breaking the "what does this model_version mean" guarantee the
# schema depends on. Old and new versions simply coexist; no migration
# needed (same pattern as Overture release versioning).
OBJECT_DETECT_VOCABULARY_VERSION = "core-v1"

OBJECT_DETECT_VOCABULARY = {
    "people_animals": [
        "person", "dog", "cat", "horse", "bird", "fish", "deer", "squirrel", "rabbit",
    ],
    "vehicles": [
        "car", "truck", "bus", "bicycle", "motorcycle", "boat", "ship", "airplane",
        "train", "scooter", "RV", "golf cart",
    ],
    "outdoor_recreation": [
        "tent", "backpack", "kayak", "canoe", "surfboard", "ski", "snowboard", "sled",
        "fishing rod", "campfire", "hiking pole", "life jacket",
    ],
    "sports_equipment": [
        "soccer ball", "basketball", "baseball bat", "football", "frisbee", "golf club",
        "tennis racket", "skateboard", "helmet",
    ],
    "furniture_household": [
        "chair", "couch", "table", "bed", "lamp", "television",
    ],
    "food_drink": [
        "cake", "pizza", "wine glass", "cup", "bottle", "birthday candle", "grill",
    ],
    "electronics": [
        "laptop", "cell phone", "camera", "book",
    ],
    "kids_baby": [
        "stroller", "high chair", "toy", "balloon", "crib",
    ],
    "structures_generic": [
        "bridge", "lighthouse", "windmill", "fountain", "statue", "ferris wheel",
        "roller coaster", "playground", "dock", "barn", "silo",
    ],
    "clothing_accessories": [
        "hat", "umbrella", "suitcase", "sunglasses",
    ],
    "nature": [
        "tree", "flower", "mountain", "waterfall", "snow", "cactus", "lake",
    ],
    "disney_and_cruises": [
        "cruise ship", "carousel", "monorail", "fireworks", "parade float",
        "character costume", "mouse ears", "lifeboat", "gangway", "pool deck chair",
    ],
    "churches_castles_religious": [
        "steeple", "spire", "stained glass window", "bell tower", "altar", "cross",
        "dome", "turret", "drawbridge", "pew",
    ],
}


def object_detect_class_list():
    """Flat list of all vocabulary terms, in a stable order, for passing to
    the inference request. Order is stable (dict preserves insertion order
    in Python 3.7+) so repeated calls produce identical input to the model."""
    return [term for group in OBJECT_DETECT_VOCABULARY.values() for term in group]
