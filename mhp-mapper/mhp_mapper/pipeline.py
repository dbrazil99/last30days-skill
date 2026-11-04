"""County-level pipeline: discover -> resolve to parcels -> count -> decide -> (vision) -> export."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from shapely.geometry import Point

from . import budget, classify, export
from .buildings import BuildingStore
from .config import Settings
from .models import Candidate, ParkResult
from .sources import osm, places
from .sources.arcgis import ArcGISParcels
from .sources.parcels import ParcelFile, RegridAPI


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _norm_owner(o: str) -> str:
    o = re.sub(r"[^a-z0-9 ]", " ", o.lower())
    o = re.sub(r"\b(llc|inc|lp|ltd|co|corp|trust|the|tr)\b", " ", o)
    return " ".join(o.split())


def parcel_source_for(county: dict, s: Settings, parcels_dir: Path | None):
    cfg_path = Path(__file__).resolve().parent.parent / "counties.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text()).get(county["fips"])
        if cfg:
            return ArcGISParcels(cfg["url"], cfg["fields"], cfg.get("park_where"))
    if parcels_dir:
        st = county["state"].lower()
        keys = [county["fips"], f"{st}_{_slug(county['name']).replace('-', '_')}", f"{st}_{_slug(county['name'])}"]
        for f in sorted(parcels_dir.iterdir()):
            stem = f.name.split(".")[0].lower()
            if stem in keys and f.suffix.lower() in (".gpkg", ".parquet", ".shp", ".geojson", ".json", ".fgb"):
                return ParcelFile(f)
    if s.regrid_token:
        return RegridAPI(s.regrid_token, f"/us/{county['state'].lower()}/{_slug(county['name'])}")
    return None


def gather_candidates(county: dict, s: Settings, registry: list[Candidate], use_places: bool,
                      log=print) -> list[Candidate]:
    geom = county["geom"]
    bbox = geom.bounds
    found: list[Candidate] = []
    try:
        osm_c = osm.fetch(bbox, s.overpass_urls)
        found += osm_c
        log(f"  osm: {len(osm_c)}")
    except RuntimeError as exc:
        log(f"  osm: skipped ({exc})")
    if use_places and s.google_key:
        pl = places.fetch(s.google_key, bbox)
        found += pl
        log(f"  google places: {len(pl)}")
    reg = [c for c in registry if geom.contains(Point(c.lon, c.lat))]
    found += reg
    log(f"  registries: {len(reg)}")
    return [c for c in found if geom.contains(Point(c.lon, c.lat))]


def resolve_parcel(psrc, c: Candidate):
    """Parcel for a candidate point. Address geocodes often land in the street or on the house
    next door, so if the hit isn't a park-coded or sizable parcel, look within ~150 m for one."""
    from .geo import area_acres

    p = psrc.at_point(c.lat, c.lon)
    if p is not None and (p.landuse_flag or area_acres(p.geom) >= 2):
        return p
    near = getattr(psrc, "near", None)
    if near is None:
        return p
    pt = Point(c.lon, c.lat)
    pool = near(c.lat, c.lon, 150)
    coded = [q for q in pool if q.landuse_flag]
    if coded:
        return min(coded, key=lambda q: q.geom.distance(pt))
    big = [q for q in pool if area_acres(q.geom) >= 2 and q.geom.distance(pt) < 0.0006]  # ~60 m
    if big:
        return min(big, key=lambda q: q.geom.distance(pt))
    return p


