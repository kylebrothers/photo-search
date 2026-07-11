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


_SYSTEM_PROMPT_TEMPLATE = """You extract structured search filters from a natural-language photo search query.

Known people (use EXACTLY these strings if matched, never invent a name): {people}
Known landmarks (use EXACTLY these strings if matched, never invent one): {landmarks}
Known cities actually present in this photo library (map any place name in the query to the closest one of these; if none plausibly matches, leave location null rather than guessing): {cities}

Respond with ONLY a JSON object, no other text, matching this exact schema:
{{
  "person_names": [string, ...],
  "landmark_names": [string, ...],
  "location": string or null,
  "date_from": string or null,
  "date_to": string or null,
  "object_query": string
}}

date_from and date_to, if present, must be ISO 8601 with milliseconds and a
"Z" timezone suffix, e.g. "2026-06-01T00:00:00.000Z" — this exact format,
nothing else, or Immich's API will reject the request.

Rules:
- Only include a person or landmark if it is one of the known values above, matched by meaning (nicknames, partial names, misspellings all count).
- "location" must be one of the known cities above, or null — never a place name that isn't in that list.
- object_query should describe visual content only (objects, scenes, settings) — never include people's names, place names, or dates in it.
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
            "format": "json",
            "stream": False,
            # Unload the model shortly after use rather than keeping it
            # resident in VRAM indefinitely — the GPU device is shared with
            # immich-machine-learning, and this task doesn't need the model
            # kept warm between infrequent search requests.
            "keep_alive": "5m",
        },
        # Bumped from 30s to 60s (2026-07-11) — the 1060 is slower than
        # initially assumed, and 30s was racing against (and sometimes
        # losing to) Gunicorn's own worker timeout, which killed the whole
        # request ungracefully instead of letting this timeout fail
        # cleanly into the rule-based fallback. See search-api/Dockerfile
        # for the corresponding Gunicorn timeout bump — that one must stay
        # LARGER than this value so this timeout wins the race.
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
