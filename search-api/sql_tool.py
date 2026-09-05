"""
sql_tool.py — safeguarded raw read-only SQL, as a generic factory
(make_readonly_sql_tool) plus two concrete instances: run_readonly_sql
(Immich's Postgres database) and run_readonly_sidecar_sql (the sidecar
database, docs/sidecar-augmentation.md).

DB-agnostic by design (see docs/sidecar-augmentation.md, "Next steps" #5):
the mechanics below (SQL generation, single-SELECT verification, statement
timeout, row cap, handle-vs-inline-rows routing) are identical regardless
of which database is being queried. Only two things differ per instance:
the DSN (which database/role to connect as) and the schema-description
text fed into SQL generation. This means promoting the sidecar tool to
production later is a config change (set SIDECAR_SQL_READONLY_DSN and
AGENT_SIDECAR_SQL_ENABLED on prod's environment once sidecar_prod exists),
not a code change — prod stays sidecar-blind by absence of config, the
same way it already is today.

Why TWO separate tools instead of one that reaches both databases: Immich's
database and the sidecar database are genuinely separate Postgres
databases (see docs/sidecar-augmentation.md, "Core design decisions" —
deliberate, for schema-stability isolation), and standard Postgres cannot
JOIN across databases in a single query. Bridging that with postgres_fdw
was considered and rejected: it would require foreign-table definitions
and grants spanning both databases, quietly eroding the isolation that was
the whole point of splitting them. Instead, a query that needs both (e.g.
"Kevin's photos of the Eiffel Tower") is composed at the AGENT level —
one call to each tool, combined via the existing combine_results
primitive (tools.py) — reusing machinery already built for exactly this
kind of set composition, rather than inventing cross-database plumbing.

"Read-only" is enforced in depth, not hand-waved, for BOTH instances:
  1. A dedicated Postgres role per database (see sql/create_readonly_role.sql
     and sql/create_sidecar_readonly_role.sql) with SELECT on an explicit
     allowlist of tables only. This is the real security boundary; the
     checks below are defence in depth on top of it.
  2. Server-side verification that the statement is a single SELECT before
     execution — the model's own restraint is not trusted.
  3. statement_timeout on the connection.
  4. Hard row-limit cap on rows returned to the model.

Dual-path result handling (reference-based, see tools.py):
  - PHOTO queries: when the model selects a column literally named asset_id,
    the result is a set of photos. We store the ids in the ResultStore and
    return {handle, count} — the bulk ids never enter the model's context.
  - VALUE lookups: a person id, a landmark name, etc. — small, and the model
    needs to read them to use as arguments. These are returned inline as
    rows.
  The path is chosen by whether an "asset_id" column is present, i.e. by
  how the model writes the query — self-correcting, since the response
  tells it how to retry if it forgets.

Model split: SQL generation uses a SEPARATE Claude call with SQL_MODEL, not
the orchestration model, for both instances — see config.py.

Ordering matters for the sidecar tool specifically (added 2026-09): when a
sidecar photo-set handle becomes combine_results' base_handle in a union
with a CLIP search_photos handle (see search_agent.py's "STRUCTURED DATA
BEATS FUZZY VISUAL SIMILARITY" principle), the union's ordering follows
base_handle's ordering first — so the sidecar SQL-generation prompt
instructs the model to ORDER BY confidence DESC on landmark/object-count
lookups, putting the strongest structured matches first in the combined
result rather than in whatever order Postgres happens to return them.
"""

import logging
import re

import psycopg2
import psycopg2.extras

import config

logger = logging.getLogger(__name__)


# ── SQL generation (separate model call, SQL_MODEL) ───────────────────────────

