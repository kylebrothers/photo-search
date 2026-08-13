"""
Manages the fixed, pinned dev test set (sidecar.test_set table): 100 photos
+ hand-picked hard cases, so enrichment approaches are comparable across runs.
"""
from . import db


def get(label=None):
    """Return asset_ids from the pinned test set, optionally filtered by label."""
    query = "SELECT asset_id, label FROM test_set"
    params = ()
    if label:
        query += " WHERE label = %s"
        params = (label,)
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()


def populate(random_asset_ids, hard_cases=None):
    """
    random_asset_ids: iterable of ~100 asset_id strings to label 'random'.
    hard_cases: dict of {asset_id: label}, e.g.
        {"<uuid>": "hard_case:multi_face"}.
    Idempotent: random uses ON CONFLICT DO NOTHING (won't duplicate or
    relabel an existing row), hard_cases uses ON CONFLICT DO UPDATE (so a
    hard case's label can be corrected on rerun without a manual delete).
    """
    hard_cases = hard_cases or {}
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            for asset_id in random_asset_ids:
                cur.execute(
                    "INSERT INTO test_set (asset_id, label) VALUES (%s, 'random') "
                    "ON CONFLICT (asset_id) DO NOTHING;",
                    (asset_id,),
                )
            for asset_id, label in hard_cases.items():
                cur.execute(
                    "INSERT INTO test_set (asset_id, label) VALUES (%s, %s) "
                    "ON CONFLICT (asset_id) DO UPDATE SET label = EXCLUDED.label;",
                    (asset_id, label),
                )
        conn.commit()
