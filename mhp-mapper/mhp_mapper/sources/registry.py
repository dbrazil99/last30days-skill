"""State / federal park registries loaded from CSV.

Each registry is a CSV you download once (e.g. California HCD park list, the HIFLD
Mobile Home Parks layer exported to CSV, other state licensing lists). Column names
differ, so you pass a mapping, e.g.

    name=Park Name,address=Park Address,city=City,zip=Zip,units=MH Lots,rv_units=RV Lots

If the file has no lat/lon columns, rows are geocoded with the free US Census geocoder.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import requests

from ..models import Candidate

CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def parse_mapping(spec: str) -> dict[str, str]:
    out = {}
    for part in spec.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _int(v) -> int | None:
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


class Geocoder:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.cache: dict[str, list[float] | None] = (
            json.loads(cache_path.read_text()) if cache_path.exists() else {}
        )

    def __call__(self, address: str) -> tuple[float, float] | None:
        if address in self.cache:
            hit = self.cache[address]
            return (hit[0], hit[1]) if hit else None
        result = None
        try:
            r = requests.get(
                CENSUS_GEOCODER,
                params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
                timeout=30,
            )
            matches = r.json().get("result", {}).get("addressMatches", [])
            if matches:
                c = matches[0]["coordinates"]
                result = (float(c["y"]), float(c["x"]))
        except (requests.RequestException, ValueError, KeyError):
            result = None
        self.cache[address] = list(result) if result else None
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache))
        return result


def load(path: Path, name: str, mapping: dict[str, str], geocoder: Geocoder | None = None,
         park_type: str = "unknown") -> list[Candidate]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            get = lambda k: (row.get(mapping[k], "") if k in mapping else "").strip()  # noqa: E731
            lat, lon = get("lat"), get("lon")
            address = ", ".join(filter(None, [get("address"), get("city"), get("state"), get("zip")]))
            if lat and lon:
                coords = (float(lat), float(lon))
            elif geocoder and address:
                coords = geocoder(address)
            else:
                coords = None
            if not coords:
                continue
            ptype = get("type").lower() if "type" in mapping else park_type
            out.append(
                Candidate(
                    source=f"registry:{name}",
                    source_id=get("id") or f"{name}-{i}",
                    name=get("name"),
                    lat=coords[0],
                    lon=coords[1],
                    park_type=ptype or "unknown",
                    reported_units=_int(get("units")),
                    reported_rv_units=_int(get("rv_units")),
                    address=address,
                    extra={"county": get("county")},
                )
            )
    return out
