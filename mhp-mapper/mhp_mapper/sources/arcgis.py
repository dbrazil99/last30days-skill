"""Free parcel data straight from a county/state ArcGIS REST service.

Most US counties publish parcels on an ArcGIS MapServer/FeatureServer. This backend queries
it live (no download, no account, no cost): land-use filter for park parcels, point lookups
for candidates, and envelope queries for adjacent parcels.

Configure counties in `counties.json` (see README). Example (Riverside County, CA):

  "06065": {
    "url": "https://gis.countyofriverside.us/arcgis_mapping/rest/services/OpenData/Assessor/MapServer/50",
    "park_where": "CLASS_CODE LIKE '%Manufactured Home Park' OR CLASS_CODE = 'Private Campground'",
    "fields": {"id": "APN", "apn": "APN", "mail_address": ["MAIL_STREET", "MAIL_CITY"],
               "site_address": "SITUS_STREET", "city": "SITUS_CITY", "zip": "ZIP_CODE",
               "use_desc": "CLASS_CODE"}
  }
"""

from __future__ import annotations

import json
import time

import requests
from shapely.geometry import shape

from ..models import Parcel
from .parcels import is_park_use

PAGE = 1000


class ArcGISParcels:
    def __init__(self, url: str, fields: dict, park_where: str | None = None, owner_known: bool = False):
        self.url = url.rstrip("/") + "/query"
        self.fields = fields
        self.park_where = park_where
        self.owner_known = owner_known
        self._cache: dict[str, Parcel] = {}

    def _get(self, params: dict) -> dict:
        base = {"outFields": "*", "returnGeometry": "true", "outSR": 4326, "f": "geojson"}
        last = None
        for attempt in range(4):
            try:
                r = requests.post(self.url, data={**base, **params}, timeout=120)
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last = exc
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"ArcGIS query failed: {last}")

    def _field(self, props: dict, key: str) -> str:
        spec = self.fields.get(key)
        if not spec:
            return ""
        if isinstance(spec, list):
            return ", ".join(str(props.get(k) or "").strip() for k in spec if props.get(k))
        return str(props.get(spec) or "").strip()

    def _parcels(self, data: dict) -> list[Parcel]:
        out = []
        for f in data.get("features", []):
            if not f.get("geometry"):
                continue
            props = f.get("properties", {})
            use = self._field(props, "use_desc")
            pid = self._field(props, "id") or str(props.get("OBJECTID", ""))
            if pid in self._cache:
                out.append(self._cache[pid])
                continue
            p = Parcel(
                parcel_id=pid, apn=self._field(props, "apn") or pid, geom=shape(f["geometry"]).buffer(0),
                owner=self._field(props, "owner"), mail_address=self._field(props, "mail_address"),
                site_address=self._field(props, "site_address"), city=self._field(props, "city"),
                zip=self._field(props, "zip"), use_desc=use,
                landuse_flag=is_park_use(use),
            )
            self._cache[pid] = p
            out.append(p)
        return out

    def landuse_parcels(self) -> list[Parcel]:
        if not self.park_where:
            return []
        out, offset = [], 0
        while True:
            data = self._get({"where": self.park_where, "resultOffset": offset, "resultRecordCount": PAGE,
                              "orderByFields": "OBJECTID"})
            batch = self._parcels(data)
            for p in batch:
                p.landuse_flag = True
            out += batch
            if len(data.get("features", [])) < PAGE:
                return out
            offset += PAGE

    def at_point(self, lat: float, lon: float) -> Parcel | None:
        data = self._get({"where": "1=1", "geometry": json.dumps({"x": lon, "y": lat}),
                          "geometryType": "esriGeometryPoint", "inSR": 4326,
                          "spatialRel": "esriSpatialRelIntersects"})
        hits = self._parcels(data)
        return hits[0] if hits else None

    def neighbors(self, parcel: Parcel) -> list[Parcel]:
        g = parcel.geom.buffer(0.00003)
        ring = [list(c) for c in g.exterior.coords] if g.geom_type == "Polygon" else \
               [list(c) for c in g.convex_hull.exterior.coords]
        data = self._get({"where": "1=1", "geometry": json.dumps({"rings": [ring]}),
                          "geometryType": "esriGeometryPolygon", "inSR": 4326,
                          "spatialRel": "esriSpatialRelIntersects", "resultRecordCount": 200})
        return [p for p in self._parcels(data) if p.parcel_id != parcel.parcel_id]
