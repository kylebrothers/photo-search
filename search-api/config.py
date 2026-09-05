import os

IMMICH_URL = os.environ.get("IMMICH_URL", "http://immich-server:2283")
IMMICH_API_KEY = os.environ.get("IMMICH_API_KEY", "")

# Direct Postgres access — used only by landmark/match.py, which needs raw
# CLIP embeddings that Immich's public API does not expose.
IMMICH_DB_DSN = os.environ.get(
    "IMMICH_DB_DSN", "postgresql://postgres:postgres@immich-postgres:5432/immich"
)

LANDMARK_MATCH_THRESHOLD = float(os.environ.get("LANDMARK_MATCH_THRESHOLD", "0.75"))
LANDMARK_REFERENCE_STORE = os.environ.get(
    "LANDMARK_REFERENCE_STORE", "/data/landmark_references.json"
)

# Query parser backend — see query_parser.py's dispatcher docstring.
# "llm" (default): query_parser_llm.py, grounded with real Immich data via
#   Ollama running on the gpu-ml device. Falls back to "rules" automatically
#   if Ollama is unreachable or returns malformed output.
# "rules": query_parser_rules.py only, no LLM dependency.
QUERY_PARSER_MODE = os.environ.get("QUERY_PARSER_MODE", "llm")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")


# ── Search agent (Claude API) ─────────────────────────────────────────────────
# The tool-calling agent (search_agent.py) that supersedes the one-shot LLM
# parser for the primary search path. query_parser_rules.py stays as the
# permanent fallback.

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Models are PINNED, not auto-"latest": SQL/tool correctness is prompt-
# sensitive, so model moves must be deliberate and followed by re-running the
# structured test list. Two separate vars so the SQL step can be escalated
# (Haiku -> Sonnet) independently of the orchestrator; today both default to
# Haiku. Current escalation target as of 2026-07: claude-sonnet-5.
# NB: the Haiku id's date suffix is mandatory — "claude-haiku-4-5" alone fails.
AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-haiku-4-5-20251001")
SQL_MODEL = os.environ.get("SQL_MODEL", "claude-haiku-4-5-20251001")

# Loop bounds. The wall-clock timeout is independent of Gunicorn's --timeout
# and MUST be smaller than it, so this fires first and the request fails
# gracefully into the rules fallback rather than Gunicorn killing the worker
# (same race-condition lesson as the earlier Ollama timeout bug — Gunicorn is
# at 90s, so keep this well below that).
AGENT_MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "6"))
AGENT_WALL_CLOCK_TIMEOUT = float(os.environ.get("AGENT_WALL_CLOCK_TIMEOUT", "60"))

# Max output tokens per agent turn. With reference-based handles a finalize
# turn is tiny (a handle + a sentence), so the old 1024 truncation can't recur;
# kept generous as a backstop. Raise if a legitimately long SQL request or
# explanation ever truncates (logged distinctly as max_tokens truncation).
AGENT_MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "4096"))

# Toggle the run_readonly_sql tool. Lets the four-tool loop be proven before
# the SQL tool's Postgres role exists (README step 4 before 5). When false,
# the agent runs with search_photos + finalize_search only.
AGENT_SQL_ENABLED = os.environ.get("AGENT_SQL_ENABLED", "true").lower() == "true"

# Toggle the run_readonly_sidecar_sql tool (see sql_tool.py's module
# docstring and docs/sidecar-augmentation.md, "Next steps" #5). Defaults to
# false so production search-api — which does not set
# SIDECAR_SQL_READONLY_DSN and is deliberately kept sidecar-blind (see
# design doc, "Process & infrastructure decisions") — is entirely unaffected
# by this toggle even existing. search-api-dev sets this to true once its
# sidecar read-only role exists.
AGENT_SIDECAR_SQL_ENABLED = os.environ.get("AGENT_SIDECAR_SQL_ENABLED", "false").lower() == "true"


# ── SQL tools (run_readonly_sql, run_readonly_sidecar_sql) ───────────────────
# Both connect as a DEDICATED read-only role, NOT a superuser. See
# sql/create_readonly_role.sql (Immich) and sql/create_sidecar_readonly_role.sql
# (sidecar). Each DSN's role must have SELECT on only that database's search
# allowlist and nothing else — the DSN is the security boundary, the
# in-process checks in sql_tool.py are defence in depth.
SQL_READONLY_DSN = os.environ.get("SQL_READONLY_DSN", "")

# Unset by default — see AGENT_SIDECAR_SQL_ENABLED above. Set on
# search-api-dev once sql/create_sidecar_readonly_role.sql has been run
# against sidecar_dev.
SIDECAR_SQL_READONLY_DSN = os.environ.get("SIDECAR_SQL_READONLY_DSN", "")

# Hard cap on rows returned to the model (context + cost control). Shared by
# both SQL tools — not a database-specific concern. Also instructed as a
# LIMIT in each SQL-generation prompt; enforced again server-side via
# fetchmany as a backstop.
SQL_ROW_CAP = int(os.environ.get("SQL_ROW_CAP", "100"))

# Per-statement timeout (ms) applied on the connection. Shared by both SQL
# tools.
SQL_STATEMENT_TIMEOUT_MS = int(os.environ.get("SQL_STATEMENT_TIMEOUT_MS", "5000"))
