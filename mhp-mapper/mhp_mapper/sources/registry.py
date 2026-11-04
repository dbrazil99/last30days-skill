"""State / federal park registries loaded from CSV.

Each registry is a CSV you download once (e.g. California HCD park list, the HIFLD
Mobile Home Parks layer exported to CSV, other state licensing lists). Column names
differ, so you pass a mapping, e.g.

    name=Park Name,address=Park Address,city=City,zip=Zip,units=MH Lots,rv_units=RV Lots

If the file has no lat/lon columns, rows are geocoded with the free US Census geocoder.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import requests

from ..models import Candidate

CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
CENSUS_BATCH = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"


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
        self._save()
        return result

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache))

    def batch(self, rows: list[tuple[str, str, str, str]]) -> None:
        """Geocode (street, city, state, zip) rows via the Census batch API, 1,000 per request."""
        todo = [r for r in rows if _key(*r) not in self.cache]
        for i in range(0, len(todo), 1000):
            chunk = todo[i:i + 1000]
            buf = io.StringIO()
            w = csv.writer(buf)
            for j, (street, city, state, zip_) in enumerate(chunk):
                w.writerow([j, street, city, state, zip_])
            try:
                r = requests.post(CENSUS_BATCH, data={"benchmark": "Public_AR_Current"},
                                  files={"addressFile": ("a.csv", buf.getvalue(), "text/csv")}, timeout=600)
                r.raise_for_status()
            except requests.RequestException as exc:
                print(f"  census batch geocode failed ({exc}); falling back to one-by-one")
                return
            for rec in csv.reader(io.StringIO(r.text)):
                if len(rec) < 3:
                    continue
                key = _key(*chunk[int(rec[0])])
                if rec[2] == "Match" and len(rec) > 5 and "," in rec[5]:
                    lon, lat = rec[5].split(",")
                    self.cache[key] = [float(lat), float(lon)]
                else:
                    self.cache[key] = None
            self._save()


def _norm_type(t: str) -> str:
    t = t.lower()
    has_rv = "rv" in t or "recreational" in t or "camp" in t
    has_mh = "mobile" in t or "mh" in t or "manufactured" in t
    if has_rv and has_mh:
        return "mixed"
    return "rv" if has_rv else ("mhp" if has_mh else "unknown")


def _key(street: str, city: str, state: str, zip_: str) -> str:
    return ", ".join(x for x in [street, city, state, zip_] if x)


def load(path: Path, name: str, mapping: dict[str, str], geocoder: Geocoder | None = None,
         park_type: str = "unknown") -> list[Candidate]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    def getter(row):
        return lambda k: (str(row.get(mapping[k], "") or "") if k in mapping else "").strip()

    if geocoder and "lat" not in mapping:
        geocoder.batch([(getter(r)("address"), getter(r)("city"), getter(r)("state"), getter(r)("zip")) for r in rows])
    for i, row in enumerate(rows):
            get = getter(row)
            lat, lon = get("lat"), get("lon")
            address = _key(get("address"), get("city"), get("state"), get("zip"))
            if lat and lon:
                coords = (float(lat), float(lon))
            elif geocoder and address:
                coords = geocoder(address)
            else:
                coords = None
            if not coords:
                continue
            ptype = _norm_type(get("type")) if "type" in mapping else park_type
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
