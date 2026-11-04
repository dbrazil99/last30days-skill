"""Footprint classification and the park / not-a-park decision."""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry

from .config import Settings
from .geo import area_acres, footprint_shape
from .models import Footprint, ParkResult

# Size bands in square meters (1 m2 ~= 10.76 sq ft).
#   Single-wide 10-16 ft x 40-80 ft  ->  ~37-119 m2
#   Double-wide 20-32 ft x 40-80 ft  ->  ~74-238 m2
#   Footprint datasets usually merge a home with its carport / awning / porch, so on real
#   parks (tested in Hemet, CA) most homes measure 120-330 m2 with a near-square outline.
#   Shape alone can't separate a mobile home from a small site-built house; the "one parcel
#   with many of these" test (plus land-use codes and registries) is what makes it a park.
#   Park-model / RV 8-12 ft x 20-40 ft -> ~15-35 m2, long and narrow.
UNIT_MIN, UNIT_MAX = 35.0, 400.0
RV_MIN, RV_MAX = 12.0, 35.0
LARGE_MIN = 400.0


def classify_footprint(geom: BaseGeometry) -> Footprint:
    area, long_m, short_m = footprint_shape(geom)
    aspect = long_m / short_m if short_m else 0.0
    if area >= LARGE_MIN:
        kind = "large_structure"          # clubhouse, office, apartment block, barn
    elif UNIT_MIN <= area < UNIT_MAX and 3.0 <= short_m <= 20.0:
        kind = "mobile_home"
    elif RV_MIN <= area < UNIT_MIN and aspect >= 1.8:
        kind = "rv"
    else:
        kind = "other_small"              # sheds, carports split from the home, slivers
    c = geom.centroid
    return Footprint(lat=c.y, lon=c.x, area_m2=round(area, 1), long_m=round(long_m, 1),
                     short_m=round(short_m, 1), kind=kind)


def decide(result: ParkResult, s: Settings) -> ParkResult:
    """Set status, reasons, counts and a 0-1 confidence on a ParkResult."""
    fps = result.footprints
    result.mh_count = sum(f.kind == "mobile_home" for f in fps)
    result.rv_count = sum(f.kind == "rv" for f in fps)
    residential = result.mh_count + result.rv_count
    total = len(fps)
    acres = area_acres(result.parcel.geom)
    share = residential / total if total else 0.0

    registry = [c.reported_units for c in result.candidates if c.reported_units]
    result.registry_units = max(registry) if registry else None

    sources = {c.source.split(":")[0] for c in result.candidates}
    if result.parcel.landuse_flag:
        sources.add("parcel_landuse")
    votes = len(sources)

    reasons = list(result.reasons)
    if acres <= s.single_lot_max_acres and residential <= 2:
        result.status = "REJECTED"
        reasons.append(f"single lot ({acres:.2f} ac, {residential} unit(s)) - likely MH subdivision/own-lot home")
    elif residential < s.min_units_for_park:
        result.status = "REVIEW" if votes >= 2 else "REJECTED"
        reasons.append(f"only {residential} MH/RV-shaped footprints found")
        if result.parcel.landuse_flag or result.registry_units:
            result.status = "REVIEW"
            reasons.append("but a registry or land-use code says it is a park (RV parks often show no footprints)")
    elif share < s.min_mh_share:
        result.status = "REVIEW"
        reasons.append(f"MH/RV shapes are only {share:.0%} of structures - may be mixed use or site-built homes")
    else:
        result.status = "CONFIRMED"
        reasons.append(f"{result.mh_count} MH + {result.rv_count} RV footprints on one {acres:.1f} ac parcel")

    if result.registry_units and result.status == "CONFIRMED":
        diff = abs(residential - result.registry_units) / result.registry_units
        if diff > s.registry_tolerance:
            result.status = "REVIEW"
            reasons.append(f"count {residential} vs registry {result.registry_units} ({diff:.0%} apart)")
        else:
            reasons.append(f"matches registry ({result.registry_units} spaces)")

    if result.multi_parcel:
        reasons.append("park appears to span multiple parcels (same owner on adjacent parcel)")
        if result.status == "CONFIRMED":
            result.status = "REVIEW"

    density = residential / acres if acres else 0
    conf = 0.25 * min(votes, 3) / 3
    conf += 0.35 * min(share / 0.8, 1.0)
    conf += 0.2 * (1.0 if 3 <= density <= 25 else 0.3)
    if result.registry_units:
        conf += 0.2 * max(0.0, 1 - abs(residential - result.registry_units) / result.registry_units)
    else:
        conf += 0.1
    result.confidence = round(min(conf, 1.0), 2)
    result.reasons = reasons
    return result
