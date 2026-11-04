"""Google Places API (New) Text Search: discovery and "is it still operating" checks.

Google's terms allow storing the place_id indefinitely; other Places content (name, address,
coordinates) should be refreshed rather than kept as a permanent database. We store the
place_id plus the parcel-level facts we derive from public data, and link reviewers to
Google Maps for visual confirmation. We never download or analyze Google imagery.
"""

from __future__ import annotations

import time

import requests

from ..models import Candidate

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.types",
        "places.businessStatus",
        "places.googleMapsUri",
        "nextPageToken",
    ]
)
QUERIES = {
    "mhp": ["mobile home park", "manufactured home community", "trailer park"],
    "rv": ["RV park"],
}
MAX_PER_QUERY = 60  # API returns at most 3 pages x 20


def _search(key: str, text: str, rect: tuple[float, float, float, float]) -> list[dict]:
    w, s, e, n = rect
    body = {
        "textQuery": text,
        "pageSize": 20,
        "locationRestriction": {
            "rectangle": {"low": {"latitude": s, "longitude": w}, "high": {"latitude": n, "longitude": e}}
        },
    }
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS}
    results: list[dict] = []
    while True:
        r = requests.post(URL, json=body, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("places", []))
        token = data.get("nextPageToken")
        if not token or len(results) >= MAX_PER_QUERY:
            return results
        body["pageToken"] = token
        time.sleep(1.5)


def _split(rect: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    w, s, e, n = rect
    mx, my = (w + e) / 2, (s + n) / 2
    return [(w, s, mx, my), (mx, s, e, my), (w, my, mx, n), (mx, my, e, n)]


def fetch(key: str, bbox: tuple[float, float, float, float], max_depth: int = 5) -> list[Candidate]:
    """Quadtree search: if a rectangle hits the 60-result cap, split it and search again."""
    seen: dict[str, Candidate] = {}
    for park_type, texts in QUERIES.items():
        for text in texts:
            stack = [(bbox, 0)]
            while stack:
                rect, depth = stack.pop()
                places = _search(key, text, rect)
                if len(places) >= MAX_PER_QUERY and depth < max_depth:
                    stack.extend((r, depth + 1) for r in _split(rect))
                    continue
                for p in places:
                    if p.get("businessStatus") == "CLOSED_PERMANENTLY":
                        continue
                    pid = p["id"]
                    if pid in seen:
                        continue
                    loc = p.get("location", {})
                    seen[pid] = Candidate(
                        source="google_places",
                        source_id=pid,
                        name=(p.get("displayName") or {}).get("text", ""),
                        lat=loc.get("latitude"),
                        lon=loc.get("longitude"),
                        park_type=park_type,
                        address=p.get("formattedAddress", ""),
                        extra={"types": p.get("types", []), "maps_uri": p.get("googleMapsUri", "")},
                    )
    return [c for c in seen.values() if c.lat is not None]
