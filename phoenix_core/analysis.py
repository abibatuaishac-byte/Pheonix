"""Flood exposure engine shared by every city.

Inputs are always:
  * a hazard footprint (shapely geometry, WGS84) for one scenario, and
  * infrastructure: point facilities (DataFrame with lat/lon) and/or road lines.

Outputs are plain DataFrames / dicts so the UI and the assistant can use them
without knowing anything about the source datasets.

"Exposed" in PHOENIX always means *located inside (or within a chosen distance
of) a mapped or modelled flood hazard area*.  It is a screening result, not a
damage or loss estimate, and the UI says so wherever it is shown.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import shapely
from shapely.strtree import STRtree

from .geo import LocalProjection


@dataclass
class HazardIndex:
    """A hazard footprint prepared for fast repeated queries in metres."""

    geom_m: object          # projected (metres) geometry
    proj: LocalProjection
    parts: STRtree           # index of polygon parts for distance queries

    @classmethod
    def build(cls, geom_wgs84, proj: LocalProjection) -> "HazardIndex":
        g = proj.forward(geom_wgs84)
        shapely.prepare(g)
        return cls(g, proj, STRtree(_tiled_parts(g)))

    def distance_to_points(self, lons, lats) -> np.ndarray:
        """Distance in metres from each point to the hazard area (0 if inside)."""
        pts = self.proj.points(lons, lats)
        if len(pts) == 0:
            return np.array([])
        idx = self.parts.nearest(pts)
        nearest = self.parts.geometries.take(idx)
        return shapely.distance(pts, nearest)


def _tiled_parts(g, cell_m: float = 1000.0, max_coords: int = 400) -> list:
    """Polygon parts, with large parts cut into <= cell_m tiles.

    Tiles have disjoint interiors, so distances are unchanged and per-tile intersection lengths can be summed.
    Small tiles make nearest-distance and road clipping fast even for city-wide FEMA polygons.
    """
    out = []
    for part in shapely.get_parts(g):
        if shapely.get_num_coordinates(part) <= max_coords:
            out.append(part)
            continue
        minx, miny, maxx, maxy = part.bounds
        xs = np.arange(minx, maxx + cell_m, cell_m)
        ys = np.arange(miny, maxy + cell_m, cell_m)
        boxes = [shapely.box(x, y, x + cell_m, y + cell_m) for x in xs[:-1] for y in ys[:-1]] or [shapely.box(*part.bounds)]
        pieces = shapely.intersection(part, boxes)
        for pc in pieces:
            for q in shapely.get_parts(pc):
                if q.geom_type == "Polygon" and not q.is_empty and q.area > 0:
                    out.append(q)
    return out


def area_outside(a, b, cell: float = 0.02, max_coords: int = 400) -> float:
    """Area of ``a`` not covered by ``b`` (same value as ``a.difference(b).area``), in the units of the inputs.

    A single difference of two city-wide FEMA multipolygons (New Orleans: ~400k vertices, one 500-year part
    with 16k holes) needs hundreds of MB in one GEOS call and can fail with "bad allocation" when less memory
    is free. Here both inputs are cut into small tiles (the same tiling the app uses for hazard queries) and
    only tiles that touch are overlaid, so memory is bounded by tile size, not by city size.
    """
    if a is None or a.is_empty:
        return 0.0
    if b is None or b.is_empty:
        return float(a.area)
    ta = np.array(_tiled_parts(a, cell, max_coords), dtype=object)
    tree = STRtree(_tiled_parts(b, cell, max_coords))
    covered = np.zeros(len(ta), bool)
    covered[np.unique(tree.query(ta, predicate="covered_by")[0])] = True   # fully inside one b tile: 0
    near: dict[int, list[int]] = {}
    for i, j in zip(*tree.query(ta, predicate="intersects")):
        if not covered[i]:
            near.setdefault(int(i), []).append(int(j))
    out = 0.0
    for i in np.flatnonzero(~covered):
        js = near.get(int(i))
        out += ta[i].area if not js else shapely.difference(ta[i], shapely.union_all(tree.geometries.take(js))).area
    return float(out)


def facility_exposure(facilities: pd.DataFrame, hazard: HazardIndex | None, buffer_m: float = 0.0) -> pd.DataFrame:
    """Add ``dist_to_hazard_m`` and ``status`` columns to a copy of ``facilities``.

    status is one of: "Inside hazard area", "Within {buffer} m", "Outside", "Not assessed".
    """
    out = facilities.copy()
    if hazard is None or out.empty:
        out["dist_to_hazard_m"] = np.nan
        out["status"] = "Not assessed"
        out["inside"] = False
        out["exposed"] = False
        return out
    d = hazard.distance_to_points(out["lon"].to_numpy(), out["lat"].to_numpy())
    out["dist_to_hazard_m"] = np.round(d, 1)
    out["inside"] = d <= 0
    status = np.where(d <= 0, "Inside hazard area",
                      np.where(d <= buffer_m, f"Within {int(buffer_m)} m", "Outside"))
    out["status"] = status
    out["exposed"] = d <= buffer_m
    return out


def summarise_facilities(fx: pd.DataFrame) -> pd.DataFrame:
    """Counts by category: total, inside, within buffer (exclusive), exposed total."""
    if fx.empty or "exposed" not in fx:
        return pd.DataFrame(columns=["Category", "Total", "Inside", "Near edge", "Exposed", "Share exposed"])
    g = fx.groupby("category")
    t = pd.DataFrame({
        "Total": g.size(),
        "Inside": g["inside"].sum().astype(int),
        "Exposed": g["exposed"].sum().astype(int),
    })
    t["Near edge"] = t["Exposed"] - t["Inside"]
    t["Share exposed"] = (t["Exposed"] / t["Total"]).fillna(0)
    t = t.reset_index().rename(columns={"category": "Category"})
    return t[["Category", "Total", "Inside", "Near edge", "Exposed", "Share exposed"]].sort_values(
        "Exposed", ascending=False, ignore_index=True)


def road_exposure(roads: list[dict], hazard: HazardIndex | None, buffer_m: float = 0.0) -> dict:
    """Length of road network inside the (optionally buffered) hazard area.

    roads: list of {"geometry": shapely LineString (WGS84), "class": str, "bridge": bool, "name": str}
    Returns totals by road class plus a list of the most-affected named roads.
    """
    if hazard is None or not roads:
        return {"assessed": False}
    # Bridges and elevated sections (OSM bridge=yes) are excluded: they usually sit above flood level and no
    # elevation data is available to judge them, so counting them as "in the hazard area" would mislead.
    n_bridge = sum(1 for r in roads if r.get("bridge"))
    roads = [r for r in roads if not r.get("bridge")]
    if not roads:
        return {"assessed": False}
    lines = [hazard.proj.forward(r["geometry"]) for r in roads]
    lengths = shapely.length(lines)
    inside_len = np.zeros(len(lines))
    exposed_geoms = []
    # Only intersect each road with the hazard polygon parts near it (fast for hazard layers with
    # thousands of parts, e.g. FEMA zones). Parts come from a valid dissolved geometry, so they don't overlap.
    probe = shapely.buffer(lines, buffer_m) if buffer_m > 0 else lines
    road_idx, part_idx = hazard.parts.query(probe, predicate="intersects")
    parts_geoms = hazard.parts.geometries
    for i in np.unique(road_idx):
        local = parts_geoms.take(part_idx[road_idx == i])
        if buffer_m > 0:  # buffered tiles overlap: merge them first
            area = shapely.union_all(shapely.buffer(local, buffer_m))
            seg = shapely.intersection(lines[i], area)
        else:  # tiles have disjoint interiors: clip piecewise and merge the pieces
            seg = shapely.line_merge(shapely.union_all(shapely.intersection(lines[i], local)))
        inside_len[i] = seg.length
        if seg.length > 0:
            exposed_geoms.append({"geometry": hazard.proj.inverse(seg), "name": roads[i].get("name") or "Unnamed road",
                                  "class": roads[i]["class"], "bridge": bool(roads[i].get("bridge")),
                                  "km": round(seg.length / 1000, 2)})
    df = pd.DataFrame({
        "class": [r["class"] for r in roads],
        "name": [r.get("name") or "" for r in roads],
        "bridge": [bool(r.get("bridge")) for r in roads],
        "length_m": lengths,
        "inside_m": inside_len,
    })
    by_class = df.groupby("class").agg(total_km=("length_m", "sum"), exposed_km=("inside_m", "sum"))
    by_class = (by_class / 1000).round(1)
    by_class["Share exposed"] = (by_class["exposed_km"] / by_class["total_km"]).fillna(0)
    named = df[df["name"] != ""].groupby("name")["inside_m"].sum().sort_values(ascending=False)
    named = (named[named > 0] / 1000).round(2).head(10)
    return {
        "assessed": True,
        "total_km": round(df["length_m"].sum() / 1000, 1),
        "exposed_km": round(df["inside_m"].sum() / 1000, 1),
        "by_class": by_class.reset_index(),
        "top_named_km": named,
        "bridge_segments_excluded": int(n_bridge),
        "exposed_geoms": exposed_geoms,
    }


def hazard_area_stats(hazard: HazardIndex, study_area_wgs84) -> dict:
    """Area of the hazard footprint inside the study area (km2 and share)."""
    sa = hazard.proj.forward(study_area_wgs84)
    inter = hazard.geom_m.intersection(sa)
    return {"hazard_km2": round(inter.area / 1e6, 2), "study_km2": round(sa.area / 1e6, 2),
            "share": (inter.area / sa.area) if sa.area else 0.0}


def grid_points(study_area_wgs84, proj: LocalProjection, spacing_m: float = 500.0) -> "pd.DataFrame":
    """Regular sample points over the study area: a neutral 'demand' surface for service-gap what-ifs."""
    sa = proj.forward(study_area_wgs84)
    minx, miny, maxx, maxy = sa.bounds
    xs = np.arange(minx + spacing_m / 2, maxx, spacing_m)
    ys = np.arange(miny + spacing_m / 2, maxy, spacing_m)
    gx, gy = np.meshgrid(xs, ys)
    pts = shapely.points(gx.ravel(), gy.ravel())
    shapely.prepare(sa)
    pts = pts[shapely.contains(sa, pts)]
    if len(pts) == 0:
        return pd.DataFrame(columns=["lat", "lon"])
    back = proj.inverse(shapely.multipoints(pts))
    coords = shapely.get_coordinates(back)
    return pd.DataFrame({"lon": coords[:, 0], "lat": coords[:, 1]})


def service_gap(fx: pd.DataFrame, service_categories: list[str], demand: pd.DataFrame,
                proj: LocalProjection) -> dict:
    """WHAT IF exposed facilities of a service type stop operating?

    For every demand point (e.g. schools or exposed buildings) compute the distance
    to the nearest *unexposed* facility of the given service categories, compared
    with the nearest facility of that type when nothing is lost.

    Returns medians and the share of demand points whose nearest facility gets
    further away.  Straight-line distances, not travel time.
    """
    svc = fx[fx["category"].isin(service_categories)]
    if svc.empty or demand.empty:
        return {"assessed": False, "reason": "No facilities of this type or no demand points."}
    ok = svc[~svc["exposed"]]
    dem = proj.points(demand["lon"], demand["lat"])
    all_pts = proj.points(svc["lon"], svc["lat"])
    tree_all = STRtree(all_pts)
    d_before = shapely.distance(dem, tree_all.geometries.take(tree_all.nearest(dem)))
    if ok.empty:
        return {"assessed": True, "facilities_total": len(svc), "facilities_lost": len(svc),
                "facilities_remaining": 0, "median_before_m": float(np.median(d_before)),
                "median_after_m": None, "share_further": 1.0, "all_lost": True}
    ok_pts = proj.points(ok["lon"], ok["lat"])
    tree_ok = STRtree(ok_pts)
    d_after = shapely.distance(dem, tree_ok.geometries.take(tree_ok.nearest(dem)))
    further = d_after > d_before + 1
    return {
        "assessed": True,
        "facilities_total": int(len(svc)),
        "facilities_lost": int(len(svc) - len(ok)),
        "facilities_remaining": int(len(ok)),
        "median_before_m": float(np.median(d_before)),
        "median_after_m": float(np.median(d_after)),
        "p90_after_m": float(np.percentile(d_after, 90)),
        "share_further": float(further.mean()),
        "n_demand": int(len(demand)),
        "all_lost": False,
    }
