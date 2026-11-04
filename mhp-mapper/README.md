# MHP Mapper

This tool finds every mobile home park and RV park in a county. For each one it:

1. Confirms the park is **one parcel with many mobile homes and/or RVs on it**.
2. Counts the units.
3. Records the location of each unit.

It then sends the uncertain parks to a person to check on Google Maps.

It runs one county at a time, in parallel. Each county can be re-run on its own, so a national run is just a list of 3,143 counties.

---

## 1. Accounts you need

| # | Account | Why | Required? | Setup |
|---|---|---|---|---|
| 1 | **Regrid** (regrid.com) | Parcel boundaries, APN, owner of record, mailing address, and county land-use code for every US parcel. This is the backbone. | **Yes** | Ask sales for the **nationwide parcel data license** (bulk GeoPackage/GeoParquet by county) **plus an API token**. A token alone is enough for the pilot. Put the token in `.env` as `REGRID_API_TOKEN`. Bulk files go in `data/parcels/`. |
| 2 | **Google Cloud** (console.cloud.google.com) | Places API (New): finds parks by name and drops ones Google lists as permanently closed. Reviewers also use the Google Maps links. | Strongly recommended | Create a project, attach billing, and enable **Places API (New)**. Create an API key restricted to that API. Set a daily quota cap. Put the key in `.env` as `GOOGLE_MAPS_API_KEY`. |
| 3 | **Anthropic Console** (console.anthropic.com) | Claude vision gives a second-opinion count on public-domain aerial photos for the uncertain parks. | Recommended | Create an API key and set a monthly spend limit. Put it in `.env` as `ANTHROPIC_API_KEY`. |
| 4 | **California HCD park list** | Official space counts, used to grade our accuracy in the pilot. | Yes for the CA pilot | Download the HCD mobilehome / special-occupancy park listing as CSV. No account needed. |
| 5 | Other state registries / HIFLD Mobile Home Parks (archived) | More candidates and reported unit counts. | Nice to have | Download as CSV and load with `--registry`. |
| 6 | Nearmap or EagleView | Sharper and newer imagery than NAIP. Needed if you want **reliable RV counts**. | Optional | Get a quote after the pilot, if RV counts matter. |
| 7 | A cloud VM | The national run: 8–16 parallel workers. | For the national run | Any provider. About 16 vCPU, 64 GB RAM, 500 GB disk. A laptop is fine for pilots. |

**No account needed:**
- Overture building footprints (public S3)
- USDA NAIP aerial imagery (Microsoft Planetary Computer)
- OpenStreetMap (Overpass)
- Census county boundaries
- Census geocoder

**Get pricing quotes from Regrid, Google and Nearmap directly.** Regrid's nationwide license is the biggest cost and usually takes the longest to sign, so start that first.

---

## 2. How it decides "this is a park" and counts units

For each county:

1. **Collect candidate points** from every source:
   - OpenStreetMap: parks tagged `trailer_park` and `caravan_site`
   - Google Places: "mobile home park", "manufactured home community", "trailer park" and "RV park" searches. A search area that hits the 60-result cap gets split into quarters and searched again.
   - State registries
2. **Resolve each point to the parcel it sits on.** Then add every parcel whose **county land-use code** says mobile home park, trailer park, RV park or campground. A plain "MOBILE HOME" code (one home on its own lot) does not count.
3. **Pull building footprints** inside each parcel from Overture and classify each one by size and shape:
   - `mobile_home`: 35–400 m². Footprint data merges a home with its carport, so real double-wides measure 120–330 m².
   - `rv`: 12–35 m², long and narrow.
   - `large_structure`: clubhouse, office, apartment block.
   - `other_small`: sheds and slivers.
4. **Decide** for each parcel:
   - **CONFIRMED**: at least 4 home/RV units, at least half of all structures are homes/RVs, and the count is within ±15% of the registry figure (when a registry has one).
   - **REJECTED**: a small lot with 1–2 homes. That's a mobile home on its own lot or in a subdivision, not a park.
   - **REVIEW**: everything else. That includes possible multi-parcel parks (same owner on an adjacent parcel full of homes), registry mismatches, and RV parks with no footprints.
5. **Vision second opinion** (optional, `--vision N`): sends the REVIEW parks to Claude with the NAIP aerial photo and the parcel outlined in red. A high-confidence answer can confirm or reject the park.
6. **Human review**: `review.html` lists every REVIEW park with a **Google Maps satellite link**. The reviewer opens it, confirms, counts, and types the result into `review_queue.csv`. Then `apply-review` merges it back in.

**Final count priority:** human review > vision > footprint count. Every row records which method produced its count.

**Why Google Maps is for people only:** Google's terms prohibit pulling data out of its satellite imagery, so the automated count uses public-domain NAIP imagery. Google Maps is for a reviewer to look at, and Places is for discovery.

### Outputs (`out/<county FIPS>/`)

| File | What it is |
|---|---|
| `parks.csv` | One row per parcel checked: status, confidence, name, MH/RV counts and how they were counted, APN, owner, mailing address, acres, sources, lat/lon, Google Maps link, and the reasons for the decision. |
| `parks.geojson` | Parcel outlines of CONFIRMED and REVIEW parks (opens in QGIS, Google My Maps or kepler.gl). |
| `units.geojson` | **One point per counted home/RV**, tagged with its park and its own lat/lon. |
| `hubspot_import.csv` | CONFIRMED parks in import-ready columns for the HubSpot Properties object. Map the columns to your internal property names on import. |
| `review_queue.csv` / `review.html` | The checklist for human review. |
| `out/ALL_parks.csv` | National roll-up, built by `rollup`. |