def _strip_sql(raw):
    """Remove accidental markdown fences / leading 'sql' labels."""
    s = raw.strip()
    s = re.sub(r"^```(?:sql)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _generate_sql(request, claude_client, schema_prompt):
    prompt = schema_prompt.format(row_cap=config.SQL_ROW_CAP)
    resp = claude_client.messages.create(
        model=config.SQL_MODEL,
        max_tokens=600,
        system=prompt,
        messages=[{"role": "user", "content": request}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _strip_sql(text)


# ── Server-side single-SELECT verification (shared by every instance) ────────

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|grant|revoke|truncate|copy|create|"
    r"merge|call|do|vacuum|analyze|reindex|comment|set|reset|begin|commit|"
    r"rollback|savepoint|listen|notify|prepare|execute|lock)\b",
    re.IGNORECASE,
)


def _verify_single_select(sql):
    """
    Return (ok, reason). Verifies the string is a single read-only SELECT.
    Belt-and-braces with the role's privileges — a malformed or hostile
    statement is rejected here before it ever reaches the connection.
    """
    s = sql.strip().rstrip(";").strip()
    if not s:
        return False, "empty statement"

    # Reject multiple statements. A ';' remaining after stripping one trailing
    # ';' means more than one statement.
    if ";" in s:
        return False, "multiple statements are not allowed"

    lowered = s.lstrip("(").lstrip().lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False, "only SELECT / WITH ... SELECT is allowed"

    if _FORBIDDEN.search(s):
        return False, "statement contains a forbidden keyword"

    return True, "ok"


# ── Execution against a dedicated read-only role (shared by every instance) ──

def _run(sql, dsn):
    """
    Execute the verified SELECT on a fresh connection using the given DSN's
    dedicated read-only role, with a statement timeout.

    Returns (columns, rows): column names from the cursor description (so an
    empty photo-query still routes correctly by column name), and up to
    SQL_ROW_CAP dict rows with non-JSON-native values coerced to str.
    """
    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET statement_timeout = {config.SQL_STATEMENT_TIMEOUT_MS};")
            cur.execute(sql)
            columns = [d.name for d in cur.description] if cur.description else []
            raw_rows = cur.fetchmany(config.SQL_ROW_CAP)
            rows = [
                {k: (str(v) if not isinstance(v, (str, int, float, bool, type(None)))
                     else v)
                 for k, v in row.items()}
                for row in raw_rows
            ]
            return columns, rows
    finally:
        conn.close()


# ── Generic factory ────────────────────────────────────────────────────────

def make_readonly_sql_tool(tool_name, description, schema_prompt, dsn_config_attr):
    """
    Build one (schema, execute) pair for a read-only-SQL tool against a
    single database.

    tool_name: the tool's name as seen by the agent (e.g. "run_readonly_sql").
    description: the agent-facing tool description.
    schema_prompt: the SQL-generation system prompt for THIS database —
      encodes its table/column names and query-writing traps (NOT security;
      that's the role + verifier). See _IMMICH_SQL_SYSTEM_PROMPT and
      _SIDECAR_SQL_SYSTEM_PROMPT below for the two current instances.
    dsn_config_attr: the config.py attribute NAME (a string) holding this
      instance's DSN — read at CALL time via getattr(), not at import time,
      so an unset DSN (e.g. prod's sidecar DSN, which stays empty on
      purpose) fails per-call with a clear error rather than at import, and
      so a DSN set after this module is imported is still picked up.
    """
    schema = {
        "name": tool_name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": (
                        "Plain-language description of the read-only lookup. "
                        "Say whether you want a value (e.g. 'return the id "
                        "and name') or a set of photos."
                    ),
                },
            },
            "required": ["request"],
        },
    }

    def execute(store, request, claude_client):
        """
        Generate SQL (SQL_MODEL), verify it's a single SELECT, execute it on
        this instance's read-only role, and return either a handle (photo
        set) or inline rows (value lookup).

        Photo path (an "asset_id" column present):
            {"sql": "...", "handle": "result_N", "count": N}
        Value path:
            {"sql": "...", "rows": [...], "row_count": N}
        Failure:
            {"error": "...", "sql": "..."}  (or no sql if generation failed)

        All failure modes return a structured error rather than raising, so
        a bad generation degrades that one tool call rather than aborting
        the agent loop.
        """
        dsn = getattr(config, dsn_config_attr, "")
        if not dsn:
            return {"error": f"{tool_name} is not configured ({dsn_config_attr} is unset)"}

        try:
            sql = _generate_sql(request, claude_client, schema_prompt)
        except Exception as e:
            logger.warning(f"{tool_name}: SQL generation failed: {e}")
            return {"error": f"could not generate SQL: {e}"}

        ok, reason = _verify_single_select(sql)
        if not ok:
            logger.warning(f"{tool_name}: rejected generated SQL ({reason}): {sql!r}")
            return {"error": f"generated SQL rejected: {reason}", "sql": sql}

        try:
            columns, rows = _run(sql, dsn)
        except psycopg2.errors.QueryCanceled:
            logger.warning(f"{tool_name}: SQL statement timeout: {sql!r}")
            return {"error": "query timed out", "sql": sql}
        except psycopg2.Error as e:
            # Includes insufficient-privilege errors from the role — the
            # security boundary doing its job. Surface enough for the agent
            # to adapt.
            logger.warning(f"{tool_name}: SQL execution error: {e} — {sql!r}")
            return {"error": f"execution error: {str(e).strip()}", "sql": sql}

        if "asset_id" in columns:
            # Photo set — store ids, return a handle (bulk ids never reach
            # the model).
            asset_ids = [r["asset_id"] for r in rows if r.get("asset_id") is not None]
            handle = store.put(asset_ids)
            logger.info(f"{tool_name} (photo set) -> {handle} "
                        f"({len(asset_ids)}): {sql!r}")
            return {"sql": sql, "handle": handle, "count": len(asset_ids)}

        # Value lookup — return rows inline for the model to read.
        logger.info(f"{tool_name} (value) -> {len(rows)} row(s): {sql!r}")
        return {"sql": sql, "rows": rows, "row_count": len(rows)}

    return schema, execute


