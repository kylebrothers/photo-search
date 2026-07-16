"""
tools.py — Tool implementations and Anthropic tool schemas for the
search agent (see search_agent.py).

Reference-based results
───────────────────────
Retrieval tools do NOT return photo id lists to the model. Each stores its
result in a per-request ResultStore and returns a `handle` + `count`; the model
manipulates handles, never bulk ids. The only ids the model sees are small
VALUE lookups (a person id, a city name) returned inline by run_readonly_sql.

Set algebra, two layers
───────────────────────
The model expresses AND / OR / NOT over photo sets at two levels:

  1. WITHIN a search_photos call — each multi-value filter carries its own
     match mode, chosen by the model:
        people = {ids:[...], match:"all"|"any"}   all = contains every person
                                                  any = contains any of them
        cities = {values:[...], match:"any"|"all"} any = in any of these cities
                                                  (all is meaningless for >1,
                                                   handled as empty)
     Different filter TYPES combine with AND (people AND cities AND date).
     object_query stays single — it's a CLIP *ranking*, not a set, so multiple
     objects are done as separate searches combined at layer 2.

  2. ACROSS result sets — combine_results(base, filters, mode) with
     mode in {union, intersect, subtract} = the full boolean algebra (OR/AND/
     NOT). base carries the ordering; filters are applied by handle. This is
     how objects, SQL-only predicates, and cross-tool conditions combine.

Immich's search_metadata natively ANDs its filters and takes a single city, so
"any" modes fan out into per-value calls that are unioned here in Python; the
model sees one clean call.
"""

import logging

logger = logging.getLogger(__name__)


# ── Per-request result store ──────────────────────────────────────────────────