---

## 3. Running it

```bash
cd mhp-mapper
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in the keys
```

```bash
python -m mhp_mapper check
```

`check` verifies every account and data source.

**Pilot (one county).** Riverside County, CA, with the HCD list as the accuracy check:

```bash
python -m mhp_mapper run --fips 06065 --vision 50 \
  --registry "ca_hcd:data/ca_hcd_parks.csv:name=Park Name,address=Park Address,city=City,zip=Zip,units=Total Lots"
```

Adjust the `--registry` column names to match the HCD file's real headers.

**A whole state, 8 counties at a time:**

```bash
python -m mhp_mapper run --states CA --workers 8 --parcels-dir data/parcels --vision 50
```

**After a reviewer fills in a county's `review_queue.csv`:**

```bash
python -m mhp_mapper apply-review out/06065
```

**Build the national file:**

```bash
python -m mhp_mapper rollup
```

Runs are **resumable**. A county with a `DONE` file is skipped, and a county that fails writes `ERROR.txt` without stopping the batch. Use `--force` to redo a county. To go faster, split states across machines (for example, one VM per region) and combine the `out/` folders before `rollup`.

**Tests:**

```bash
pip install pytest
python -m pytest tests
```

## Hard spending limits (enforced in code)

Every paid call (Google Places, the parcel API, Claude) goes through `mhp_mapper/budget.py` before it is made. Spending is logged per calendar month in `data/spend_ledger.json`, which all parallel workers share. If a call would exceed a cap, the bot **stops** instead of spending. Check spending at any time:

```bash
python -m mhp_mapper budget
```

| | Phase 1: pilot (now) | Phase 2: national (only after Dylan approves) |
|---|---|---|
| **Overall, per month** | **$100** | **$3,000** |
| Claude vision | $50 | $1,000 |
| Google Places | $0: free tier only, capped at 4,500 calls/month | $300 |
| Parcel API (ReportAll/Regrid) | $0: trial only, capped at 1,000 lookups | $1,500, prepaid with no overage |
| Compute | $0 (laptop) | $200 (one VM, shut down after the run) |

The caps live in `.env` (`MHP_CAP_*`; see `.env.example`). Also set the same limits inside each vendor's own console:
- an Anthropic monthly spend limit
- a Google Cloud daily quota and budget alert
- prepaid-only parcel packages

That way the caps hold even if someone runs other code.

---

## 4. Timeline to cover the US (estimate)

Assumptions:
- Roughly 40,000–50,000 MHPs plus about 15,000 or more RV parks/campgrounds.
- Up to about 100,000 candidate parcels once false positives are included.

These are planning figures. The pilot replaces them with measured ones.

| Phase | Work | Time |
|---|---|---|
| 0. Accounts | Regrid national license (the slow part), Google Cloud, Anthropic | Days to 2 weeks, mostly Regrid's contracting |
| 1. Pilot | 3 counties: Riverside CA (dense, with HCD counts), one non-CA metro, one rural county. Measure counts against HCD and tune the thresholds in `config.py` and `classify.py`. | 1 week |
| 2. National automated pass | 3,143 counties on 8–16 parallel workers. Footprint downloads are the bulk of compute: about 15–20 seconds per 5 km cell with parks. | **About 3–7 days** of machine time once parcel data is in hand |
| 3. Vision second opinion | Uncertain parks only (likely 20–30%) | Runs inside phase 2 |
| 4. Human review on Google Maps | Whatever vision can't settle (likely 10–20% of parks) at about 2–4 minutes each. Roughly 200–700 hours. | 1–4 months with one reviewer; 3–6 weeks with three |

**Bottom line:**
- **A national map with automated counts is about 3–4 weeks after Regrid data arrives.**
- **A fully human-verified dataset is 2–3 months**, set mainly by how many reviewers you have.

Start review in your target markets first. CONFIRMED parks are usable on day one.

**Vision cost (estimate):**
- Each call is one aerial image of about 1–3k input tokens, plus a short answer.
- On Claude Opus 5.5 ($4/$20 per million tokens), that's a few cents per park.
- About 20,000 uncertain parks comes to the high hundreds of dollars.
- Measure this in the pilot before the national run.

---

## 5. Known limits

- **RV counts are approximate.** RVs come and go, and NAIP's 0.6 m pixels are coarse for them. Use paid high-res imagery if RV counts matter.
- **NAIP is refreshed every 2–3 years per state.** Parks built or cleared since then need human review.
- **Footprints miss homes under heavy tree cover** and sometimes merge tightly packed homes. Vision and human review catch these.
- **The public Overpass servers are rate-limited and flaky.** For the national run, switch OSM to a downloaded state extract (Geofabrik). If Overpass fails, OSM is skipped for that county and the log says so.
- **The Regrid API query endpoints** (`sources/parcels.py: RegridAPI`) follow Regrid's public docs. Confirm them on the pilot. The bulk-file path (`ParcelFile`) is what the national run should use.
- **Pilot status:** the full pipeline (OSM, parcels, Overture footprints, classification, all outputs) has been run end to end on 26 real Hemet, CA parks, using OSM outlines as stand-in parcels. Thresholds still need tuning against HCD counts on real Regrid parcels.
