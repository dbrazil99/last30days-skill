"""Parcel boundaries + county land-use codes.

Two backends, same interface:

* ParcelFile  - a local parcel file. Use this for the national run: Regrid bulk county
                files (GeoPackage / GeoParquet / shapefile) or any county GIS export.
* RegridAPI   - Regrid's REST API. Handy for pilots and spot checks without bulk data.

Interface:
    landuse_parcels()        -> parcels whose land-use description says MHP / RV park
    at_point(lat, lon)       -> parcel containing a point (or None)
    neighbors(parcel)        -> parcels touching this one (multi-parcel park check)
"""

from __future__ import annotations

import re
from pathlib import Path

import requests
from shapely import wkt as shapely_wkt
from shapely.geometry import shape

from ..models import Parcel

# County use descriptions vary wildly; these phrases indicate a *park* (many units on one
# parcel), not a single mobile home on its own lot.
PARK_USE_RE = re.compile(
    r"mobile\s*home\s*(park|court|community|cmty|pk|ct)|mobilehome\s*park|"
    r"trailer\s*(park|court|pk|ct)|\bm\.?h\.?\s*(park|pk|court)\b|\bmhp\b|"
    r"manufactured\s*(home|housing)\s*(park|community|comm)|"
    r"\brv\s*(park|resort|pk)\b|recreational\s*vehicle\s*park|travel\s*trailer\s*park|"
    r"campground|camp\s*ground",
    re.IGNORECASE,
)


def is_park_use(desc: str) -> bool:
    return bool(desc and PARK_USE_RE.search(desc))


# Default field names follow Regrid's standard schema; override for raw county files.
REGRID_FIELDS = {
    "id": "ll_uuid",
    "apn": "parcelnumb",
    "owner": "owner",
    "mail_address": "mailadd",
    "site_address": "address",
    "city": "scity",
    "zip": "szip",
    "use_desc": "usedesc",
}


def _parcel_from_props(props: dict, geom, fields: dict[str, str]) -> Parcel:
    g = lambda k: str(props.get(fields.get(k, ""), "") or "")  # noqa: E731
    use = g("use_desc")
    return Parcel(
        parcel_id=g("id") or g("apn"),
        apn=g("apn"),
        geom=geom,
        owner=g("owner"),
        mail_address=g("mail_address"),
        site_address=g("site_address"),
        city=g("city"),
        zip=g("zip"),
        use_desc=use,
        landuse_flag=is_park_use(use),
    )


class ParcelFile:
    def __init__(self, path: Path, fields: dict[str, str] | None = None):
        import duckdb

        self.fields = {**REGRID_FIELDS, **(fields or {})}
        self.con = duckdb.connect()
        self.con.sql("INSTALL spatial; LOAD spatial;")
        p = str(path)
        if p.endswith(".parquet"):
            src = f"read_parquet('{p}')"
            geom_col = "geometry"
        else:
            src = f"ST_Read('{p}')"
            geom_col = "geom"
        self.con.sql(f"CREATE TABLE parcels AS SELECT * FROM {src}")
        cols = [r[0] for r in self.con.sql("DESCRIBE parcels").fetchall()]
        if geom_col not in cols:
            geom_col = next(c for c in cols if c.lower() in ("geom", "geometry", "wkb_geometry", "shape"))
        if self.con.sql(f"SELECT typeof({geom_col}) FROM parcels LIMIT 1").fetchall()[0][0] == "BLOB":
            self.con.sql(f"ALTER TABLE parcels ADD COLUMN _g GEOMETRY; UPDATE parcels SET _g = ST_GeomFromWKB({geom_col})")
        else:
            self.con.sql(f"ALTER TABLE parcels ADD COLUMN _g GEOMETRY; UPDATE parcels SET _g = {geom_col}")
        self.cols = [c for c in cols if c != geom_col]

    def _rows(self, where: str, params: list | None = None) -> list[Parcel]:
        sel = ", ".join(f'"{c}"' for c in self.cols)
        rows = self.con.execute(f"SELECT {sel}, ST_AsText(_g) FROM parcels WHERE {where}", params or []).fetchall()
        out = []
        for r in rows:
            props = dict(zip(self.cols, r[:-1]))
            out.append(_parcel_from_props(props, shapely_wkt.loads(r[-1]), self.fields))
        return out

    def landuse_parcels(self) -> list[Parcel]:
        col = self.fields["use_desc"]
        if col not in self.cols:
            return []
        rows = self._rows(f'"{col}" IS NOT NULL')
        return [p for p in rows if p.landuse_flag]

    def at_point(self, lat: float, lon: float) -> Parcel | None:
        hits = self._rows("ST_Contains(_g, ST_Point(?, ?))", [lon, lat])
        return hits[0] if hits else None

    def neighbors(self, parcel: Parcel) -> list[Parcel]:
        wkt = parcel.geom.buffer(0.00003).wkt  # ~3 m, bridges slivers between adjacent parcels
        hits = self._rows("ST_Intersects(_g, ST_GeomFromText(?))", [wkt])
        return [p for p in hits if p.parcel_id != parcel.parcel_id]


class RegridAPI:
    """Regrid v2 API. Endpoint shapes are from Regrid's public docs; confirm on the pilot run."""

    BASE = "https://app.regrid.com/api/v2/parcels"

    def __init__(self, token: str, county_path: str):
        self.token = token
        self.path = county_path  # e.g. "/us/ca/riverside"
        self.fields = REGRID_FIELDS

    def _features(self, endpoint: str, params: dict) -> list[Parcel]:
        r = requests.get(f"{self.BASE}/{endpoint}", params={**params, "token": self.token}, timeout=60)
        r.raise_for_status()
        feats = (r.json().get("parcels") or {}).get("features", [])
        return [
            _parcel_from_props(f["properties"].get("fields", f["properties"]), shape(f["geometry"]), self.fields)
            for f in feats
            if f.get("geometry")
        ]

    def landuse_parcels(self) -> list[Parcel]:
        out: dict[str, Parcel] = {}
        for phrase in ["%mobile%home%park%", "%trailer%park%", "%mobile%home%court%",
                       "%manufactured%", "%rv%park%", "%campground%"]:
            try:
                for p in self._features(
                    "query", {"path": self.path, "fields[usedesc][ilike]": phrase, "limit": 1000}
                ):
                    if p.landuse_flag:
                        out[p.parcel_id] = p
            except requests.HTTPError as exc:  # query filters vary by plan; fall back to point lookups
                print(f"  regrid query '{phrase}' failed: {exc}")
        return list(out.values())

    def at_point(self, lat: float, lon: float) -> Parcel | None:
        hits = self._features("point", {"lat": lat, "lon": lon, "limit": 1})
        return hits[0] if hits else None

    def neighbors(self, parcel: Parcel) -> list[Parcel]:
        return []  # one call per neighbor is too expensive; use bulk files for adjacency
