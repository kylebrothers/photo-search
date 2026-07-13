"""
tools.py — Tool implementations and Anthropic tool schemas for the
search agent (see search_agent.py).

Reference-based results
───────────────────────
Retrieval tools do NOT return photo id lists to the model. Bulk asset ids are
opaque payload the model can't reason about — routing them through the context
costs tokens, adds latency, and (as observed) risks truncation. Instead each
retrieval tool stores its result in a per-request ResultStore and returns a
`handle` + `count`. The model manipulates handles ("result_1, 46 photos"), and
the actual ids never enter the context. finalize_search resolves the chosen
handle back to ids server-side.

The only ids that ever reach the model are small VALUE lookups (a single
person UUID, a city name) returned inline by run_readonly_sql — those the
model genuinely needs to read to use as arguments.

Thin tool set (people/city/landmark structured resolvers were deliberately
dropped — see README). What remains:

  search_photos    — ranked CLIP+metadata retrieval. Ranking preserved in
                     Python (CLIP order; metadata as membership filter).
                     Returns {handle, count}.
  combine_results  — intersect/subtract result sets by handle, in Python, so
                     the model never merges id lists itself. base_handle
                     carries the ranking; filter_handles are membership sets.
  finalize_search  — end-of-loop signal; takes a handle + explanation.

run_readonly_sql lives in sql_tool.py (dual-path: photo results -> handle,
value lookups -> inline rows).
"""

import logging

logger = logging.getLogger(__name__)


# ── Per-request result store ──────────────────────────────────────────────────

class ResultStore:
    """
    Holds ordered asset-id lists for one search request only. Created fresh in
    run_search_agent per call, discarded when the request ends — so handles are
    never valid across requests and there's no cross-request state or thread
    safety concern (Gunicorn sync workers each handle one request at a time).
    """

    def __init__(self):
        self._results = {}
        self._counter = 0

    def put(self, asset_ids):
        self._counter += 1
        handle = f"result_{self._counter}"
        self._results[handle] = list(asset_ids)
        return handle

    def get(self, handle):
        """Return the ordered id list for a handle, or None if unknown."""
        return self._results.get(handle)


# ── Ranked retrieval core (also used by the fallback path) ────────────────────

def run_ranked_search(immich, object_query="", person_ids=None,
                      city=None, date_from=None, date_to=None):
    """
    Ranked retrieval, returning an ordered list of asset id strings.

    Mirrors the rank-then-filter behaviour that previously lived in app.py:
    CLIP smart_search establishes relevance order; metadata matches are applied
    only as a membership FILTER on that order, never as the result list itself.

    This is the reusable core. execute_search_photos wraps it into a handle for
    the agent; the rules fallback and the no-API-key path call it directly for
    raw ids.
    """
    person_ids = person_ids or []
    object_query = (object_query or "").strip()

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
        f"ranked_search: object={object_query!r} people={len(person_ids)} "
        f"city={city!r} -> {len(result_ids)} ids"
    )
    return result_ids


# ── search_photos (handle-returning tool wrapper) ─────────────────────────────

SEARCH_PHOTOS_SCHEMA = {
    "name": "search_photos",
    "description": (
        "Retrieve photos ranked by relevance and store them as a result set. "
        "Use for the common case: an object/scene description (e.g. 'beach', "
        "'dog on a sofa') optionally combined with a known person, a city, "
        "and/or a date range. CLIP semantic search establishes the ranking "
        "when object_query is given; person/city/date act as filters. Resolve "
        "person names to person_ids and place names to a real stored city "
        "value BEFORE calling this (use run_readonly_sql for that). Returns a "
        "handle and a count — NOT the photo ids. Pass the handle to "
        "finalize_search, or to combine_results to intersect/subtract with "
        "another result set."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "object_query": {
                "type": "string",
                "description": (
                    "Free-text visual description for CLIP semantic search "
                    "(e.g. 'sunset over water'). Empty string for a pure "
                    "metadata search."
                ),
            },
            "person_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Immich person UUIDs to filter by (photos containing ALL "
                    "of them). Resolve names via run_readonly_sql first. Empty "
                    "if none."
                ),
            },
            "city": {
                "type": ["string", "null"],
                "description": (
                    "Exact stored EXIF city value (e.g. 'Manhattan', not 'New "
                    "York'). Resolve colloquial names via run_readonly_sql "
                    "first. Null if none."
                ),
            },
            "date_from": {
                "type": ["string", "null"],
                "description": (
                    "ISO 8601 with milliseconds and Z, e.g. "
                    "'2026-06-01T00:00:00.000Z'. Null if no lower bound."
                ),
            },
            "date_to": {
                "type": ["string", "null"],
                "description": "ISO 8601 with milliseconds and Z. Null if no upper bound.",
            },
        },
        "required": ["object_query", "person_ids", "city", "date_from", "date_to"],
    },
}


