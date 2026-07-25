"""
Manages the fixed, pinned dev test set (sidecar.test_set table): 100 photos
+ hand-picked hard cases, so enrichment approaches are comparable across runs.

Not yet implemented — stub reflects the agreed shape (README-adjacent design
chat, sidecar-augmentation.md). TODO before this is usable:
  - populate(): pick 100 asset_ids from Immich (random or stratified — TBD)
    plus known hard cases (e.g. the 4 Disney no-city photos), write them here
    with an appropriate `label`.
  - get(label=None): return asset_ids, optionally filtered to one label.
"""
import db


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
        {"<uuid>": "hard_case:disney_no_city"}.
    TODO: wire up the actual selection logic (see module docstring).
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
