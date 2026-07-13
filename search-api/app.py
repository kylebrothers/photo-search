"""
search-api entry point. Combines Immich's separate search modes (CLIP,
people, metadata) into one natural-language query, since Immich's own
search bar only handles one mode at a time. Also proxies thumbnails/downloads
so the browser never needs an Immich API key directly.

/api/search runs the tool-calling search agent (search_agent.py, Claude API),
which supersedes the one-shot query parser as the primary path. The rule-based
parser (query_parser_rules.py) remains as the permanent fallback, invoked
automatically by the agent on total failure.
"""
import logging

# Configure logging at import time — Flask/Gunicorn do NOT configure
# Python's logging module by default, so without this, every logger.info()/
# logger.warning() call in this app and its imports was silently going
# nowhere. Discovered 2026-07-11.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

import anthropic
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
import config
from immich_client import ImmichClient
from search_agent import run_search_agent

logger = logging.getLogger(__name__)

app = Flask(__name__)
immich = ImmichClient()

# Anthropic client for the search agent. If no key is configured, the agent
# path is unavailable and /api/search falls straight through to the rule-based
# parser (search still works, less precisely).
if config.ANTHROPIC_API_KEY:
    claude_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    logger.info(f"Search agent enabled (model: {config.AGENT_MODEL}, "
                f"sql_tool: {config.AGENT_SQL_ENABLED})")
else:
    claude_client = None
    logger.warning("ANTHROPIC_API_KEY not set — search agent unavailable, "
                   "using rule-based parser only")


@app.route("/")
def index():
    return render_template("search.html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "agent_available": claude_client is not None,
        "sql_tool_enabled": config.AGENT_SQL_ENABLED and claude_client is not None,
    })


@app.route("/api/search", methods=["POST"])
def search():
    body = request.get_json(force=True)
    text = body.get("query", "")
    if not text.strip():
        return jsonify({"error": "query is required"}), 400

    if claude_client is not None:
        agent_result = run_search_agent(text, immich, claude_client)
        asset_ids = agent_result["asset_ids"]
        explanation = agent_result["explanation"]
        trace = agent_result["trace"]
        fell_back = agent_result["fell_back"]
    else:
        # No API key configured — use the rule-based parser directly.
        import query_parser_rules as rules
        import tools as tools_mod
        parsed = rules.parse_query(text)
        person_ids = [pid for pid in (immich.find_person_id(n)
                                      for n in parsed.person_names) if pid]
        asset_ids = tools_mod.execute_search_photos(
            immich, object_query=parsed.object_query, person_ids=person_ids,
            city=parsed.location, date_from=parsed.date_from, date_to=parsed.date_to,
        )
        explanation = "Rule-based parser (search agent not configured)."
        trace = []
        fell_back = True

    results = [
        {
            "id": aid,
            "view_url": immich.view_url(aid),
            "thumbnail_url": f"/proxy/thumbnail/{aid}",
            "download_url": f"/proxy/download/{aid}",
        }
        for aid in asset_ids
    ]
    return jsonify({
        "query": text,
        "explanation": explanation,
        "results": results,
        "trace": trace,
        "fell_back": fell_back,
    })


@app.route("/proxy/thumbnail/<asset_id>")
def proxy_thumbnail(asset_id):
    content, content_type = immich.thumbnail_response(asset_id)
    return Response(content, mimetype=content_type)


@app.route("/proxy/download/<asset_id>")
def proxy_download(asset_id):
    if not immich.asset_exists(asset_id):
        return jsonify({"error": "This photo may have moved or been deleted — try refreshing your search."}), 404

    upstream = immich.original_stream(asset_id)
    return Response(
        stream_with_context(upstream.iter_content(chunk_size=8192)),
        content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
        headers={"Content-Disposition": upstream.headers.get("Content-Disposition", "attachment")},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