# ── Instance 1: run_readonly_sql (Immich's database) ──────────────────────────
# These rules are NOT security (that's the role + verifier). They exist so
# the generated SQL is *correct* — raw SQL bypasses everything Immich's API
# does for free, so each trap must be stated explicitly.

_IMMICH_SQL_SYSTEM_PROMPT = """You write a single PostgreSQL read-only SELECT for a \
photo-library database, from a plain-language request. Output ONLY the SQL — \
no prose, no markdown fences, no trailing semicolon-plus-comment.

Hard rules:
- Exactly one statement, and it MUST be a SELECT (or WITH ... SELECT). Never \
INSERT/UPDATE/DELETE/DROP/ALTER/GRANT/TRUNCATE/COPY or anything else.
- All identifiers are camelCase and MUST be double-quoted, e.g. \
"assetId", "personId", "deletedAt". Table names are lower_snake_case and are \
not quoted.
- Always exclude soft-deleted and non-timeline assets unless explicitly asked \
otherwise: include `asset."deletedAt" IS NULL AND asset.visibility = \
'timeline'` whenever you read from asset.
- Cap results: end the query with an appropriate LIMIT (never return more than \
{row_cap} rows).

Two kinds of query — pick by what the request asks for:
- If the request wants a SET OF PHOTOS, you MUST alias the asset id column as \
asset_id: `SELECT asset.id AS asset_id ... `. This routes the result into a \
stored handle. Select ONLY asset_id (plus what you need to filter/order).
- If the request wants a VALUE to use later (a person id, a real city name, \
etc.), select it normally and do NOT alias anything as asset_id. Return the id \
AND a human-readable label so the caller can confirm.

Schema (only these tables are readable; anything else will error):

asset(
  id uuid PRIMARY KEY, "ownerId" uuid, type varchar, "originalPath" varchar,
  "fileCreatedAt" timestamptz, "localDateTime" timestamptz,
  "isFavorite" bool, "deletedAt" timestamptz, visibility asset_visibility_enum,
  "originalFileName" varchar
)
asset_exif(
  "assetId" uuid,  -- joins to asset.id
  make varchar, model varchar, "dateTimeOriginal" timestamptz,
  latitude double precision, longitude double precision,
  city varchar, state varchar, country varchar,
  description text, "timeZone" varchar, rating int,
  tags varchar[]
)
asset_face(
  id uuid, "assetId" uuid,  -- joins to asset.id
  "personId" uuid,          -- NULL for an unassigned/unknown face
  "isVisible" bool,
  "sourceType" sourcetype
)
person(
  id uuid PRIMARY KEY, "ownerId" uuid, name varchar,
  "isHidden" bool, "birthDate" date
)
asset_ocr(
  id uuid, "assetId" uuid,  -- joins to asset.id
  text text, "textScore" real, "isVisible" bool
)
geodata_places(
  id int, name varchar, latitude double precision, longitude double precision,
  "countryCode" char(2), "admin1Name" varchar, "admin2Name" varchar,
  "alternateNames" varchar
)
tag(id uuid, value varchar, "userId" uuid)
tag_asset("tagsId" uuid, "assetsId" uuid)

Guidance for common requests:
- Fuzzy name/city match (VALUE lookup): use ILIKE with % wildcards, or compare \
lowercased values. Return the id AND the readable value.
- "Only person X in frame, nobody else" (PHOTO set): group asset_face by \
"assetId" and require the set of visible non-null "personId" values to be \
exactly {{X}} — e.g. HAVING count(*) FILTER (WHERE "isVisible" AND "personId" \
IS NOT NULL) matches only that person. Think carefully about faces with NULL \
"personId". Select asset.id AS asset_id.
- OCR text (PHOTO set): filter asset_ocr on text ILIKE and a sensible \
"textScore" floor (e.g. > 0.5), "isVisible" = true. Select asset.id AS asset_id.
- Place granularity: match against geodata_places.name / "admin1Name" / \
"admin2Name" / "alternateNames", or asset_exif.city/state/country directly."""