def execute_search_photos(immich, store, object_query="", person_ids=None,
                          city=None, date_from=None, date_to=None):
    """Run ranked retrieval, store the result, return {handle, count}."""
    ids = run_ranked_search(
        immich, object_query=object_query, person_ids=person_ids,
        city=city, date_from=date_from, date_to=date_to,
    )
    handle = store.put(ids)
    return {"handle": handle, "count": len(ids)}


# ── combine_results ───────────────────────────────────────────────────────────

COMBINE_RESULTS_SCHEMA = {
    "name": "combine_results",
    "description": (
        "Combine two or more stored result sets by handle, in one exact "
        "server-side operation — use this instead of trying to merge photo "
        "ids yourself. base_handle provides the ordering (e.g. the CLIP-ranked "
        "search_photos result); each filter handle is treated as a membership "
        "set. mode='intersect' keeps base items present in ALL filter sets "
        "(e.g. 'beach photos' intersected with 'photos where only Kevin is in "
        "frame'); mode='subtract' keeps base items present in NONE of them. "
        "Returns a new handle and count."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "base_handle": {
                "type": "string",
                "description": "Handle whose ordering is preserved in the output.",
            },
            "filter_handles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Handles treated as membership sets over the base.",
            },
            "mode": {
                "type": "string",
                "enum": ["intersect", "subtract"],
                "description": (
                    "intersect: keep base items in ALL filter sets. "
                    "subtract: keep base items in NONE of them."
                ),
            },
        },
        "required": ["base_handle", "filter_handles", "mode"],
    },
}


def execute_combine_results(store, base_handle, filter_handles, mode):
    """Intersect/subtract stored result sets, preserving base ordering."""
    base = store.get(base_handle)
    if base is None:
        return {"error": f"unknown base_handle {base_handle!r}"}

    filter_sets = []
    for h in filter_handles:
        ids = store.get(h)
        if ids is None:
            return {"error": f"unknown filter handle {h!r}"}
        filter_sets.append(set(ids))

    if mode == "intersect":
        combined = [aid for aid in base if all(aid in s for s in filter_sets)]
    elif mode == "subtract":
        combined = [aid for aid in base if not any(aid in s for s in filter_sets)]
    else:
        return {"error": f"unknown mode {mode!r} (use 'intersect' or 'subtract')"}

    handle = store.put(combined)
    logger.info(
        f"combine_results: {mode} base={base_handle}({len(base)}) "
        f"filters={filter_handles} -> {handle}({len(combined)})"
    )
    return {"handle": handle, "count": len(combined)}


# ── finalize_search ───────────────────────────────────────────────────────────

FINALIZE_SEARCH_SCHEMA = {
    "name": "finalize_search",
    "description": (
        "Call this exactly once, at the end, to return the final photos to the "
        "user. Pass the handle of the result set you want returned (from "
        "search_photos, run_readonly_sql, or combine_results) and a short "
        "plain-language explanation of how you got there (which people/places/"
        "filters you resolved and applied). The explanation is shown to the "
        "user — be honest if results are partial or a filter couldn't be "
        "resolved. Do NOT list photo ids; just pass the handle."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "Handle of the result set to return.",
            },
            "explanation": {
                "type": "string",
                "description": (
                    "Short user-facing account of what was searched and how "
                    "the results were derived."
                ),
            },
        },
        "required": ["handle", "explanation"],
    },
}


# finalize_search has no executor — it's a control signal resolved by the agent
# loop, which looks the handle up in the store and terminates.


# ── Registry ──────────────────────────────────────────────────────────────────

def build_tool_schemas(include_sql=True):
    """
    Assemble the tool schema list sent to the Anthropic API.

    combine_results is always included (it operates on handles regardless of
    where they came from). include_sql toggles run_readonly_sql so the loop can
    be proven before the SQL tool's Postgres role exists.
    """
    schemas = [SEARCH_PHOTOS_SCHEMA, COMBINE_RESULTS_SCHEMA, FINALIZE_SEARCH_SCHEMA]
    if include_sql:
        from sql_tool import RUN_READONLY_SQL_SCHEMA
        schemas.insert(1, RUN_READONLY_SQL_SCHEMA)
    return schemas
