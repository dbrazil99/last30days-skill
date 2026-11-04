"""Shared record types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry.base import BaseGeometry


@dataclass
class Candidate:
    """A possible park reported by one source (a point on the map)."""

    source: str                 # "osm" | "google_places" | "registry:<name>" | "parcel_landuse"
    source_id: str
    name: str
    lat: float
    lon: float
    park_type: str = "unknown"  # "mhp" | "rv" | "mixed" | "unknown"
    reported_units: int | None = None   # e.g. lot/space count from a state registry
    reported_rv_units: int | None = None
    address: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Parcel:
    parcel_id: str              # Regrid ll_uuid or county APN; must be unique within the county
    apn: str
    geom: BaseGeometry
    owner: str = ""
    mail_address: str = ""
    site_address: str = ""
    city: str = ""
    zip: str = ""
    use_desc: str = ""
    landuse_flag: bool = False  # county land-use code says MHP / RV park


@dataclass
class Footprint:
    lat: float
    lon: float
    area_m2: float
    long_m: float
    short_m: float
    kind: str                   # "mobile_home" | "rv" | "other_small" | "large_structure"


@dataclass
class ParkResult:
    park_id: str
    county_fips: str
    state: str
    parcel: Parcel
    candidates: list[Candidate]
    footprints: list[Footprint]
    status: str = "REVIEW"      # CONFIRMED | REVIEW | REJECTED
    reasons: list[str] = field(default_factory=list)
    mh_count: int = 0
    rv_count: int = 0
    registry_units: int | None = None
    vision_mh: int | None = None
    vision_rv: int | None = None
    reviewer_mh: int | None = None
    reviewer_rv: int | None = None
    confidence: float = 0.0
    multi_parcel: bool = False

    @property
    def name(self) -> str:
        for c in sorted(self.candidates, key=lambda c: c.source.startswith("registry"), reverse=True):
            if c.name:
                return c.name
        return self.parcel.site_address or self.parcel.apn

    @property
    def park_type(self) -> str:
        types = {c.park_type for c in self.candidates if c.park_type != "unknown"}
        # A few RV-shaped footprints are usually sheds or park models; call it mixed only when
        # RVs are a meaningful share of the units.
        if self.mh_count and self.rv_count >= max(3, 0.1 * (self.mh_count + self.rv_count)):
            types.add("mixed")
        if "mixed" in types or {"mhp", "rv"} <= types:
            return "mixed"
        return next(iter(types), "mhp" if self.mh_count else "unknown")

    def final_counts(self) -> tuple[int, int, str]:
        """Best available (mh, rv, method). Human review beats vision beats footprints."""
        if self.reviewer_mh is not None:
            return self.reviewer_mh, self.reviewer_rv or 0, "human_review"
        if self.vision_mh is not None:
            return self.vision_mh, self.vision_rv or 0, "vision_model"
        return self.mh_count, self.rv_count, "footprints"
