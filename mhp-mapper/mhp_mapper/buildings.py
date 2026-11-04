"""Building footprints from Overture Maps (free, public S3, no account).

Overture merges Microsoft, Google Open Buildings, Esri and OSM footprints. Queries are
grouped into ~5 km cells so one S3 scan serves every parcel in the cell, and each cell is
cached to local parquet so reruns are free.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

from shapely import wkt as shapely_wkt
from shapely.geometry.base import BaseGeometry

CELL_DEG = 0.05


def _connect():
    import duckdb

    con = duckdb.connect()
    con.sql("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;")
    # Overture's bucket is public: send no credentials (ambient AWS keys break anonymous reads).
    con.sql("CREATE OR REPLACE SECRET overture (TYPE s3, KEY_ID '', SECRET '', REGION 'us-west-2')")
    return con


def cell_key(lon: float, lat: float) -> tuple[int, int]:
    return math.floor(lon / CELL_DEG), math.floor(lat / CELL_DEG)


class BuildingStore:
    def __init__(self, release: str, cache_dir: Path):
        self.release = release
        self.cache_dir = cache_dir / "overture" / release
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._con = None
        self._mem: dict[tuple, list[BaseGeometry]] = {}

    @property
    def con(self):
        if self._con is None:
            self._con = _connect()
        return self._con

    def _fetch_bbox(self, key: tuple, bbox: tuple[float, float, float, float]) -> list[BaseGeometry]:
        tag = "_".join(f"{v:.6f}" for v in bbox).replace("-", "m").replace(".", "p")
        path = self.cache_dir / f"{key[0]}_{key[1]}_{tag}.parquet"
        if not path.exists():
            xmin, ymin, xmax, ymax = bbox
            src = f"s3://overturemaps-us-west-2/release/{self.release}/theme=buildings/type=building/*"
            self.con.sql(
                f"""COPY (
                    SELECT id, ST_AsText(geometry) AS wkt
                    FROM read_parquet('{src}', hive_partitioning=1)
                    WHERE bbox.xmin < {xmax} AND bbox.xmax > {xmin}
                      AND bbox.ymin < {ymax} AND bbox.ymax > {ymin}
                ) TO '{path}' (FORMAT parquet)"""
            )
        rows = self.con.sql(f"SELECT wkt FROM read_parquet('{path}')").fetchall()
        return [shapely_wkt.loads(r[0]) for r in rows]

    def prefetch(self, geoms: list[BaseGeometry]) -> None:
        """Group parcels by cell and download each cell's union bbox once."""
        groups: dict[tuple, list[BaseGeometry]] = defaultdict(list)
        for g in geoms:
            c = g.centroid
            groups[cell_key(c.x, c.y)].append(g)
        for key, gs in groups.items():
            xs = [b for g in gs for b in (g.bounds[0], g.bounds[2])]
            ys = [b for g in gs for b in (g.bounds[1], g.bounds[3])]
            bbox = (round(min(xs), 6), round(min(ys), 6), round(max(xs), 6), round(max(ys), 6))
            self._mem[(key, bbox)] = self._fetch_bbox(key, bbox)

    def within(self, parcel_geom: BaseGeometry) -> list[BaseGeometry]:
        """Footprints whose centroid falls inside the parcel."""
        c = parcel_geom.centroid
        key = cell_key(c.x, c.y)
        pool = [g for (k, _), gs in self._mem.items() if k == key for g in gs]
        if not pool:
            self.prefetch([parcel_geom])
            pool = [g for (k, _), gs in self._mem.items() if k == key for g in gs]
        prepared = parcel_geom.buffer(0)
        return [g for g in pool if prepared.contains(g.centroid)]
