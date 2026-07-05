"""
search-api entry point. Combines Immich's separate search modes (CLIP,
people, metadata) into one natural-language query, since Immich's own
search bar only handles one mode at a time. Also proxies thumbnails/downloads
so the browser never needs an Immich API key directly.
"""
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
import config
import query_parser
from immich_client import ImmichClient

app = Flask(__name__)
immich = ImmichClient()

_initialized = False


def init_known_entities():
    """
    One-time setup: load known people and landmark names into query_parser.
    Deferred to first request (see _ensure_initialized) rather than run at
    import time, since immich-server may not be reachable yet when this
    container starts — Flask's before_first_request hook, which used to
    handle exactly this case, was removed in Flask 2.3+.
    """
    global _initialized
    people = [p["name"] for p in immich.get_people().get("people", []) if p.get("name")]
    from landmark.reference_embeddings import list_landmarks
    query_parser.load_known_entities(people, list_landmarks())
    _initialized = True


def _ensure_initialized():
    if not _initialized:
        init_known_entities()


@app.route("/")
def index():
    return render_template("search.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/search", methods=["POST"])
def search():
    _ensure_initialized()

    body = request.get_json(force=True)
    text = body.get("query", "")
    if not text.strip():
        return jsonify({"error": "query is required"}), 400

    parsed = query_parser.parse_query(text)

    # Collect asset ID sets from each relevant search mode, then intersect.
    result_sets = []

    if parsed.object_query:
        smart_results = immich.smart_search(parsed.object_query)
        result_sets.append({a["id"] for a in smart_results.get("assets", {}).get("items", [])})

    if parsed.person_names or parsed.location or parsed.date_from:
        person_ids = [pid for pid in (immich.find_person_id(n) for n in parsed.person_names) if pid]
        meta_results = immich.search_metadata(
            city=parsed.location, date_from=parsed.date_from, date_to=parsed.date_to,
            person_ids=person_ids or None,
        )
        result_sets.append({a["id"] for a in meta_results.get("assets", {}).get("items", [])})

    # Landmark filtering happens post-hoc against whatever the above returned,
    # since Immich has no landmark concept of its own.
    asset_ids = set.intersection(*result_sets) if result_sets else set()

    if parsed.landmark_names:
        from landmark.match import match_landmarks
        asset_ids = {
            aid for aid in asset_ids
            if any(name in [m[0] for m in match_landmarks(aid)] for name in parsed.landmark_names)
        }

    results = [
        {
            "id": aid,
            "view_url": immich.view_url(aid),
            "thumbnail_url": f"/proxy/thumbnail/{aid}",
            "download_url": f"/proxy/download/{aid}",
        }
        for aid in asset_ids
    ]
    return jsonify({"query": text, "parsed": parsed.__dict__, "results": results})


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
