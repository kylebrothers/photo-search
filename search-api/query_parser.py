"""
Rule-based natural language query parser.

Deliberately isolated behind parse_query() so it can be swapped for an LLM
call later (Phase 5) without touching app.py or immich_client.py.
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
# Kept simple: exact (case-insensitive) name matching, not fuzzy.
_KNOWN_PEOPLE = set()
_KNOWN_LANDMARKS = set()

# Connector/filler words that can be left over after stripping recognized
# names/landmarks (e.g. "Kevin and Kyle" -> "and", "Kevin alone" -> "alone").
# Without this filter, leftover noise words were being sent to CLIP's
# smart_search as if they were real object/scene queries, which degraded or
# zeroed out results that the person/metadata filter alone would have
# answered correctly. Verified against a live bug 2026-07-09.
_STOPWORDS = {
    "and", "alone", "with", "together", "the", "a", "an", "of",
    "at", "in", "on", "by",
}


def load_known_entities(people_names, landmark_names):
    global _KNOWN_PEOPLE, _KNOWN_LANDMARKS
    _KNOWN_PEOPLE = {n.lower() for n in people_names}
    _KNOWN_LANDMARKS = {n.lower() for n in landmark_names}


_LOCATION_PATTERN = re.compile(r"\bin ([A-Z][a-zA-Z\s]+?)(?:\s+(?:in|on|during|from|at)\b|$)")
_DATE_RANGE_PATTERN = re.compile(
    r"\b(?:in|during|from)\s+((?:last|this)\s+\w+|\w+\s+\d{4}|\d{4})\b", re.IGNORECASE
)


def parse_query(text: str) -> ParsedQuery:
    result = ParsedQuery(raw_text=text)
    remaining = text

    # People: match any known name appearing as a substring.
    for name in _KNOWN_PEOPLE:
        if re.search(rf"\b{re.escape(name)}\b", remaining, re.IGNORECASE):
            result.person_names.append(name)
            remaining = re.sub(rf"\b{re.escape(name)}\b", "", remaining, flags=re.IGNORECASE)

    # Landmarks: same approach, against the labeled reference set.
    for landmark in _KNOWN_LANDMARKS:
        if re.search(rf"\b{re.escape(landmark)}\b", remaining, re.IGNORECASE):
            result.landmark_names.append(landmark)
            remaining = re.sub(rf"\b{re.escape(landmark)}\b", "", remaining, flags=re.IGNORECASE)

    # Location: "in <Place>" pattern. Naive — doesn't disambiguate "in the pool" from "in Paris".
    loc_match = _LOCATION_PATTERN.search(remaining)
    if loc_match:
        result.location = loc_match.group(1).strip()
        remaining = remaining.replace(loc_match.group(0), "")

    # Date range: hand off to dateparser for anything date-like.
    date_match = _DATE_RANGE_PATTERN.search(remaining)
    if date_match:
        parsed_date = dateparser.parse(date_match.group(1))
        if parsed_date:
            result.date_from = parsed_date.replace(day=1).isoformat()
            result.date_to = datetime.now().isoformat()
        remaining = remaining.replace(date_match.group(0), "")

    # Drop connector/filler words before deciding there's a real object
    # query left. If nothing meaningful remains, object_query stays empty
    # and app.py correctly falls back to pure metadata/person filtering
    # instead of sending noise to CLIP.
    leftover_words = [w for w in remaining.split() if w.lower() not in _STOPWORDS]
    result.object_query = " ".join(leftover_words)
    return result