def run_county(county: dict, s: Settings, registry: list[Candidate], parcels_dir: Path | None = None,
               use_places: bool = True, vision_limit: int = 0, log=print) -> list[ParkResult]:
    fips = county["fips"]
    out_dir = s.out_dir / fips
    log(f"[{fips}] {county['name']}, {county['state']}")

    psrc = parcel_source_for(county, s, parcels_dir)
    if psrc is None:
        raise RuntimeError(f"No parcel data for {fips}: add a file to --parcels-dir or set REGRID_API_TOKEN")

    candidates = gather_candidates(county, s, registry, use_places, log)

    # Resolve every candidate point to the parcel it sits on.
    by_parcel: dict[str, list[Candidate]] = defaultdict(list)
    parcels = {}
    unresolved: list[Candidate] = []
    for c in candidates:
        p = resolve_parcel(psrc, c)
        if p is None:
            unresolved.append(c)
            continue
        parcels[p.parcel_id] = p
        by_parcel[p.parcel_id].append(c)

    # Add parcels the county itself codes as MHP / RV park, even if no other source knows them.
    lu = psrc.landuse_parcels()
    for p in lu:
        parcels.setdefault(p.parcel_id, p)
        by_parcel.setdefault(p.parcel_id, [])
    log(f"  land-use coded parcels: {len(lu)} | unique parcels to check: {len(parcels)} | unresolved points: {len(unresolved)}")

    store = BuildingStore(s.overture_release, s.data_dir)
    store.prefetch([p.geom for p in parcels.values()])

    results: list[ParkResult] = []
    for pid, parcel in parcels.items():
        fps = [classify.classify_footprint(g) for g in store.within(parcel.geom)]
        r = ParkResult(park_id=f"{fips}-{pid}", county_fips=fips, state=county["state"],
                       parcel=parcel, candidates=by_parcel[pid], footprints=fps)
        # Multi-parcel check: adjacent parcel, same owner, also full of MH-shaped footprints.
        owner_key = lambda p: _norm_owner(p.owner) or _norm_owner(p.mail_address)  # noqa: E731
        owner = owner_key(parcel)
        if owner:
            for nb in psrc.neighbors(parcel):
                if owner_key(nb) == owner:
                    nb_mh = sum(classify.classify_footprint(g).kind in ("mobile_home", "rv")
                                for g in store.within(nb.geom))
                    if nb_mh >= s.min_units_for_park or nb.landuse_flag:
                        r.multi_parcel = True
                        r.reasons.append(f"adjacent parcel {nb.apn}")
        classify.decide(r, s)
        results.append(r)

    if vision_limit:
        _apply_vision(results, s, out_dir, vision_limit, log)

    export.write_county(results, unresolved, out_dir, county)
    counts = defaultdict(int)
    for r in results:
        counts[r.status] += 1
    log(f"  done: {dict(counts)} -> {out_dir}")
    return results


def _apply_vision(results: list[ParkResult], s: Settings, out_dir: Path, limit: int, log) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from . import vision

    # Parks where a registry or land-use code says "park" but footprints are missing go first:
    # that is where the free building data is known to have gaps.
    def priority(r: ParkResult):
        gap = (r.registry_units or r.parcel.landuse_flag) and r.mh_count + r.rv_count < s.min_units_for_park
        return (0 if gap else 1, -r.confidence)

    queue = sorted((r for r in results if r.status == "REVIEW"), key=priority)[:limit]
    log(f"  vision: {len(queue)} parks")

    def one(r: ParkResult):
        try:
            return r, vision.count_with_claude(r, s.claude_model, out_dir / "chips"), None
        except budget.BudgetExceeded as exc:
            return r, None, exc
        except Exception as exc:  # network/API errors shouldn't kill the county
            log(f"  vision failed for {r.park_id}: {exc}")
            return r, None, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(one, queue))
    stop = next((e for _, _, e in outcomes if e), None)
    for r, v, _ in outcomes:
        if not v:
            continue
        r.vision_mh, r.vision_rv, r.vision_vacant = v["mobile_home_count"], v["rv_count"], v["vacant_pads"]
        r.reasons.append(f"vision ({v['confidence']}): {r.vision_mh} MH, {r.vision_rv} RV, "
                         f"{r.vision_vacant} vacant pads - {v['notes']}")
        if v["confidence"] in ("high", "medium"):
            if not v["is_mobile_home_or_rv_park"]:
                r.status = "REJECTED" if v["confidence"] == "high" else r.status
            else:
                spaces = r.vision_mh + r.vision_rv + r.vision_vacant
                if not r.multi_parcel and (
                    not r.registry_units
                    or abs(spaces - r.registry_units) / r.registry_units <= s.registry_tolerance
                ):
                    r.status = "CONFIRMED"
    if stop:
        raise stop