RUN_READONLY_SQL_SCHEMA, execute_run_readonly_sql = make_readonly_sql_tool(
    tool_name="run_readonly_sql",
    description=(
        "Run a read-only query against the photo library's main database. "
        "Describe in natural language what you need. TWO uses: (1) look up "
        "a VALUE to use as an argument — e.g. 'the person id whose name "
        "best matches \"kev\"', or the real stored city for a colloquial "
        "place name — these rows are returned to you inline to read. (2) "
        "SELECT a SET OF PHOTOS matching a predicate search_photos can't "
        "express (only-person-X-in-frame, text visible in a photo, geo "
        "proximity) — these are stored and returned as a handle + count, "
        "which you pass to finalize_search or combine_results. For "
        "landmark names, object counts, or county-level location, use "
        "run_readonly_sidecar_sql instead — this tool's database doesn't "
        "have those. Never use it to modify data."
    ),
    schema_prompt=_IMMICH_SQL_SYSTEM_PROMPT,
    dsn_config_attr="SQL_READONLY_DSN",
)


# ── Instance 2: run_readonly_sidecar_sql (the sidecar database) ──────────────
# See docs/sidecar-augmentation.md for what the sidecar is and why it's a
# separate database from Immich's own.

_SIDECAR_SQL_SYSTEM_PROMPT = """You write a single PostgreSQL read-only SELECT for a \
photo-library SIDE-CAR database — augmentation data the main photo-library \
database does not have — from a plain-language request. Output ONLY the SQL \
— no prose, no markdown fences, no trailing semicolon-plus-comment.

Hard rules:
- Exactly one statement, and it MUST be a SELECT (or WITH ... SELECT). Never \
INSERT/UPDATE/DELETE/DROP/ALTER/GRANT/TRUNCATE/COPY or anything else.
- All identifiers here are lower_snake_case and UNQUOTED — unlike the main \
photo-library database's camelCase quoted identifiers, nothing here needs \
double-quoting.
- asset_id in every table below is the SAME uuid as the main database's \
asset.id — but this is a SEPARATE Postgres database with NO cross-database \
JOIN available to you. Query these tables alone; do not attempt to join \
against asset/asset_exif/person/etc. — that join does not exist in this \
connection and will error. A request needing BOTH sidecar data and main-\
database data (e.g. a specific person's photos of a landmark) is handled by \
running two separate tool calls and combining their handles with \
combine_results at the agent level, not by this tool.
- Cap results: end the query with an appropriate LIMIT (never return more than \
{row_cap} rows).
- For a PHOTO-set query, ORDER BY the relevant quality signal DESCENDING \
(confidence for landmark_matches, count for object_counts) so the strongest \
matches come first — this ordering is preserved when the caller later unions \
this result with a broader CLIP search, putting your best matches at the \
front of the combined list.

Two kinds of query — pick by what the request asks for:
- If the request wants a SET OF PHOTOS, select the id column exactly as \
named in the schema below: `SELECT asset_id ... `. Select ONLY asset_id \
(plus what you need to filter/order — the ORDER BY column itself does not \
need to be selected).
- If the request wants a VALUE to use later (e.g. confirming a landmark name \
exists), select it normally and do NOT select asset_id.

Schema (only these tables are readable; anything else will error):

landmark_matches(
  asset_id uuid,                  -- same id as the main database's asset.id
  landmark_id text,                -- GLDv2's numeric landmark ID as a string;
                                    -- NULL for source='overture_places' rows
                                    -- (proximity matches have no GLDv2 ID)
  landmark_name text,              -- human-readable name; MAY be a bare
                                    -- "landmark <id>" placeholder when GLDv2's
                                    -- own metadata is thin for that ID — a
                                    -- known data gap, not a bug; still a
                                    -- valid, potentially correct match
  confidence double precision,     -- cosine similarity (source=
                                    -- 'dinov3_visual') or Overture's existence
                                    -- confidence (source='overture_places') —
                                    -- NOT directly comparable across sources
  distance_meters double precision,-- NULL for dinov3_visual rows (visual
                                    -- matches have no distance concept)
  source text,                     -- 'dinov3_visual' or 'overture_places'
  model_version text
)
object_counts(
  asset_id uuid,
  class text,                      -- open-vocabulary object/animal/scene
                                    -- class name (e.g. 'dog', 'birthday cake')
  count int,
  avg_confidence double precision,
  model_version text
)
resolved_geo(
  asset_id uuid,
  city text, county text, state text, country text,
  source text                      -- 'immich_reverse_geocode' or
                                    -- 'overture_divisions'
)

Guidance for common requests:
- Landmark name search (PHOTO set): match landmark_name with ILIKE — it's a \
rough, sometimes messy parsed display name, not a curated one, so avoid \
requiring an exact match. Add a confidence floor if the request implies \
"definitely"/"clearly" (e.g. confidence > 0.75); leave it loose otherwise. \
ORDER BY confidence DESC (see hard rules above).
- Object/count search (PHOTO set): filter object_counts.class with ILIKE or \
exact match; add a numeric comparison on count if the request implies a \
specific quantity (e.g. "3 or more dogs" -> count >= 3). ORDER BY count DESC.
- County-level or finer place search (PHOTO set or VALUE lookup): \
resolved_geo.county exists here but not in the main database — useful when \
a request needs finer granularity than city/state/country alone provides."""

