import csv
import json
import math

from shapely.geometry import Polygon, box

from mhp_mapper import classify, export
from mhp_mapper.config import Settings
from mhp_mapper.geo import area_acres, footprint_shape, google_maps_url
from mhp_mapper.models import Candidate, Parcel, ParkResult
from mhp_mapper.sources import osm
from mhp_mapper.sources.parcels import ParcelFile, is_park_use
from mhp_mapper.sources.registry import load, parse_mapping

LAT0 = 33.74
M_LAT = 1 / 110_540
M_LON = 1 / (111_320 * math.cos(math.radians(LAT0)))


def rect(x_m, y_m, w_m, h_m, lon0=-116.98, lat0=LAT0):
    """Axis-aligned rectangle w_m x h_m meters with lower-left at (x_m, y_m) meters from origin."""
    x0, y0 = lon0 + x_m * M_LON, lat0 + y_m * M_LAT
    return box(x0, y0, x0 + w_m * M_LON, y0 + h_m * M_LAT)


def park(n_homes, parcel_w=200, parcel_h=200, home=(12, 20), extra=()):
    parcel = rect(0, 0, parcel_w, parcel_h)
    homes = [rect(10 + (i % 8) * 22, 10 + (i // 8) * 25, *home) for i in range(n_homes)]
    fps = [classify.classify_footprint(g) for g in homes + list(extra)]
    p = Parcel(parcel_id="p1", apn="123-456-789", geom=parcel, owner="Sunny Acres LLC", landuse_flag=True)
    return ParkResult(park_id="06065-p1", county_fips="06065", state="CA", parcel=p, candidates=[], footprints=fps)


def test_shape_and_area():
    g = rect(0, 0, 4.3, 18.3)  # single-wide 14x60 ft
    area, long_m, short_m = footprint_shape(g)
    assert 75 < area < 82 and 18 < long_m < 18.6 and 4.1 < short_m < 4.5
    assert abs(area_acres(rect(0, 0, 63.6, 63.6)) - 1.0) < 0.01


def test_footprint_kinds():
    assert classify.classify_footprint(rect(0, 0, 4.3, 18.3)).kind == "mobile_home"   # single-wide
    assert classify.classify_footprint(rect(0, 0, 14, 18)).kind == "mobile_home"      # double-wide + carport
    assert classify.classify_footprint(rect(0, 0, 2.6, 10)).kind == "rv"              # travel trailer
    assert classify.classify_footprint(rect(0, 0, 30, 20)).kind == "large_structure"  # clubhouse
    assert classify.classify_footprint(rect(0, 0, 3, 3)).kind == "other_small"        # shed


def test_confirmed_park():
    r = classify.decide(park(40), Settings())
    assert r.status == "CONFIRMED" and r.mh_count == 40 and r.confidence > 0.5


def test_single_lot_rejected():
    r = park(1, parcel_w=30, parcel_h=40)
    r.parcel.landuse_flag = False
    assert classify.decide(r, Settings()).status == "REJECTED"


def test_registry_mismatch_goes_to_review():
    r = park(40)
    r.candidates = [Candidate("registry:ca_hcd", "1", "Sunny Acres", LAT0, -116.98, "mhp", reported_units=80)]
    r = classify.decide(r, Settings())
    assert r.status == "REVIEW" and r.registry_units == 80
    r2 = park(78)
    r2.candidates = [Candidate("registry:ca_hcd", "1", "Sunny Acres", LAT0, -116.98, "mhp", reported_units=80)]
    assert classify.decide(r2, Settings()).status == "CONFIRMED"


def test_mostly_large_buildings_is_review():
    big = [rect(5 + i * 35, 150, 30, 20) for i in range(5)]
    r = park(4, extra=big)
    assert classify.decide(r, Settings()).status == "REVIEW"


def test_final_counts_priority():
    r = classify.decide(park(10), Settings())
    assert r.final_counts() == (10, 0, "footprints")
    r.vision_mh, r.vision_rv = 12, 1
    assert r.final_counts() == (12, 1, "vision_model")
    r.reviewer_mh, r.reviewer_rv = 11, 2
    assert r.final_counts() == (11, 2, "human_review")


def test_park_use_regex():
    for yes in ["MOBILE HOME PARK", "Mobile Home Court", "TRAILER PARK", "MH PARK", "RV PARK",
                "Manufactured Housing Community", "CAMPGROUND", "MOBILEHOME PARK"]:
        assert is_park_use(yes), yes
    for no in ["MOBILE HOME", "SINGLE FAMILY RESIDENCE", "MOBILE HOME ON OWN LOT", "VACANT", ""]:
        assert not is_park_use(no), no


def test_osm_parse():
    payload = {"elements": [
        {"type": "way", "id": 1, "center": {"lat": 33.7, "lon": -117.0}, "tags": {"residential": "trailer_park", "name": "A"}},
        {"type": "node", "id": 2, "lat": 33.8, "lon": -117.1, "tags": {"tourism": "caravan_site"}},
        {"type": "way", "id": 3, "tags": {}},
    ]}
    c = osm.parse(payload)
    assert [x.park_type for x in c] == ["mhp", "rv"] and c[0].name == "A"


def test_registry_loader(tmp_path):
    f = tmp_path / "hcd.csv"
    f.write_text("Park Name,Lat,Lon,MH Lots,RV Lots\nSunny Acres,33.74,-116.98,\"1,204\",10\nNo Coords,,,5,0\n")
    rows = load(f, "ca_hcd", parse_mapping("name=Park Name,lat=Lat,lon=Lon,units=MH Lots,rv_units=RV Lots"))
    assert len(rows) == 1 and rows[0].reported_units == 1204 and rows[0].reported_rv_units == 10


def test_parcel_file_and_exports(tmp_path):
    feats = []
    for i, (x, use) in enumerate([(0, "MOBILE HOME PARK"), (300, "SINGLE FAMILY")]):
        g = rect(x, 0, 200, 200)
        feats.append({"type": "Feature", "geometry": json.loads(json.dumps(g.__geo_interface__)),
                      "properties": {"ll_uuid": f"u{i}", "parcelnumb": f"APN{i}", "owner": "X", "usedesc": use,
                                     "address": f"{i} Main St"}})
    src = tmp_path / "06065.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    pf = ParcelFile(src)
    lu = pf.landuse_parcels()
    assert [p.apn for p in lu] == ["APN0"]
    c = rect(100, 100, 1, 1).centroid
    assert pf.at_point(c.y, c.x).apn == "APN0"
    assert [n.apn for n in pf.neighbors(lu[0])] == []  # 100 m gap, not adjacent

    r = park(30)
    r.parcel = lu[0]
    classify.decide(r, Settings())
    county = {"fips": "06065", "name": "Riverside", "state": "CA"}
    out = tmp_path / "out"
    export.write_county([r], [Candidate("osm", "node/9", "Lost Park", 33.9, -117.2)], out, county)
    rows = list(csv.DictReader(open(out / "parks.csv")))
    assert rows[0]["status"] == "CONFIRMED" and rows[0]["final_mh"] == "30"
    units = json.loads((out / "units.geojson").read_text())["features"]
    assert len(units) == 30 and units[0]["properties"]["unit_id"].startswith("06065-p1-")
    assert (out / "hubspot_import.csv").exists()
    review = list(csv.DictReader(open(out / "review_queue.csv")))
    assert review[0]["name"] == "Lost Park"

    # Reviewer corrects the count; merge it back.
    review_rows = [{**review[0], "park_id": "06065-p1", "reviewer_is_park": "Y", "reviewer_mh": "31", "reviewer_rv": "0"}]
    with open(out / "review_queue.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=export.REVIEW_FIELDS)
        w.writeheader()
        w.writerows(review_rows)
    assert export.apply_review(out) == 1
    rows = list(csv.DictReader(open(out / "parks.csv")))
    assert rows[0]["final_mh"] == "31" and rows[0]["count_method"] == "human_review"


def test_maps_url():
    assert "basemap=satellite" in google_maps_url(33.7, -117.0)
