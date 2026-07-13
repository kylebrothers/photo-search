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

Two ways this tool is used by the agent:
  a) As a resolver — look up a person UUID from a fuzzy name, or the real
     stored city value for a colloquial place name — feeding search_photos.
  b) As the escape hatch for predicates Immich's API can't express
     (solo-person-in-frame, OCR text, geo proximity), returning asset ids
     straight to finalize_search.

Model split: run_readonly_sql generates its SQL with a SEPARATE Claude call
using SQL_MODEL, not the orchestration model. Today both default to Haiku;
the split lets the SQL step escalate to Sonnet independently after the
SQL-specific test list (README step 6) without moving the orchestrator.
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
        "in natural language what you need (e.g. 'the person id whose name best "
        "matches \"kev\"', or 'asset ids where exactly one visible person is "
        "present and that person is <uuid>'). A single read-only SELECT is "
        "generated and executed; rows are returned to you. Use this to resolve "
        "names/places to real stored values, and for predicates search_photos "
        "cannot express (solo-person-in-frame, text visible in a photo, "
        "flexible place matching). Never use it to modify data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": (
                    "Plain-language description of the read-only lookup you "
                    "need. Be specific about which values you want back "
                    "(e.g. 'return person.id and person.name')."
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
- Fuzzy name/city match: use ILIKE with % wildcards, or compare lowercased \
values. Return the id AND the human-readable value so the caller can confirm.
- "Only person X in frame, nobody else": group asset_face by "assetId", and \
require the set of visible non-null "personId" values to be exactly {{X}} — \
e.g. HAVING count(*) FILTER (WHERE "isVisible" AND "personId" IS NOT NULL) \
matches only that person. Think carefully about faces with NULL "personId".
- OCR text: filter asset_ocr on text ILIKE and a sensible "textScore" floor \
(e.g. > 0.5), "isVisible" = true.
- Place granularity: match the query place against geodata_places.name / \
"admin1Name" / "admin2Name" / "alternateNames", or against \
asset_exif.city/state/country directly, whichever the request implies.
- When returning photos, select asset.id."""


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
    Returns a list of dict rows (already row-capped by the query's LIMIT;
    also hard-capped here as a final backstop).
    """
    conn = psycopg2.connect(config.SQL_READONLY_DSN)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET statement_timeout = {config.SQL_STATEMENT_TIMEOUT_MS};")
            cur.execute(sql)
            rows = cur.fetchmany(config.SQL_ROW_CAP)
            # RealDictCursor rows are dicts; coerce non-JSON-native types.
            return [
                {k: (str(v) if not isinstance(v, (str, int, float, bool, type(None)))
                     else v)
                 for k, v in row.items()}
                for row in rows
            ]
    finally:
        conn.close()


# ── Public executor (called by the agent loop) ────────────────────────────────

def execute_run_readonly_sql(request, claude_client):
    """
    Generate SQL from the request (SQL_MODEL), verify it's a single SELECT,
    execute it on the read-only role, and return rows.

    Returns a dict the agent sees as the tool result:
        {"sql": "...", "rows": [...], "row_count": N}
      or {"error": "...", "sql": "..."} on failure.

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
        rows = _run(sql)
    except psycopg2.errors.QueryCanceled:
        logger.warning(f"SQL statement timeout: {sql!r}")
        return {"error": "query timed out", "sql": sql}
    except psycopg2.Error as e:
        # Includes insufficient-privilege errors from the role — the security
        # boundary doing its job. Surface enough for the agent to adapt.
        logger.warning(f"SQL execution error: {e} — {sql!r}")
        return {"error": f"execution error: {str(e).strip()}", "sql": sql}

    logger.info(f"run_readonly_sql -> {len(rows)} row(s): {sql!r}")
    return {"sql": sql, "rows": rows, "row_count": len(rows)}
