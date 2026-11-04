"""Outputs: parks CSV/GeoJSON, per-unit GeoJSON, HubSpot import CSV, reviewer queue (CSV + HTML)."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from shapely.geometry import mapping

from .geo import area_acres, google_maps_url
from .models import Candidate, ParkResult

PARK_FIELDS = [
    "park_id", "status", "confidence", "name", "park_type", "final_mh", "final_rv", "count_method",
    "footprint_mh", "footprint_rv", "vision_mh", "vision_rv", "registry_units",
    "apn", "owner", "mail_address", "site_address", "city", "state", "zip", "county_fips",
    "acres", "use_desc", "multi_parcel", "sources", "lat", "lon", "google_maps", "reasons",
]

REVIEW_FIELDS = ["park_id", "name", "site_address", "city", "state", "google_maps", "footprint_mh",
                 "footprint_rv", "vision_mh", "registry_units", "reasons",
                 # reviewer fills these in:
                 "reviewer_is_park", "reviewer_mh", "reviewer_rv", "reviewer_notes"]


def park_row(r: ParkResult) -> dict:
    c = r.parcel.geom.centroid
    mh, rv, method = r.final_counts()
    sources = sorted({x.source for x in r.candidates} | ({"parcel_landuse"} if r.parcel.landuse_flag else set()))
    return {
        "park_id": r.park_id, "status": r.status, "confidence": r.confidence, "name": r.name,
        "park_type": r.park_type, "final_mh": mh, "final_rv": rv, "count_method": method,
        "footprint_mh": r.mh_count, "footprint_rv": r.rv_count, "vision_mh": r.vision_mh,
        "vision_rv": r.vision_rv, "registry_units": r.registry_units, "apn": r.parcel.apn,
        "owner": r.parcel.owner, "mail_address": r.parcel.mail_address,
        "site_address": r.parcel.site_address or next((x.address for x in r.candidates if x.address), ""),
        "city": r.parcel.city, "state": r.state, "zip": r.parcel.zip, "county_fips": r.county_fips,
        "acres": round(area_acres(r.parcel.geom), 2), "use_desc": r.parcel.use_desc,
        "multi_parcel": r.multi_parcel, "sources": "|".join(sources),
        "lat": round(c.y, 6), "lon": round(c.x, 6), "google_maps": google_maps_url(c.y, c.x),
        "reasons": "; ".join(r.reasons),
    }


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def hubspot_row(row: dict) -> dict:
    """Generic columns for a HubSpot Properties import; map to your internal property names on import."""
    return {
        "Property Name": row["name"], "Street Address": row["site_address"], "City": row["city"],
        "State": row["state"], "Zip": row["zip"], "County FIPS": row["county_fips"], "APN": row["apn"],
        "Owner of Record": row["owner"], "Owner Mailing Address": row["mail_address"],
        "Property Type": {"mhp": "Mobile Home Park", "rv": "RV Park", "mixed": "MHP / RV"}.get(row["park_type"], "Mobile Home Park"),
        "MH Units": row["final_mh"], "RV Units": row["final_rv"], "Unit Count Method": row["count_method"],
        "Acres": row["acres"], "Latitude": row["lat"], "Longitude": row["lon"],
        "Verification Status": row["status"], "Confidence": row["confidence"],
        "Map Link": row["google_maps"], "MHP Mapper ID": row["park_id"],
    }


def write_county(results: list[ParkResult], unresolved: list[Candidate], out_dir: Path, county: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [park_row(r) for r in results]
    _write_csv(out_dir / "parks.csv", PARK_FIELDS, rows)

    kept = [(r, row) for r, row in zip(results, rows) if r.status != "REJECTED"]
    (out_dir / "parks.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": mapping(r.parcel.geom), "properties": row} for r, row in kept],
    }))

    # One point per counted unit, tagged with its park and coordinates.
    units = []
    for r, _ in kept:
        n = 0
        for f in r.footprints:
            if f.kind not in ("mobile_home", "rv"):
                continue
            n += 1
            units.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(f.lon, 7), round(f.lat, 7)]},
                "properties": {"unit_id": f"{r.park_id}-{n:04d}", "park_id": r.park_id, "kind": f.kind,
                               "lat": round(f.lat, 7), "lon": round(f.lon, 7), "area_m2": f.area_m2},
            })
    (out_dir / "units.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": units}))

    confirmed = [row for r, row in kept if r.status == "CONFIRMED"]
    hs = [hubspot_row(row) for row in confirmed]
    if hs:
        _write_csv(out_dir / "hubspot_import.csv", list(hs[0].keys()), hs)

    review = [row for r, row in kept if r.status == "REVIEW"]
    for c in unresolved:
        review.append({"park_id": f"{county['fips']}-pt-{c.source}-{c.source_id}"[:80], "name": c.name,
                       "site_address": c.address, "state": county["state"],
                       "google_maps": google_maps_url(c.lat, c.lon),
                       "reasons": f"{c.source} point did not land on a parcel"})
    _write_csv(out_dir / "review_queue.csv", REVIEW_FIELDS, review)
    (out_dir / "review.html").write_text(review_html(review, county))
    (out_dir / "DONE").write_text(json.dumps({"parks": len(results), "review": len(review)}))


def review_html(rows: list[dict], county: dict) -> str:
    trs = "\n".join(
        "<tr><td>{id}</td><td>{name}<br><small>{addr}</small></td><td>{fp}</td><td>{vis}</td><td>{reg}</td>"
        "<td><a href='{url}' target='_blank'>Open satellite</a></td><td><small>{why}</small></td></tr>".format(
            id=html.escape(str(r.get("park_id", ""))), name=html.escape(str(r.get("name", "") or "(no name)")),
            addr=html.escape(str(r.get("site_address", "") or "")), fp=r.get("footprint_mh", ""),
            vis=r.get("vision_mh", "") or "", reg=r.get("registry_units", "") or "",
            url=html.escape(str(r.get("google_maps", ""))), why=html.escape(str(r.get("reasons", ""))),
        )
        for r in rows
    )
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review queue {html.escape(county['name'])} {county['state']}</title>
<style>body{{font:14px system-ui,sans-serif;margin:16px;background:#fff;color:#111}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}
th{{background:#f4f4f4}}</style>
<h1>Review queue: {html.escape(county['name'])}, {county['state']} ({len(rows)})</h1>
<p>Open each link, confirm it is ONE parcel with multiple mobile homes/RVs, count units, and enter
results in <code>review_queue.csv</code> (reviewer_is_park = Y/N, reviewer_mh, reviewer_rv).</p>
<table><tr><th>ID</th><th>Name / address</th><th>Footprint MH</th><th>Vision MH</th><th>Registry</th><th>Map</th><th>Why flagged</th></tr>
{trs}</table>"""


