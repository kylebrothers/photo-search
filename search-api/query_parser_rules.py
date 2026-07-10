"""
Rule-based natural language query parser — kept as an explicit fallback
behind query_parser.py's dispatcher (see that file), not relied on as the
primary parser anymore. Exposes the same interface as query_parser_llm.py
so app.py never needs to know which backend is active.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
import dateparser  # pip install dateparser


@dataclass
class ParsedQuery:
    raw_text: str
    object_query: str = ""          # residual free text, sent to CLIP smart search
    person_names: list = field(default_factory=list)
    landmark_names: list = field(default_factory=list)
    location: str = None
    date_from: str = None
    date_to: str = None


# Populated at startup from Immich's people list + the landmark reference set.
_KNOWN_PEOPLE = set()          # full lowercase names, e.g. "kyle brothers"
_PERSON_TOKEN_MAP = {}         # unambiguous single-word alias -> full lowercase name
_KNOWN_LANDMARKS = set()

# Connector/filler words that can be left over after stripping recognized
# names/landmarks (e.g. "Kevin and Kyle" -> "and", "Kevin alone" -> "alone").
# Without this filter, leftover noise words were being sent to CLIP's
# smart_search as if they were real object/scene queries. Verified against
# a live bug 2026-07-09.
_STOPWORDS = {
    "and", "alone", "with", "together", "the", "a", "an", "of",
    "at", "in", "on", "by",
}


def load_known_entities(people_names, landmark_names):
    global _KNOWN_PEOPLE, _PERSON_TOKEN_MAP, _KNOWN_LANDMARKS
    full_names = [n.lower() for n in people_names]
    _KNOWN_PEOPLE = set(full_names)
    _KNOWN_LANDMARKS = {n.lower() for n in landmark_names}

    # Map each individual word in a full name to that full name, but only
    # when the word doesn't belong to more than one registered person —
    # e.g. two people both named "Kevin" leaves "kevin" unregistered as an
    # alias, forcing full-name entry for both rather than guessing wrong.
    # Fixes a verified bug (2026-07-10): full-name-only matching meant
    # "Kyle" alone never matched "Kyle Brothers", falling through to CLIP
    # as meaningless literal text search.
    token_owners = {}
    for full in full_names:
        for word in full.split():
            token_owners.setdefault(word, set()).add(full)
    _PERSON_TOKEN_MAP = {w: next(iter(owners)) for w, owners in token_owners.items() if len(owners) == 1}


_LOCATION_PATTERN = re.compile(r"\bin ([A-Z][a-zA-Z\s]+?)(?:\s+(?:in|on|during|from|at)\b|$)")
_DATE_RANGE_PATTERN = re.compile(
    r"\b(?:in|during|from)\s+((?:last|this)\s+\w+|\w+\s+\d{4}|\d{4})\b", re.IGNORECASE
)


def parse_query(text: str) -> ParsedQuery:
    result = ParsedQuery(raw_text=text)
    remaining = text

    # People: full names first (longest first, so "Kyle Brothers" isn't
    # partially consumed before matching), then unambiguous single-word
    # aliases for anything not already matched.
    for name in sorted(_KNOWN_PEOPLE, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", remaining, re.IGNORECASE):
            result.person_names.append(name)
            remaining = re.sub(rf"\b{re.escape(name)}\b", "", remaining, flags=re.IGNORECASE)

    for alias, full_name in _PERSON_TOKEN_MAP.items():
        if full_name in result.person_names:
            continue
        if re.search(rf"\b{re.escape(alias)}\b", remaining, re.IGNORECASE):
            result.person_names.append(full_name)
            remaining = re.sub(rf"\b{re.escape(alias)}\b", "", remaining, flags=re.IGNORECASE)

    # Landmarks: same approach, against the labeled reference set.
    for landmark in _KNOWN_LANDMARKS:
        if re.search(rf"\b{re.escape(landmark)}\b", remaining, re.IGNORECASE):
            result.landmark_names.append(landmark)
            remaining = re.sub(rf"\b{re.escape(landmark)}\b", "", remaining, flags=re.IGNORECASE)

    # Location: "in <Place>" pattern. Naive — doesn't disambiguate "in the
    # pool" from "in Paris", and doesn't know Immich's actual stored city
    # granularity (e.g. "Manhattan" vs. colloquial "New York") — a real,
    # unresolved gap in this parser, part of why the LLM backend exists.
    loc_match = _LOCATION_PATTERN.search(remaining)
    if loc_match:
        result.location = loc_match.group(1).strip()
        remaining = remaining.replace(loc_match.group(0), "")

    # Date range: hand off to dateparser for anything date-like.
    # Format fix (2026-07-10): Immich's API requires full ISO 8601 with a
    # timezone (e.g. "2026-06-01T00:00:00.000Z"); Python's bare .isoformat()
    # has no timezone and was causing a 400 from Immich's own validation.
    date_match = _DATE_RANGE_PATTERN.search(remaining)
    if date_match:
        parsed_date = dateparser.parse(date_match.group(1))
        if parsed_date:
            result.date_from = parsed_date.replace(day=1).strftime("%Y-%m-%dT00:00:00.000Z")
            result.date_to = datetime.now().strftime("%Y-%m-%dT23:59:59.999Z")
        remaining = remaining.replace(date_match.group(0), "")

    # Drop connector/filler words before deciding there's a real object
    # query left.
    leftover_words = [w for w in remaining.split() if w.lower() not in _STOPWORDS]
    result.object_query = " ".join(leftover_words)
    return result
