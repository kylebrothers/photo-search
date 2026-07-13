"""
sql_tool.py — run_readonly_sql: safeguarded raw read-only SQL over Immich's
Postgres schema.

This is the highest-risk, most novel piece (README). "Read-only" is enforced
in depth, not hand-waved:

  1. Dedicated Postgres role (see sql/create_readonly_role.sql) with SELECT
     on an explicit allowlist of tables only — auth/user/session/api_key and
     all *_audit tables excluded. This is the real security boundary; the
     checks below are defence in depth on top of it.
  2. Server-side verification that the statement is a single SELECT before
     execution — the model's own restraint is not trusted.
  3. statement_timeout on the connection.
  4. Hard row-limit cap on rows returned to the model.

Dual-path result handling (reference-based, see tools.py):
  - PHOTO queries: when the model aliases the asset id column AS asset_id, the
    result is a set of photos. We store the ids in the ResultStore and return
    {handle, count} — the bulk ids never enter the model's context.
  - VALUE lookups: a person id, a city name, etc. — small, and the model needs
    to read them to use as arguments. These are returned inline as rows.
  The path is chosen by whether an "asset_id" column is present, i.e. by how
  the model writes the query — self-correcting, since the response tells it how
  to retry if it forgets the alias.

Model split: run_readonly_sql generates its SQL with a SEPARATE Claude call
using SQL_MODEL, not the orchestration model. Today both default to Haiku; the
split lets the SQL step escalate to Sonnet independently after the SQL-specific
test list (README step 6) without moving the orchestrator.
"""

import logging
import re

import psycopg2
import psycopg2.extras

import config

logger = logging.getLogger(__name__)


# ── Tool schema (exposed to the orchestration agent) ──────────────────────────

RUN_READONLY_SQL_SCHEMA = {
    "name": "run_readonly_sql",
    "description": (
        "Run a read-only query against the photo library's database. Describe "
        "in natural language what you need. TWO uses: (1) look up a VALUE to "
        "use as an argument — e.g. 'the person id whose name best matches "
        "\"kev\"', or the real stored city for a colloquial place name — these "
        "rows are returned to you inline to read. (2) SELECT a SET OF PHOTOS "
        "matching a predicate search_photos can't express (only-person-X-in-"
        "frame, text visible in a photo, geo proximity) — these are stored and "
        "returned as a handle + count, which you pass to finalize_search or "
        "combine_results. Never use it to modify data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": (
                    "Plain-language description of the read-only lookup. Say "
                    "whether you want a value (e.g. 'return person.id and "
                    "name') or a set of photos."
                ),
            },
        },
        "required": ["request"],
    },
}


# ── SQL-generation prompt: encodes the schema + correctness traps ─────────────
#
# These rules are NOT security (that's the role + verifier). They exist so the
# generated SQL is *correct* — raw SQL bypasses everything Immich's API does
# for free, so each trap must be stated explicitly.

_SQL_SYSTEM_PROMPT = """You write a single PostgreSQL read-only SELECT for a \
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


# ── SQL generation (separate model call, SQL_MODEL) ───────────────────────────

def _strip_sql(raw):
    """Remove accidental markdown fences / leading 'sql' labels."""
    s = raw.strip()
    s = re.sub(r"^```(?:sql)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _generate_sql(request, claude_client):
    prompt = _SQL_SYSTEM_PROMPT.format(row_cap=config.SQL_ROW_CAP)
    resp = claude_client.messages.create(
        model=config.SQL_MODEL,
        max_tokens=600,
        system=prompt,
        messages=[{"role": "user", "content": request}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _strip_sql(text)


# ── Server-side single-SELECT verification ────────────────────────────────────

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


# ── Execution against the dedicated read-only role ────────────────────────────

def _run(sql):
    """
    Execute the verified SELECT on a fresh connection using the dedicated
    read-only role (config.SQL_READONLY_DSN), with a statement timeout.

    Returns (columns, rows): column names from the cursor description (so an
    empty photo-query still routes correctly by column name), and up to
    SQL_ROW_CAP dict rows with non-JSON-native values coerced to str.
    """
    conn = psycopg2.connect(config.SQL_READONLY_DSN)
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


# ── Public executor (called by the agent loop) ────────────────────────────────

def execute_run_readonly_sql(store, request, claude_client):
    """
    Generate SQL (SQL_MODEL), verify it's a single SELECT, execute it on the
    read-only role, and return either a handle (photo set) or inline rows
    (value lookup).

    Photo path (an "asset_id" column present):
        {"sql": "...", "handle": "result_N", "count": N}
    Value path:
        {"sql": "...", "rows": [...], "row_count": N}
    Failure:
        {"error": "...", "sql": "..."}  (or no sql if generation failed)

    All failure modes return a structured error rather than raising, so a bad
    generation degrades that one tool call rather than aborting the loop.
    """
    try:
        sql = _generate_sql(request, claude_client)
    except Exception as e:
        logger.warning(f"SQL generation failed: {e}")
        return {"error": f"could not generate SQL: {e}"}

    ok, reason = _verify_single_select(sql)
    if not ok:
        logger.warning(f"Rejected generated SQL ({reason}): {sql!r}")
        return {"error": f"generated SQL rejected: {reason}", "sql": sql}

    try:
        columns, rows = _run(sql)
    except psycopg2.errors.QueryCanceled:
        logger.warning(f"SQL statement timeout: {sql!r}")
        return {"error": "query timed out", "sql": sql}
    except psycopg2.Error as e:
        # Includes insufficient-privilege errors from the role — the security
        # boundary doing its job. Surface enough for the agent to adapt.
        logger.warning(f"SQL execution error: {e} — {sql!r}")
        return {"error": f"execution error: {str(e).strip()}", "sql": sql}

    if "asset_id" in columns:
        # Photo set — store ids, return a handle (bulk ids never reach the model).
        asset_ids = [r["asset_id"] for r in rows if r.get("asset_id") is not None]
        handle = store.put(asset_ids)
        logger.info(f"run_readonly_sql (photo set) -> {handle} "
                    f"({len(asset_ids)}): {sql!r}")
        return {"sql": sql, "handle": handle, "count": len(asset_ids)}

    # Value lookup — return rows inline for the model to read.
    logger.info(f"run_readonly_sql (value) -> {len(rows)} row(s): {sql!r}")
    return {"sql": sql, "rows": rows, "row_count": len(rows)}
