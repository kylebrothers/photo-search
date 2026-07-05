"""
Thin wrapper over Immich's public REST API.
Covers everything except raw embedding retrieval (see landmark/match.py + db.py
for the one place we go around the API instead of through it).
"""
import requests
import config


class ImmichClient:
    def __init__(self, base_url=None, api_key=None):
        self.base_url = (base_url or config.IMMICH_URL).rstrip("/")
        self.headers = {"x-api-key": api_key or config.IMMICH_API_KEY}

    def _get(self, path, **kwargs):
        r = requests.get(f"{self.base_url}{path}", headers=self.headers, **kwargs)
        r.raise_for_status()
        return r.json()

    def _post(self, path, json=None, **kwargs):
        r = requests.post(f"{self.base_url}{path}", headers=self.headers, json=json, **kwargs)
        r.raise_for_status()
        return r.json()

    def smart_search(self, text, page=1, size=100):
        """CLIP-based semantic search — covers object/scene description."""
        return self._post("/api/search/smart", json={"query": text, "page": page, "size": size})

    def search_metadata(self, city=None, date_from=None, date_to=None, person_ids=None, page=1, size=100):
        """Structured filters — location (from EXIF reverse geocode), date range, people."""
        body = {"page": page, "size": size}
        if city:
            body["city"] = city
        if date_from:
            body["takenAfter"] = date_from
        if date_to:
            body["takenBefore"] = date_to
        if person_ids:
            body["personIds"] = person_ids
        return self._post("/api/search/metadata", json=body)

    def get_people(self):
        """List known (named) people, for resolving a name in a query to a person ID."""
        return self._get("/api/people")

    def find_person_id(self, name):
        people = self.get_people().get("people", [])
        name_lower = name.lower()
        for p in people:
            if p.get("name", "").lower() == name_lower:
                return p["id"]
        return None

    def get_asset(self, asset_id):
        return self._get(f"/api/assets/{asset_id}")

    def asset_exists(self, asset_id):
        """Used for the existence check before serving a download (see README point 2)."""
        try:
            self.get_asset(asset_id)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return False
            raise

    def view_url(self, asset_id):
        """Link to Immich's own web viewer — a real page load; the user's own
        Immich login (if any) applies here, independent of search-api's proxy."""
        return f"{self.base_url}/photos/{asset_id}"

    def thumbnail_response(self, asset_id, size="thumbnail"):
        """
        Fetch a thumbnail server-side (with our API key) so the browser never
        needs to authenticate to Immich directly for inline <img> display.
        Returns (content_bytes, content_type).
        """
        r = requests.get(
            f"{self.base_url}/api/assets/{asset_id}/thumbnail",
            headers=self.headers, params={"size": size},
        )
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "image/jpeg")

    def original_stream(self, asset_id):
        """Streaming response object for proxying a full-quality download."""
        r = requests.get(
            f"{self.base_url}/api/assets/{asset_id}/original",
            headers=self.headers, stream=True,
        )
        r.raise_for_status()
        return r
