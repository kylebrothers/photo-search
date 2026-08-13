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


def sidecar_db_kwargs():
    """psycopg2.connect(**kwargs) for the sidecar database."""
    return dict(host=DB_HOST, port=DB_PORT, user=DB_USER,
                password=DB_PASSWORD, dbname=SIDECAR_DB_NAME)


def immich_db_kwargs():
    """psycopg2.connect(**kwargs) for Immich's own database (read-only use)."""
    return dict(host=DB_HOST, port=DB_PORT, user=DB_USER,
                password=DB_PASSWORD, dbname=IMMICH_DB_NAME)
