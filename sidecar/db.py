"""
Connection helper for the sidecar DB (our own schema — see
migrations/001_initial_schema.sql). Not Immich's database; see
search-api/db.py for that.
"""
import psycopg2
import config


def get_connection():
    return psycopg2.connect(config.SIDECAR_DB_DSN)


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
