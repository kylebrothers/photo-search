"""
LLM-based natural language query parser, grounded with real data from the
live Immich instance (registered people, labeled landmarks, and actual
stored city values) so the model maps colloquial phrasing to real records
instead of guessing from its own training knowledge.

Exposes the same interface as query_parser_rules.py (ParsedQuery,
load_known_entities, parse_query) so query_parser.py can dispatch to either
without app.py knowing the difference.
"""
import json
import logging
from datetime import datetime, timezone

import requests

import config
from query_parser_rules import ParsedQuery

logger = logging.getLogger(__name__)

_PEOPLE = []
_LANDMARKS = []
_CITIES = []


def load_known_entities(people_names, landmark_names, cities):
    global _PEOPLE, _LANDMARKS, _CITIES
    _PEOPLE = list(people_names)
    _LANDMARKS = list(landmark_names)
    _CITIES = list(cities)


# Full JSON Schema, not just the bare string "json" — Ollama constrains
# token generation to actually conform to this schema (field names, types),
# not just "is syntactically valid JSON somehow". Switched 2026-07-11 after
# the bare "json" format let the model produce schema-shaped-but-meaningless
# output (e.g. object_query="house" for a query with no relation to houses).
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "person_names": {"type": "array", "items": {"type": "string"}},
        "landmark_names": {"type": "array", "items": {"type": "string"}},
        "location": {"type": ["string", "null"]},
        "date_from": {"type": ["string", "null"]},
        "date_to": {"type": ["string", "null"]},
        "object_query": {"type": "string"},
    },
    "required": ["person_names", "landmark_names", "location", "date_from", "date_to", "object_query"],
}

_SYSTEM_PROMPT_TEMPLATE = """You extract structured search filters from a natural-language photo search query.

Known people (use EXACTLY these strings if matched, never invent a name): {people}
Known landmarks (use EXACTLY these strings if matched, never invent one): {landmarks}
Known cities actually present in this photo library (map any place name in the query to the closest one of these; if none plausibly matches, leave location null rather than guessing): {cities}

Example — given known people ["Alex Rivera"], known cities ["Chicago"], and the
query "Alex in Chicago at a park", the correct output is:
{{"person_names": ["Alex Rivera"], "landmark_names": [], "location": "Chicago", "date_from": null, "date_to": null, "object_query": "park"}}

Now do the same for the real query below, using ONLY the real known values listed above (not the example's).

date_from and date_to, if present, must be ISO 8601 with milliseconds and a
"Z" timezone suffix, e.g. "2026-06-01T00:00:00.000Z".

Rules:
- Only include a person or landmark if it is one of the known values above, matched by meaning (nicknames, partial names, misspellings all count).
- "location" must be one of the known cities above, or null.
- object_query must only contain words describing visual content actually implied by the query — never invent unrelated words, never include names/places/dates.
- If empty, object_query should be an empty string, not a guess.
- If the query mentions a relative date ("last summer", "this year"), convert it to a real date range based on today's date: {today}.
"""


def _build_system_prompt(today_iso):
    return _SYSTEM_PROMPT_TEMPLATE.format(
        people=json.dumps(_PEOPLE),
        landmarks=json.dumps(_LANDMARKS),
        cities=json.dumps(_CITIES),
        today=today_iso,
    )


def parse_query(text: str) -> ParsedQuery:
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system_prompt = _build_system_prompt(today_iso)

    response = requests.post(
        f"{config.OLLAMA_URL}/api/generate",
        json={
            "model": config.OLLAMA_MODEL,
            "system": system_prompt,
            "prompt": text,
            "format": _RESPONSE_SCHEMA,   # real schema, not just the bare string "json"
            "stream": False,
            "keep_alive": "5m",
            # Deterministic extraction, not creative generation — a default
            # temperature (~0.7-0.8) was a likely contributor to the model
            # producing unrelated words in object_query.
            "options": {"temperature": 0},
        },
        # Must stay smaller than search-api/Dockerfile's Gunicorn --timeout
        # (90s), so this timeout wins the race and fails gracefully into
        # the rule-based fallback instead of Gunicorn killing the worker.
        timeout=60,
    )
    response.raise_for_status()
    raw = response.json().get("response", "")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned non-JSON output: {raw!r}") from e

    result = ParsedQuery(raw_text=text)

    # Defense in depth: filter to known values even though the prompt
    # already instructs the model not to invent names/places, in case it
    # doesn't follow that instruction exactly.
    result.person_names = [p for p in parsed.get("person_names", []) if p in _PEOPLE]
    result.landmark_names = [l for l in parsed.get("landmark_names", []) if l in _LANDMARKS]
    location = parsed.get("location")
    result.location = location if location in _CITIES else None
    result.date_from = parsed.get("date_from") or None
    result.date_to = parsed.get("date_to") or None
    result.object_query = (parsed.get("object_query") or "").strip()

    logger.info(f"LLM parsed {text!r} -> {result}")
    return result
