"""
Dispatcher for query parsing backends.

Exposes the same interface (ParsedQuery, load_known_entities, parse_query)
regardless of backend, so app.py never needs to change when switching modes.

QUERY_PARSER_MODE env var selects the backend:
  "llm"   (default) — query_parser_llm.py, grounded with real Immich data
  "rules" — query_parser_rules.py, the original regex-based parser

Falls back to "rules" automatically if the LLM backend raises at parse time
(Ollama unreachable, malformed JSON, etc.), so a transient outage degrades
search quality rather than crashing it outright.
"""
import logging

import config
from query_parser_rules import ParsedQuery, load_known_entities as _load_rules, parse_query as _parse_rules

logger = logging.getLogger(__name__)

_MODE = config.QUERY_PARSER_MODE  # "llm" or "rules"

if _MODE == "llm":
    from query_parser_llm import load_known_entities as _load_llm, parse_query as _parse_llm


def load_known_entities(people_names, landmark_names, cities=None):
    # Rules backend is always loaded, regardless of active mode — it's the
    # fallback path, so it needs to be ready even when not primary.
    _load_rules(people_names, landmark_names)
    if _MODE == "llm":
        _load_llm(people_names, landmark_names, cities or [])


def parse_query(text: str) -> ParsedQuery:
    if _MODE == "llm":
        try:
            return _parse_llm(text)
        except Exception as e:
            logger.warning(f"LLM query parser failed ({e}) — falling back to rule-based parser for this query")
            return _parse_rules(text)
    return _parse_rules(text)
