"""
tools.py — Tool implementations and Anthropic tool schemas for the
search agent (see search_agent.py).

Thin tool set (people/city/landmark structured resolvers were deliberately
dropped — see README "Agreed design going forward"). What remains:

  search_photos   — ranked retrieval via Immich's own APIs. Holds the
                    CLIP-rank-then-metadata-filter logic that previously
                    lived inline in app.py: CLIP smart_search establishes
                    relevance order; metadata matches are applied only as a
                    membership FILTER on that order, never as the result
                    list itself. This invariant is the whole reason this
                    logic stays in Python rather than being reconstructed
                    by the model across tool calls.
  finalize_search — explicit end-of-loop signal. Its `explanation` becomes
                    the user-facing account of what the agent did, replacing
                    the old opaque `parsed` dict.

run_readonly_sql lives in sql_tool.py and is registered by the agent
separately, so this module has no dependency on the SQL path.

Each tool has two parts: a SCHEMA dict (sent to the Anthropic API) and an
`execute_*` function (run when the model calls it). The agent maps tool
name -> executor.
"""

import logging

logger = logging.getLogger(__name__)


# ── search_photos ─────────────────────────────────────────────────────────────

SEARCH_PHOTOS_SCHEMA = {
    "name": "search_photos",
    "description": (
        "Retrieve photos from the library, ranked by relevance. Use this for "
        "the common case: an object/scene description (e.g. 'beach', 'dog on a "
        "sofa') optionally combined with a known person, a city, and/or a date "
        "range. CLIP semantic search establishes the ranking when object_query "
        "is given; person/city/date act as filters. Resolve person names to "
        "person_ids and place names to a real stored city value BEFORE calling "
        "this (use run_readonly_sql for that resolution). For predicates this "
        "tool cannot express — 'only person X in frame, nobody else', text "
        "visible in the photo (OCR), or place-name granularity beyond an exact "
        "city match — use run_readonly_sql instead and pass the resulting asset "
        "ids to finalize_search directly."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "object_query": {
                "type": "string",
                "description": (
                    "Free-text description of visual content for CLIP semantic "
                    "search (e.g. 'sunset over water'). Empty string if the "
                    "query has no object/scene component (pure metadata search)."
                ),
            },
            "person_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Immich person UUIDs to filter by (photos containing ALL of "
                    "these people). Resolve names to ids via run_readonly_sql "
                    "first. Empty if no person filter."
                ),
            },
            "city": {
                "type": ["string", "null"],
                "description": (
                    "Exact stored EXIF city value (e.g. 'Manhattan', not 'New "
                    "York'). Resolve colloquial place names to a real stored "
                    "value via run_readonly_sql first. Null if no location "
                    "filter."
                ),
            },
            "date_from": {
                "type": ["string", "null"],
                "description": (
                    "ISO 8601 with milliseconds and Z suffix, e.g. "
                    "'2026-06-01T00:00:00.000Z'. Null if no lower date bound."
                ),
            },
            "date_to": {
                "type": ["string", "null"],
                "description": (
                    "ISO 8601 with milliseconds and Z suffix. Null if no upper "
                    "date bound."
                ),
            },
        },
        "required": ["object_query", "person_ids", "city", "date_from", "date_to"],
    },
}


def execute_search_photos(immich, object_query="", person_ids=None,
                          city=None, date_from=None, date_to=None):
    """
    Ranked retrieval. Mirrors the exact rank-then-filter behaviour that
    previously lived in app.py's /api/search handler.

    Returns a list of asset id strings in relevance order.
    """
    person_ids = person_ids or []
    object_query = (object_query or "").strip()

    # ordered_ids carries the relevance ranking; filter_sets are membership
    # filters layered on top, never the result list itself.
    ordered_ids = []
    filter_sets = []

    if object_query:
        smart = immich.smart_search(object_query)
        ordered_ids = [a["id"] for a in smart.get("assets", {}).get("items", [])]

    has_metadata = bool(person_ids or city or date_from)
    if has_metadata:
        meta = immich.search_metadata(
            city=city, date_from=date_from, date_to=date_to,
            person_ids=person_ids or None,
        )
        meta_ids = [a["id"] for a in meta.get("assets", {}).get("items", [])]
        if ordered_ids:
            filter_sets.append(set(meta_ids))
        else:
            # No object query — metadata search's own order becomes the result.
            ordered_ids = meta_ids

    result_ids = [aid for aid in ordered_ids if all(aid in s for s in filter_sets)]
    logger.info(
        f"search_photos: object={object_query!r} people={len(person_ids)} "
        f"city={city!r} -> {len(result_ids)} ids"
    )
    return result_ids


# ── finalize_search ───────────────────────────────────────────────────────────

FINALIZE_SEARCH_SCHEMA = {
    "name": "finalize_search",
    "description": (
        "Call this exactly once, at the end, to return the final photo results "
        "to the user. Provide the final ordered list of asset ids and a short "
        "plain-language explanation of how you arrived at them (which people/"
        "places/filters you resolved and applied). The explanation is shown to "
        "the user, so make it clear and honest — if results are partial or a "
        "filter could not be resolved, say so."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "asset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Final ordered list of asset id strings.",
            },
            "explanation": {
                "type": "string",
                "description": (
                    "Short user-facing account of what was searched and how the "
                    "results were derived."
                ),
            },
        },
        "required": ["asset_ids", "explanation"],
    },
}


# finalize_search has no executor — it's a control signal. The agent loop
# detects it, extracts asset_ids/explanation, and terminates. Defined here
# only so the schema lives beside the others.


# ── Registry ──────────────────────────────────────────────────────────────────

def build_tool_schemas(include_sql=True):
    """
    Assemble the tool schema list sent to the Anthropic API.

    include_sql toggles run_readonly_sql so the four-tool loop can be proven
    before the SQL tool and its Postgres role exist (README step 4 before 5).
    """
    schemas = [SEARCH_PHOTOS_SCHEMA, FINALIZE_SEARCH_SCHEMA]
    if include_sql:
        from sql_tool import RUN_READONLY_SQL_SCHEMA
        schemas.insert(1, RUN_READONLY_SQL_SCHEMA)
    return schemas
