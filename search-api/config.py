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