RUN_READONLY_SIDECAR_SQL_SCHEMA, execute_run_readonly_sidecar_sql = make_readonly_sql_tool(
    tool_name="run_readonly_sidecar_sql",
    description=(
        "Run a read-only query against the photo library's SIDE-CAR "
        "database — augmentation data the main database doesn't have: "
        "visually-matched landmarks (landmark_matches, both AI visual "
        "recognition and geospatial-proximity sources), detected objects/"
        "animals with counts (object_counts), and finer-grained location "
        "data including county (resolved_geo). Use this when a request "
        "needs one of THOSE specific kinds of fact — a named landmark, an "
        "object count, a county — that search_photos and run_readonly_sql "
        "can't express. IMPORTANT: for a named landmark or object count, "
        "this does NOT replace search_photos — run BOTH (see the agent's "
        "system prompt, 'STRUCTURED DATA BEATS FUZZY VISUAL SIMILARITY') "
        "and combine them with combine_results, mode='union', this tool's "
        "handle as base_handle. This is a genuinely SEPARATE database from "
        "run_readonly_sql's; it cannot be joined against the main database "
        "in one query. If a request needs BOTH a sidecar fact AND a "
        "main-database fact (e.g. a specific person AND a landmark), run "
        "each as its own query and combine the two handles with "
        "combine_results. Returns a handle + count for a photo set, or "
        "inline rows for a value lookup. Never use it to modify data."
    ),
    schema_prompt=_SIDECAR_SQL_SYSTEM_PROMPT,
    dsn_config_attr="SIDECAR_SQL_READONLY_DSN",
)
