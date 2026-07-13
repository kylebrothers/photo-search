-- ── create_readonly_role.sql ─────────────────────────────────────────────────
--
-- Dedicated Postgres role for search-api's run_readonly_sql tool.
--
-- This role is the REAL security boundary for the raw-SQL tool. The in-process
-- single-SELECT verifier in sql_tool.py is defence in depth on top of this,
-- not a substitute for it.
--
-- Approach: ALLOWLIST, not denylist. We REVOKE everything, then GRANT SELECT
-- on only the specific tables the agent needs to read. A future Immich
-- migration that adds a new secret-bearing table is therefore NOT readable by
-- default — it has to be granted explicitly here.
--
-- Table names below were verified against a live instance on 2026-07-11.
-- Immich renames tables across versions (there is no stable public schema
-- contract — see db.py's warning), so RE-VERIFY after any Immich upgrade:
--   \dt   then re-check this allowlist still matches.
--
-- Usage (run as the Immich DB superuser, e.g. `immich`):
--   docker compose exec -T postgres \
--     psql -U immich -d immich -v role_password="'CHANGE_ME'" \
--     -f - < sql/create_readonly_role.sql
--
-- Then set in search-api's environment:
--   SQL_READONLY_DSN=postgresql://immich_search_ro:CHANGE_ME@postgres:5432/immich
-- ─────────────────────────────────────────────────────────────────────────────

-- Create the role if it doesn't already exist. LOGIN so search-api can
-- connect as it; NOSUPERUSER NOCREATEDB NOCREATEROLE by default.
--
-- NOTE: the password is interpolated by psql via -v role_password. This MUST
-- be done OUTSIDE any dollar-quoted DO $$ ... $$ block — psql does not
-- substitute :'var' inside dollar-quotes (the block body is opaque to the
-- client), so an earlier DO-block version failed with "syntax error at :".
-- Instead we build the CREATE ROLE statement with \gexec, which interpolates
-- the variable (outside $$) and runs the result only if the role is absent —
-- idempotent and re-runnable.
SELECT 'CREATE ROLE immich_search_ro LOGIN PASSWORD ' || quote_literal(:'role_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'immich_search_ro')
\gexec

-- Start from zero: strip anything this role may have inherited, including the
-- public schema's default privileges.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM immich_search_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM immich_search_ro;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM immich_search_ro;
REVOKE ALL ON SCHEMA public FROM immich_search_ro;

-- Also strip PUBLIC's implicit privileges on future objects for this role's
-- benefit: ensure no default grant re-opens access to new tables.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM immich_search_ro;

-- Minimum needed to see the schema at all.
GRANT USAGE ON SCHEMA public TO immich_search_ro;

-- ── The allowlist: SELECT on ONLY these tables ───────────────────────────────
-- Search-relevant tables only. Explicitly EXCLUDES: user, user_metadata,
-- user_metadata_audit, session, session_sync_checkpoint, api_key, partner,
-- partner_audit, shared_link, shared_link_asset, album_user, album_user_audit,
-- and every *_audit / *_migrations / naturalearth_* table.
GRANT SELECT ON
    asset,
    asset_exif,
    asset_face,
    person,
    asset_ocr,
    geodata_places,
    tag,
    tag_asset
TO immich_search_ro;

-- Deliberately NOT granted (enumerated so the exclusion is auditable):
--   user, user_metadata, session, session_sync_checkpoint, api_key   (auth/secrets)
--   partner, shared_link, shared_link_asset, album_user              (sharing/ACL)
--   *_audit tables                                                   (history noise)
--   library, memory, notification, plugin, workflow, move_history    (unrelated)
--   smart_search, face_search                                        (raw embeddings;
--        landmark/match.py reaches these via the SEPARATE IMMICH_DB_DSN, not
--        this role — keep them out of the agent's reach)

-- Sanity check after running (should list exactly the 8 granted tables):
--   SELECT table_name FROM information_schema.role_table_grants
--   WHERE grantee = 'immich_search_ro' ORDER BY table_name;
