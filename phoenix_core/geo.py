"""Small geometry helpers that avoid heavyweight GIS dependencies.

All hazard and infrastructure data is stored in WGS84 (EPSG:4326, lon/lat).
Distances and lengths are computed in a local metric projection (azimuthal
equirectangular around the city centre).  For a study area a few tens of
kilometres across, the distance error of this projection is well under 1 %,
which is far below the positional uncertainty of the source data.
"""
from __future__ import annotations

import math
from typing import Iterable, Iterator

import numpy as np
import shapely
from shapely.geometry import shape

EARTH_R = 6_371_008.8  # mean Earth radius, metres


class LocalProjection:
    """lon/lat <-> local metres around (lat0, lon0)."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = math.radians(1) * EARTH_R * math.cos(math.radians(lat0))
        self.ky = math.radians(1) * EARTH_R

    def forward(self, geom):
        return shapely.transform(geom, lambda xy: np.column_stack(
            ((xy[:, 0] - self.lon0) * self.kx, (xy[:, 1] - self.lat0) * self.ky)))

    def inverse(self, geom):
        return shapely.transform(geom, lambda xy: np.column_stack(
            (xy[:, 0] / self.kx + self.lon0, xy[:, 1] / self.ky + self.lat0)))

    def points(self, lons, lats):
        lons = np.asarray(lons, dtype=float)
        lats = np.asarray(lats, dtype=float)
        return shapely.points((lons - self.lon0) * self.kx, (lats - self.lat0) * self.ky)


def iter_coords(geom: dict | None) -> Iterator[tuple[float, float]]:
    """Yield (x, y) pairs from any GeoJSON geometry dict."""
    if not geom:
        return
    if geom.get("type") == "GeometryCollection":
        for g in geom.get("geometries", []):
            yield from iter_coords(g)
        return
    stack = [geom.get("coordinates")]
    while stack:
        item = stack.pop()
        if isinstance(item, (list, tuple)) and item and isinstance(item[0], (int, float)):
            yield item[0], item[1]
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def looks_like_wgs84(features: Iterable[dict], sample_size: int = 500) -> bool:
    """True if a sample of coordinates falls inside lon/lat bounds.

    Projected files (UTM metres, State Plane feet) would otherwise be drawn in the
    wrong place, so callers refuse to map anything that fails this check.
    """
    sample = []
    for f in list(features)[:50]:
        sample.extend(iter_coords(f.get("geometry")))
        if len(sample) >= sample_size:
            break
    return bool(sample) and all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in sample)


def union_of_features(features: list[dict]):
    """Valid union of all polygonal features in a GeoJSON feature list (WGS84)."""
    geoms = []
    for f in features:
        g = f.get("geometry")
        if not g:
            continue
        s = shape(g)
        if not s.is_valid:
            s = shapely.make_valid(s)
        if s.geom_type in ("Polygon", "MultiPolygon", "GeometryCollection") and not s.is_empty:
            geoms.append(s)
    if not geoms:
        return None
    u = shapely.union_all(geoms)
    # make_valid may leave lines/points in a collection; keep polygonal parts only.
    if u.geom_type == "GeometryCollection":
        polys = [g for g in u.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        u = shapely.union_all(polys) if polys else None
    return u
