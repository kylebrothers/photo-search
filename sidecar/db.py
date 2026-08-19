"""
Connection helper for the sidecar DB (our own schema — see
migrations/001_initial_schema.sql). Not Immich's database; see
search-api/db.py for that.
"""
import psycopg2
from . import config


def get_connection():
    return psycopg2.connect(**config.sidecar_db_kwargs())


def run_migrations(migrations_dir="migrations"):
    """
    Dev-only helper: apply every .sql file in migrations_dir in filename
    order, unconditionally. No migration-tracking table yet — this is the
    wipe-and-redevelop dev DB, not a production migration runner. Replace
    with a real tool (e.g. golang-migrate, alembic-style tracking) before
    a production sidecar DB is introduced.
    """
    import glob

    files = sorted(glob.glob(f"{migrations_dir}/*.sql"))
    with get_connection() as conn:
        with conn.cursor() as cur:
            for path in files:
                with open(path) as f:
                    cur.execute(f.read())
        conn.commit()
    return files


def ensure_column(table, column, coltype_sql):
    """
    Idempotently ensures `column` exists on `table`, with type/default/
    constraints given by coltype_sql (e.g. "text", "integer DEFAULT 0").
    Uses Postgres' native ADD COLUMN IF NOT EXISTS, so it's a safe no-op if
    the column already exists -- can be called every time before an
    enrichment runs, not just once by hand.

    Fixes a real gap: typing ALTER TABLE into psql by hand each time the
    schema needs to evolve (as happened for resolved_geo.county) doesn't
    scale and isn't portable across dev/prod or a fresh machine. See
    docs/sidecar-augmentation.md, "Schema evolution tooling."

    table/column/coltype_sql are interpolated directly into SQL -- only
    ever call this with hardcoded, developer-controlled strings declared in
    code (see sidecar/ensure_schema.py), never with user input.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{column}" {coltype_sql};')
        conn.commit()


def ensure_table(name, create_sql):
    """
    Idempotently ensures a table exists. create_sql is the column-definition
    body a CREATE TABLE would need (e.g. 'id uuid PRIMARY KEY, ...'). Uses
    CREATE TABLE IF NOT EXISTS, a safe no-op if the table already exists.

    Same trusted-input-only caveat as ensure_column: hardcoded schema
    definitions only, never user input.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE IF NOT EXISTS {name} ({create_sql});')
        conn.commit()
