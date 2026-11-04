"""Geometry helpers and Census county boundaries."""

from __future__ import annotations

import math
import zipfile
from functools import lru_cache
from pathlib import Path

import requests
from shapely import wkt as shapely_wkt
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
import numpy as np
import shapely

M2_PER_ACRE = 4046.8564224
COUNTY_ZIP_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip"


def to_local_meters(geom: BaseGeometry, lat0: float | None = None) -> BaseGeometry:
    """Project lon/lat to a local equirectangular plane in meters (accurate at parcel scale)."""
    if lat0 is None:
        lat0 = geom.centroid.y
    kx = 111_320.0 * math.cos(math.radians(lat0))
    ky = 110_540.0
    return shapely.transform(geom, lambda xy: xy * np.array([kx, ky]))


def area_m2(geom: BaseGeometry) -> float:
    return to_local_meters(geom).area


def area_acres(geom: BaseGeometry) -> float:
    return area_m2(geom) / M2_PER_ACRE


def footprint_shape(geom: BaseGeometry) -> tuple[float, float, float]:
    """Return (area_m2, long_side_m, short_side_m) using the minimum rotated rectangle."""
    g = to_local_meters(geom)
    rect = g.minimum_rotated_rectangle
    coords = list(rect.exterior.coords) if isinstance(rect, Polygon) else []
    if len(coords) < 4:
        return g.area, 0.0, 0.0
    sides = [math.dist(coords[i], coords[i + 1]) for i in range(2)]
    return g.area, max(sides), min(sides)


def google_maps_url(lat: float, lon: float, zoom: int = 19) -> str:
    """Satellite view link for a human reviewer (viewing only; nothing is extracted from Google)."""
    return (
        "https://www.google.com/maps/@?api=1&map_action=map"
        f"&center={lat:.6f},{lon:.6f}&zoom={zoom}&basemap=satellite"
    )


def bbox_of(geom: BaseGeometry, pad_m: float = 0.0) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = geom.bounds
    if pad_m:
        dlat = pad_m / 110_540.0
        dlon = pad_m / (111_320.0 * math.cos(math.radians((miny + maxy) / 2)))
        minx, miny, maxx, maxy = minx - dlon, miny - dlat, maxx + dlon, maxy + dlat
    return minx, miny, maxx, maxy


# ---------------------------------------------------------------- Census counties


def _county_zip(data_dir: Path) -> Path:
    path = data_dir / "census" / "cb_2023_us_county_500k.zip"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(COUNTY_ZIP_URL, timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


@lru_cache(maxsize=4)
def load_counties(data_dir: str) -> dict[str, dict]:
    """Return {GEOID: {fips, name, state, geom}} for every US county."""
    import duckdb

    zpath = _county_zip(Path(data_dir))
    with zipfile.ZipFile(zpath) as zf:
        shp = next(n for n in zf.namelist() if n.endswith(".shp"))
    con = duckdb.connect()
    con.sql("INSTALL spatial; LOAD spatial;")
    rows = con.sql(
        f"SELECT GEOID, NAME, STUSPS, ST_AsText(geom) FROM ST_Read('/vsizip/{zpath}/{shp}')"
    ).fetchall()
    return {
        r[0]: {"fips": r[0], "name": r[1], "state": r[2], "geom": shapely_wkt.loads(r[3])}
        for r in rows
    }


def counties_for(data_dir: Path, fips: list[str] | None = None, states: list[str] | None = None) -> list[dict]:
    allc = load_counties(str(data_dir))
    out = []
    for c in allc.values():
        if fips and c["fips"] not in fips:
            continue
        if states and c["state"] not in states:
            continue
        out.append(c)
    return sorted(out, key=lambda c: c["fips"])
