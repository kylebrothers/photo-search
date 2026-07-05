"""
Manages the small, hand-labeled set of landmark reference embeddings.
Analogous to Immich's face-tagging: you label a few examples, matching is
nearest-neighbor from there. Stored as flat JSON — simple and adequate for
what should be a short, curated list of landmarks, not the whole library.
"""
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import config


def _load():
    if not os.path.exists(config.LANDMARK_REFERENCE_STORE):
        return {}
    with open(config.LANDMARK_REFERENCE_STORE) as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(config.LANDMARK_REFERENCE_STORE), exist_ok=True)
    with open(config.LANDMARK_REFERENCE_STORE, "w") as f:
        json.dump(data, f, indent=2)


def add_reference(landmark_name: str, asset_id: str):
    """Label a photo as a reference example for a landmark."""
    embedding = db.get_embedding(asset_id)
    if embedding is None:
        raise ValueError(f"No embedding found for asset {asset_id} — has Smart Search indexed it yet?")
    data = _load()
    data.setdefault(landmark_name, []).append({"asset_id": asset_id, "embedding": list(embedding)})
    _save(data)


def list_landmarks():
    return list(_load().keys())


def all_references():
    """Returns {landmark_name: [{asset_id, embedding}, ...]}."""
    return _load()