def apply_review(county_dir: Path) -> int:
    """Merge a filled-in review_queue.csv back into parks.csv. Returns rows updated."""
    review = {r["park_id"]: r for r in csv.DictReader(open(county_dir / "review_queue.csv"))}
    rows = list(csv.DictReader(open(county_dir / "parks.csv")))
    updated = 0
    for row in rows:
        rv = review.get(row["park_id"])
        if not rv or not rv.get("reviewer_is_park"):
            continue
        is_park = rv["reviewer_is_park"].strip().upper().startswith("Y")
        row["status"] = "CONFIRMED" if is_park else "REJECTED"
        if is_park and rv.get("reviewer_mh"):
            row["final_mh"] = rv["reviewer_mh"]
            row["final_rv"] = rv.get("reviewer_rv") or 0
            row["count_method"] = "human_review"
        row["reasons"] += f"; reviewed: {rv.get('reviewer_notes', '')}"
        updated += 1
    _write_csv(county_dir / "parks.csv", PARK_FIELDS, rows)
    confirmed = [hubspot_row(r) for r in rows if r["status"] == "CONFIRMED"]
    if confirmed:
        _write_csv(county_dir / "hubspot_import.csv", list(confirmed[0].keys()), confirmed)
    return updated


def rollup(out_root: Path) -> Path:
    """Combine every county's parks.csv into one national file."""
    rows = []
    for f in sorted(out_root.glob("*/parks.csv")):
        rows += list(csv.DictReader(open(f)))
    path = out_root / "ALL_parks.csv"
    _write_csv(path, PARK_FIELDS, rows)
    return path
