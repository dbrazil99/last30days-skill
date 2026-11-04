"""Command line entry point.

    python -m mhp_mapper check
    python -m mhp_mapper run --fips 06065 [--parcels-dir data/parcels] [--registry ...] [--vision 50]
    python -m mhp_mapper run --states CA,AZ --workers 6
    python -m mhp_mapper apply-review out/06065
    python -m mhp_mapper rollup
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import requests

from . import budget, export, geo
from .config import get_settings
from .sources import registry as registry_mod


def _load_registries(specs: list[str], s) -> list:
    """--registry NAME:PATH:MAPPING   e.g. ca_hcd:data/ca_hcd.csv:name=Park Name,address=Address,..."""
    out = []
    geocoder = registry_mod.Geocoder(s.data_dir / "geocode_cache.json")
    for spec in specs:
        name, path, mapping = spec.split(":", 2)
        rows = registry_mod.load(Path(path), name, registry_mod.parse_mapping(mapping), geocoder)
        print(f"registry {name}: {len(rows)} parks loaded")
        out += rows
    return out


def _run_one(county: dict, registry: list, parcels_dir: str | None, use_places: bool, vision: int) -> tuple[str, str]:
    from .pipeline import run_county

    s = get_settings()
    try:
        run_county(county, s, registry, Path(parcels_dir) if parcels_dir else None, use_places, vision)
        return county["fips"], "ok"
    except budget.BudgetExceeded as exc:
        return county["fips"], f"BUDGET CAP: {exc}"
    except Exception as exc:
        (s.out_dir / county["fips"]).mkdir(parents=True, exist_ok=True)
        (s.out_dir / county["fips"] / "ERROR.txt").write_text(traceback.format_exc())
        return county["fips"], f"error: {exc}"


def cmd_run(a) -> int:
    s = get_settings()
    fips = [f.strip() for f in a.fips.split(",")] if a.fips else None
    states = [x.strip().upper() for x in a.states.split(",")] if a.states else None
    if not fips and not states:
        print("Pass --fips or --states")
        return 2
    counties = geo.counties_for(s.data_dir, fips, states)
    if not a.force:
        counties = [c for c in counties if not (s.out_dir / c["fips"] / "DONE").exists()]
    registry = _load_registries(a.registry or [], s)
    print(f"{len(counties)} counties to run with {a.workers} worker(s)")
    if a.workers <= 1:
        results = []
        for c in counties:
            results.append(_run_one(c, registry, a.parcels_dir, not a.no_places, a.vision))
            if results[-1][1].startswith("BUDGET CAP"):
                print(f"STOPPED: {results[-1][1]}")
                break
    else:
        results = []
        with ProcessPoolExecutor(max_workers=a.workers) as pool:
            futs = [pool.submit(_run_one, c, registry, a.parcels_dir, not a.no_places, a.vision) for c in counties]
            for f in as_completed(futs):
                results.append(f.result())
                print(f"  {results[-1][0]}: {results[-1][1]}  ({len(results)}/{len(counties)})")
                if results[-1][1].startswith("BUDGET CAP"):
                    print(f"STOPPED: {results[-1][1]}")
                    for other in futs:
                        other.cancel()
                    break
    failed = [r for r in results if r[1] != "ok"]
    print(f"finished: {len(results) - len(failed)} ok, {len(failed)} failed")
    for f in failed:
        print(f"  {f[0]} {f[1]}")
    return 1 if failed else 0


def cmd_check(a) -> int:
    """Verify credentials and data-source reachability before a big run."""
    s = get_settings()
    ok = True

    def line(name, good, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"[{'OK ' if good else 'XX '}] {name} {detail}")

    try:
        n = len(geo.load_counties(str(s.data_dir)))
        line("Census county boundaries", n > 3000, f"({n} counties)")
    except Exception as exc:
        line("Census county boundaries", False, str(exc))
    try:
        from .buildings import BuildingStore
        from shapely.geometry import box

        b = BuildingStore(s.overture_release, s.data_dir)
        n = len(b.within(box(-116.99, 33.735, -116.98, 33.745)))
        line("Overture buildings", n > 0, f"(release {s.overture_release}, {n} test footprints)")
    except Exception as exc:
        line("Overture buildings", False, f"{exc} - check MHP_OVERTURE_RELEASE")
    for url in s.overpass_urls:
        try:
            r = requests.get(url.replace("/interpreter", "/status"), timeout=20)
            line(f"Overpass {url}", r.status_code == 200)
        except requests.RequestException as exc:
            line(f"Overpass {url}", False, str(exc)[:80])
    try:
        r = requests.get("https://planetarycomputer.microsoft.com/api/stac/v1/collections/naip", timeout=20)
        line("NAIP imagery (Planetary Computer)", r.status_code == 200)
    except requests.RequestException as exc:
        line("NAIP imagery", False, str(exc)[:80])
    if s.regrid_token:
        try:
            r = requests.get("https://app.regrid.com/api/v2/parcels/point",
                             params={"lat": 33.74, "lon": -116.985, "limit": 1, "token": s.regrid_token}, timeout=30)
            line("Regrid API", r.status_code == 200, f"HTTP {r.status_code}")
        except requests.RequestException as exc:
            line("Regrid API", False, str(exc)[:80])
    else:
        print("[ -- ] Regrid API: REGRID_API_TOKEN not set (fine if you use bulk files in --parcels-dir)")
    if s.google_key:
        try:
            r = requests.post("https://places.googleapis.com/v1/places:searchText",
                              headers={"X-Goog-Api-Key": s.google_key, "X-Goog-FieldMask": "places.id"},
                              json={"textQuery": "mobile home park Hemet CA", "pageSize": 1}, timeout=30)
            line("Google Places API (New)", r.status_code == 200, f"HTTP {r.status_code}")
        except requests.RequestException as exc:
            line("Google Places API", False, str(exc)[:80])
    else:
        print("[ -- ] Google Places: GOOGLE_MAPS_API_KEY not set (run with --no-places)")
    print(f"[ {'OK' if s.anthropic_key else '--'} ] Anthropic key {'set' if s.anthropic_key else 'not set (vision counts disabled)'}"
          f" | model {s.claude_model}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mhp_mapper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="verify accounts and data sources")

    r = sub.add_parser("run", help="run counties")
    r.add_argument("--fips", help="comma-separated 5-digit county FIPS, e.g. 06065")
    r.add_argument("--states", help="comma-separated state codes, e.g. CA,AZ (every county)")
    r.add_argument("--parcels-dir", help="folder of parcel files named <fips>.* or <st>_<county>.*")
    r.add_argument("--registry", action="append", help="NAME:PATH:MAPPING (repeatable)")
    r.add_argument("--vision", type=int, default=0, help="max REVIEW parks per county to send to Claude vision")
    r.add_argument("--no-places", action="store_true", help="skip Google Places")
    r.add_argument("--workers", type=int, default=1, help="parallel counties")
    r.add_argument("--force", action="store_true", help="rerun counties already DONE")

    ar = sub.add_parser("apply-review", help="merge a filled-in review_queue.csv")
    ar.add_argument("county_dir")

    sub.add_parser("rollup", help="combine all counties into out/ALL_parks.csv")
    sub.add_parser("budget", help="show this month's spend vs. hard caps")
    sc = sub.add_parser("score", help="accuracy vs. registry site counts for a county")
    sc.add_argument("county_dir")

    a = p.parse_args(argv)
    if a.cmd == "check":
        return cmd_check(a)
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "apply-review":
        print(f"updated {export.apply_review(Path(a.county_dir))} rows")
        return 0
    if a.cmd == "score":
        print(json.dumps(export.score(Path(a.county_dir)), indent=2))
        return 0
    if a.cmd == "budget":
        print(json.dumps(budget.report(), indent=2))
        return 0
    if a.cmd == "rollup":
        path = export.rollup(get_settings().out_dir)
        print(json.dumps({"written": str(path)}))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
