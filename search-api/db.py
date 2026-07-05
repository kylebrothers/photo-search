"""
Direct Postgres access into Immich's own database.

WARNING: this reaches into Immich's internal schema, not its public API.
Table/column names below are best-effort as of the Immich version this was
written against and are NOT guaranteed stable across Immich upgrades.
Verify against `\d+ smart_search` (or equivalent) on your actual instance
before relying on this in production, and re-check after any Immich update.
"""
import psycopg2
import config


def get_connection():
    return psycopg2.connect(config.IMMICH_DB_DSN)


def get_embedding(asset_id: str):
    """
    Return the CLIP embedding vector for a given asset, or None if not found.
    TODO: confirm actual table/column names (assumed: smart_search.embedding,
    keyed by asset_id) against your Immich version.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT embedding FROM smart_search WHERE \"assetId\" = %s LIMIT 1;",
                (asset_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
