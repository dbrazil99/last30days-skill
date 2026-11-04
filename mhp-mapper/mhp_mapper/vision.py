"""Second-opinion unit counts from public-domain NAIP aerial imagery + Claude vision.

Imagery: USDA NAIP via Microsoft Planetary Computer (free, public domain, ~0.6 m/pixel).
We deliberately do NOT use Google satellite tiles here: Google's terms prohibit extracting
data from its imagery. Google Maps links are only given to human reviewers to look at.
"""

from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path

import requests
from PIL import Image, ImageDraw

from . import budget
from .geo import bbox_of
from .models import ParkResult

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
RENDER = "https://planetarycomputer.microsoft.com/api/data/v1/item/bbox/{w},{s},{e},{n}/{px}x{py}.png"
M_PER_PX = 0.6
MAX_PX = 1568
MIN_PX = 768

SCHEMA = {
    "type": "object",
    "properties": {
        "is_mobile_home_or_rv_park": {"type": "boolean"},
        "mobile_home_count": {"type": "integer"},
        "rv_count": {"type": "integer"},
        "vacant_pads": {"type": "integer"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "notes": {"type": "string"},
    },
    "required": ["is_mobile_home_or_rv_park", "mobile_home_count", "rv_count",
                 "vacant_pads", "confidence", "notes"],
    "additionalProperties": False,
}

PROMPT = """This is a USDA NAIP aerial photo (~0.6 m per pixel, north up, {width_m:.0f} m wide).
The red outline is one property parcel. Count only what is INSIDE the red outline.

Count:
- mobile_home_count: manufactured / mobile homes (long rectangular roofs, ~4-10 m wide, usually in rows along internal streets; include single- and double-wides and park models).
- rv_count: RVs, travel trailers and fifth-wheels parked in sites (smaller, ~2.5 m wide, often with a vehicle alongside). Do not count cars or boats.
- vacant_pads: empty spaces/pads clearly laid out for a home or RV.
Exclude clubhouses, laundry/office buildings, garages, sheds and carports.
Set is_mobile_home_or_rv_park true only if this parcel is a park with many homes/RVs on it (not one home on its own lot, not a subdivision of separately owned lots, not an RV dealer or storage lot).
Count carefully row by row. If trees or image quality make counts uncertain, say so in notes and lower confidence."""


def naip_chip(result: ParkResult) -> tuple[bytes, float] | None:
    geom = result.parcel.geom
    w, s, e, n = bbox_of(geom, pad_m=25)
    poly = {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
    r = requests.post(
        STAC,
        json={"collections": ["naip"], "intersects": poly, "limit": 20,
              "sortby": [{"field": "properties.datetime", "direction": "desc"}]},
        timeout=60,
    )
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        return None
    newest_year = feats[0]["properties"]["datetime"][:4]
    items = [f["id"] for f in feats if f["properties"]["datetime"][:4] == newest_year]

    width_m = (e - w) * 111_320 * math.cos(math.radians((s + n) / 2))
    height_m = (n - s) * 110_540
    native = max(width_m, height_m) / M_PER_PX
    scale = min(MAX_PX / native, max(1.0, MIN_PX / native))  # upsample small parks, cap big ones
    px = max(64, int(width_m / M_PER_PX * scale))
    py = max(64, int(height_m / M_PER_PX * scale))
    url = RENDER.format(w=w, s=s, e=e, n=n, px=px, py=py)
    im = Image.new("RGBA", (px, py), (0, 0, 0, 255))
    for item in items:  # NAIP tiles abut; paste each tile's valid pixels into one chip
        tile = requests.get(
            url,
            params={"collection": "naip", "item": item, "assets": "image",
                    "asset_bidx": "image|1,2,3", "nodata": 0},
            timeout=120,
        )
        if tile.status_code != 200:
            continue
        layer = Image.open(io.BytesIO(tile.content)).convert("RGBA").resize((px, py))
        im.alpha_composite(layer)
    im = im.convert("RGB")
    draw = ImageDraw.Draw(im)
    polys = getattr(geom, "geoms", [geom])
    for poly in polys:
        pts = [((x - w) / (e - w) * im.width, (n - y) / (n - s) * im.height) for x, y in poly.exterior.coords]
        draw.line(pts, fill=(255, 0, 0), width=3)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue(), width_m


def count_with_claude(result: ParkResult, model: str, save_dir: Path | None = None) -> dict | None:
    import anthropic

    chip = naip_chip(result)
    if chip is None:
        return None
    png, width_m = chip
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / f"{result.park_id}.png").write_bytes(png)

    budget.guard("claude")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.standard_b64encode(png).decode()}},
                {"type": "text", "text": PROMPT.format(width_m=width_m)},
            ],
        }],
    )
    budget.settle_claude(model, response.usage.input_tokens, response.usage.output_tokens)
    if response.stop_reason == "refusal":
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text) if text else None
