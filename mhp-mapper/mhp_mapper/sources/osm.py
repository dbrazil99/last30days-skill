"""OpenStreetMap candidates via the Overpass API (free, no account)."""

from __future__ import annotations

import time

import requests

from ..models import Candidate

QUERY = """
[out:json][timeout:300];
(
  nwr["residential"="trailer_park"]({s},{w},{n},{e});
  nwr["landuse"="trailer_park"]({s},{w},{n},{e});
  nwr["place"="trailer_park"]({s},{w},{n},{e});
  nwr["tourism"="caravan_site"]({s},{w},{n},{e});
);
out center tags;
"""


def _park_type(tags: dict) -> str:
    if tags.get("tourism") == "caravan_site":
        return "rv"
    return "mhp"


def parse(payload: dict) -> list[Candidate]:
    out = []
    for el in payload.get("elements", []):
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        tags = el.get("tags", {})
        addr = " ".join(
            filter(None, [tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:city")])
        )
        out.append(
            Candidate(
                source="osm",
                source_id=f"{el['type']}/{el['id']}",
                name=tags.get("name", ""),
                lat=float(lat),
                lon=float(lon),
                park_type=_park_type(tags),
                address=addr,
                extra={"tags": tags},
            )
        )
    return out


def fetch(bbox: tuple[float, float, float, float], urls: tuple[str, ...]) -> list[Candidate]:
    w, s, e, n = bbox
    q = QUERY.format(s=s, w=w, n=n, e=e)
    last_err: Exception | None = None
    for attempt in range(2):
        for url in urls:
            try:
                r = requests.post(url, data={"data": q}, headers={"User-Agent": "mhp-mapper/0.1"}, timeout=150)
                if r.status_code == 200:
                    return parse(r.json())
                last_err = RuntimeError(f"{url} -> HTTP {r.status_code}")
            except (requests.RequestException, ValueError) as exc:  # ValueError: truncated/non-JSON body
                last_err = exc
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Overpass failed: {last_err}")