class ResultStore:
    """
    Holds ordered asset-id lists for one search request only. Created fresh in
    run_search_agent per call, discarded when the request ends — handles are
    never valid across requests, so there's no cross-request state or thread
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
        return self._results.get(handle)


# ── small helpers ─────────────────────────────────────────────────────────────

def _items(resp):
    """Asset ids from an Immich search response, in returned order."""
    return [a["id"] for a in resp.get("assets", {}).get("items", [])]


def _dedup(seq):
    """Order-preserving dedup."""
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ── metadata filtering with per-filter match modes ───────────────────────────

def _metadata_asset_ids(immich, people, cities, date_from, date_to):
    """
    Return (ordered_ids, present).

    Builds the set of asset ids satisfying
        (people condition) AND (cities condition) AND (date)
    where the people/cities conditions each honour their own match mode.
    `present` is False when no metadata filter was specified at all.

    Immich ANDs person_ids and takes one city, so:
      - people match="all"  -> one call, person_ids=[all]  (contains all)
      - people match="any"  -> per-person calls, unioned
      - cities match="any"  -> per-city calls, unioned
      - cities match="all"  -> meaningless for >1 (a photo has one city) -> empty
    People-set and cities-set (different filter types) are intersected (AND).
    """
    people = people or {}
    cities = cities or {}
    people_ids = people.get("ids") or []
    people_match = (people.get("match") or "all").lower()
    city_values = cities.get("values") or []
    cities_match = (cities.get("match") or "any").lower()

    has_people = bool(people_ids)
    has_cities = bool(city_values)
    has_date = bool(date_from)
    if not (has_people or has_cities or has_date):
        return [], False

    people_set = None
    if has_people:
        if people_match == "all":
            people_set = _items(immich.search_metadata(
                person_ids=people_ids, date_from=date_from, date_to=date_to))
        else:  # any
            acc = []
            for pid in people_ids:
                acc.extend(_items(immich.search_metadata(
                    person_ids=[pid], date_from=date_from, date_to=date_to)))
            people_set = _dedup(acc)

    cities_set = None
    if has_cities:
        if cities_match == "all" and len(city_values) > 1:
            cities_set = []  # a photo can't be in more than one city
        else:
            acc = []
            for c in city_values:
                acc.extend(_items(immich.search_metadata(
                    city=c, date_from=date_from, date_to=date_to)))
            cities_set = _dedup(acc)

    # Only a date filter: one metadata call.
    if people_set is None and cities_set is None:
        return _items(immich.search_metadata(
            date_from=date_from, date_to=date_to)), True

    # Intersect the specified filter-type sets (AND across types), preserving
    # the first set's order.
    sets = [s for s in (people_set, cities_set) if s is not None]
    base = sets[0]
    others = [set(s) for s in sets[1:]]
    combined = [aid for aid in base if all(aid in o for o in others)]
    return combined, True


# ── Ranked retrieval core (also used by the fallback path) ────────────────────

def run_ranked_search(immich, object_query="", people=None, cities=None,
                      date_from=None, date_to=None):
    """
    Ranked retrieval → ordered list of asset id strings.

    CLIP smart_search establishes relevance order when object_query is given;
    the metadata filter (people/cities/date, each with its match mode) is
    applied as a membership filter on that order. With no object_query, the
    metadata result is returned directly.
    """
    object_query = (object_query or "").strip()

    ordered_ids = []
    if object_query:
        ordered_ids = _items(immich.smart_search(object_query))

    meta_ids, meta_present = _metadata_asset_ids(
        immich, people, cities, date_from, date_to)

    if meta_present:
        if ordered_ids:
            meta_set = set(meta_ids)
            result = [aid for aid in ordered_ids if aid in meta_set]
        else:
            result = meta_ids
    else:
        result = ordered_ids

    logger.info(
        f"ranked_search: object={object_query!r} "
        f"people={(people or {}).get('ids')}/{(people or {}).get('match')} "
        f"cities={(cities or {}).get('values')}/{(cities or {}).get('match')} "
        f"-> {len(result)} ids"
    )
    return result


# ── search_photos (handle-returning tool wrapper) ─────────────────────────────

SEARCH_PHOTOS_SCHEMA = {
    "name": "search_photos",
    "description": (
        "Retrieve photos ranked by relevance and store them as a result set. "
        "Use for object/scene search (CLIP) optionally filtered by people, "
        "cities, and/or a date range. Each multi-value filter carries a match "
        "mode you choose: people.match 'all' = photos containing EVERY listed "
        "person (e.g. 'Kevin AND Sarah together'), 'any' = containing ANY of "
        "them ('Kevin OR Sarah'); cities.match 'any' = taken in ANY of the "
        "listed cities (use this for a region — list its cities). Resolve "
        "person names to ids and place names to real stored city values with "
        "run_readonly_sql FIRST. object_query is a single description — for "
        "multiple objects, do separate searches and combine_results them. "
        "Returns a handle + count, not ids."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "object_query": {
                "type": "string",
                "description": (
                    "Single free-text visual description for CLIP (e.g. 'sunset "
                    "over water'). Empty string for a pure metadata search. For "
                    "multiple objects use separate searches + combine_results."
                ),
            },
            "people": {
                "type": ["object", "null"],
                "description": (
                    "Person filter, or null. ids: list of person UUIDs "
                    "(resolve names first). match: 'all' (contains every "
                    "person) or 'any' (contains any). Default 'all'."
                ),
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "match": {"type": "string", "enum": ["all", "any"]},
                },
            },
            "cities": {
                "type": ["object", "null"],
                "description": (
                    "City filter, or null. values: list of EXACT stored city "
                    "values (resolve colloquial names first). match: 'any' (in "
                    "any listed city — use for regions) or 'all' (only "
                    "meaningful for a single city). Default 'any'."
                ),
                "properties": {
                    "values": {"type": "array", "items": {"type": "string"}},
                    "match": {"type": "string", "enum": ["any", "all"]},
                },
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
        "required": ["object_query", "people", "cities", "date_from", "date_to"],
    },
}


def execute_search_photos(immich, store, object_query="", people=None,
                          cities=None, date_from=None, date_to=None):
    """Run ranked retrieval, store the result, return {handle, count}."""
    ids = run_ranked_search(
        immich, object_query=object_query, people=people, cities=cities,
        date_from=date_from, date_to=date_to,
    )
    handle = store.put(ids)
    return {"handle": handle, "count": len(ids)}


# ── combine_results ───────────────────────────────────────────────────────────

COMBINE_RESULTS_SCHEMA = {
    "name": "combine_results",
    "description": (
        "Combine stored result sets by handle in one exact server-side "
        "operation — use this instead of merging photo ids yourself. "
        "base_handle provides the ordering; each filter handle is a set. "
        "mode='union' keeps items in the base OR any filter (e.g. 'beach "
        "photos' plus 'mountain photos'); mode='intersect' keeps items in the "
        "base AND all filters (e.g. 'beach photos' AND 'only-Kevin-in-frame'); "
        "mode='subtract' keeps base items in NONE of the filters. Returns a new "
        "handle and count."
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
                "description": "Handles to union/intersect/subtract against the base.",
            },
            "mode": {
                "type": "string",
                "enum": ["union", "intersect", "subtract"],
                "description": (
                    "union: base OR any filter. intersect: base AND all "
                    "filters. subtract: base minus any filter."
                ),
            },
        },
        "required": ["base_handle", "filter_handles", "mode"],
    },
}


def execute_combine_results(store, base_handle, filter_handles, mode):
    """Union/intersect/subtract stored result sets, preserving base ordering."""
    base = store.get(base_handle)
    if base is None:
        return {"error": f"unknown base_handle {base_handle!r}"}

    filter_lists = []
    for h in filter_handles:
        ids = store.get(h)
        if ids is None:
            return {"error": f"unknown filter handle {h!r}"}
        filter_lists.append(ids)

    if mode == "intersect":
        filter_sets = [set(l) for l in filter_lists]
        combined = [aid for aid in base if all(aid in s for s in filter_sets)]
    elif mode == "subtract":
        filter_sets = [set(l) for l in filter_lists]
        combined = [aid for aid in base if not any(aid in s for s in filter_sets)]
    elif mode == "union":
        # base order first, then each filter's ids not already present.
        seen = set()
        combined = []
        for src in [base, *filter_lists]:
            for aid in src:
                if aid not in seen:
                    seen.add(aid)
                    combined.append(aid)
    else:
        return {"error": f"unknown mode {mode!r} (use union/intersect/subtract)"}

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
        "user. Pass the handle of the result set to return (from search_photos, "
        "run_readonly_sql, or combine_results) and a short honest explanation "
        "of how you got there. IMPORTANT: the explanation must match the handle "
        "you return — do not return one set while describing another. Do NOT "
        "list photo ids; just pass the handle."
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
                    "the results were derived. Must describe the SAME set as "
                    "the handle."
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
    Assemble the tool schema list sent to the Anthropic API. combine_results is
    always included; include_sql toggles run_readonly_sql so the loop can be
    proven before the SQL tool's Postgres role exists.
    """
    schemas = [SEARCH_PHOTOS_SCHEMA, COMBINE_RESULTS_SCHEMA, FINALIZE_SEARCH_SCHEMA]
    if include_sql:
        from sql_tool import RUN_READONLY_SQL_SCHEMA
        schemas.insert(1, RUN_READONLY_SQL_SCHEMA)
    return schemas
