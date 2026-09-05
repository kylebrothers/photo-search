-- ── create_sidecar_readonly_role.sql ────────────────────────────────────────
--
-- Dedicated Postgres role for search-api's run_readonly_sidecar_sql tool
-- (see search-api/sql_tool.py). Same approach as
-- sql/create_readonly_role.sql (Immich's role), applied to the sidecar
-- database instead.
--
-- This role is the REAL security boundary for the sidecar raw-SQL tool. The
-- in-process single-SELECT verifier in sql_tool.py is defence in depth on
-- top of this, not a substitute for it.
--
-- Approach: ALLOWLIST, not denylist. We REVOKE everything, then GRANT SELECT
-- on only the specific tables the agent needs to read. enrichment_status is
-- deliberately EXCLUDED — it's internal bookkeeping (what's been processed,
-- what failed and why), not search-relevant data, and error_detail strings
-- have no business being readable by a search agent.
--
-- Usage (run as the sidecar DB superuser, against the SIDECAR database —
-- note -d sidecar_dev, NOT -d immich):
--   docker compose exec -T postgres \
--     psql -U immich -d sidecar_dev -v role_password="'CHANGE_ME'" \
--     -f - < sql/create_sidecar_readonly_role.sql
--
-- Then set in search-api-dev's environment:
--   SIDECAR_SQL_READONLY_DSN=postgresql://sidecar_search_ro:CHANGE_ME@postgres:5432/sidecar_dev
--   AGENT_SIDECAR_SQL_ENABLED=true
--
-- When sidecar_prod exists (docs/sidecar-augmentation.md, "Process &
-- infrastructure decisions"), re-run this against -d sidecar_prod and set
-- the equivalent env vars on production search-api to promote it there —
-- see sql_tool.py's module docstring: this is meant to be a config change,
-- not a code change.
-- ─────────────────────────────────────────────────────────────────────────────

-- Create the role if it doesn't already exist. LOGIN so search-api can
-- connect as it; NOSUPERUSER NOCREATEDB NOCREATEROLE by default.
--
-- Same \gexec approach as create_readonly_role.sql, for the same reason:
-- :'role_password' does not substitute inside a dollar-quoted DO $$ ... $$
-- block, so the CREATE ROLE statement is built as a string and run via
-- \gexec instead — idempotent and re-runnable.
SELECT 'CREATE ROLE sidecar_search_ro LOGIN PASSWORD ' || quote_literal(:'role_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sidecar_search_ro')
\gexec

-- Start from zero: strip anything this role may have inherited, including the
-- public schema's default privileges.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM sidecar_search_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM sidecar_search_ro;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM sidecar_search_ro;
REVOKE ALL ON SCHEMA public FROM sidecar_search_ro;

-- Also strip PUBLIC's implicit privileges on future objects for this role's
-- benefit: ensure no default grant re-opens access to new tables (e.g. a
-- future enrichment tool's table isn't readable until explicitly granted
-- here).
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM sidecar_search_ro;

-- Minimum needed to see the schema at all.
GRANT USAGE ON SCHEMA public TO sidecar_search_ro;

-- ── The allowlist: SELECT on ONLY these tables ───────────────────────────────
-- Search-relevant tables only. Explicitly EXCLUDES enrichment_status (see
-- header) and anything added later that isn't deliberately granted here.
GRANT SELECT ON
    landmark_matches,
    object_counts,
    resolved_geo
TO sidecar_search_ro;

-- Deliberately NOT granted (enumerated so the exclusion is auditable):
--   enrichment_status   (internal bookkeeping — tool/model_version/status/
--                        error_detail; not search-relevant, and error_detail
--                        strings shouldn't be agent-readable)
--   test_set             (dev-only pinned test data, not real search scope)

-- Sanity check after running (should list exactly the 3 granted tables):
--   SELECT table_name FROM information_schema.role_table_grants
--   WHERE grantee = 'sidecar_search_ro' ORDER BY table_name;
