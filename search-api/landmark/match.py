"""
Nearest-neighbor landmark matching against the labeled reference set.
Same underlying idea as Immich's face recognition, reusing CLIP embeddings
Immich already computed rather than a landmark-specialized model — expect
correspondingly lower accuracy (see README, point 3).
"""
import numpy as np
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import config
from landmark.reference_embeddings import all_references


def _cosine_distance(a, b):
    a, b = np.array(a), np.array(b)
    return 1 - (np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def match_landmarks(asset_id: str, threshold: float = None):
    """
    Returns a list of (landmark_name, distance) for matches under the
    confidence threshold, sorted best-first. Empty list if no match or if
    the asset has no embedding yet.
    """
    threshold = threshold if threshold is not None else config.LANDMARK_MATCH_THRESHOLD
    target_embedding = db.get_embedding(asset_id)
    if target_embedding is None:
        return []

    matches = []
    for landmark_name, refs in all_references().items():
        best_distance = min(_cosine_distance(target_embedding, r["embedding"]) for r in refs)
        if best_distance <= threshold:
            matches.append((landmark_name, best_distance))

    return sorted(matches, key=lambda m: m[1])
