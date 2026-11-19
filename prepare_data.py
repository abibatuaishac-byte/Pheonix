"""
PHOENIX data preparation (run once, offline).

Reads the World Bank "Freetown, Sierra Leone Flood Risk Assessment" downloads
(CC BY 4.0, JBA Risk Management / World Bank, 2023 project 2023s0512) and writes
small, app-ready files to data/processed/.

All spatial analysis runs here on the FULL-resolution flood extents. The app only
reads the results, so it needs no GIS libraries.

Usage:
    pip install geopandas pyogrio shapely
    python prepare_data.py --hazard-zip data/2023s0512_Freetown_Flood_Hazard_and_Loss_Maps.zip \
                           --exposure-zip data/2023s0512_Modelled_Exposure.zip
"""
import argparse
import json
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

OUT = Path(__file__).parent / "data" / "processed"
UTM = 32628  # WGS 84 / UTM zone 28N: metres, used for areas and simplification
RETURN_PERIODS = {"RP20": 20, "RP100": 100}  # the vector extents included in this download

SOURCE = {
    "dataset": "Freetown, Sierra Leone Flood Risk Assessment",
    "publisher": "World Bank Data Catalog (hazard modelling by JBA Risk Management; exposure by GEM Foundation / JBA)",
    "url": "https://datacatalog.worldbank.org/search/dataset/0066934/freetown-sierra-leone-flood-risk-assessment",
    "license": "CC BY 4.0 (GED4ALL provincial exposure tables: CC BY-NC-SA 4.0)",
    "hazard_type": "Undefended combined river + surface-water flood extent (modelled)",
    "processed_on": date.today().isoformat(),
}


def find(root: Path, pattern: str) -> Path:
    hits = sorted(root.rglob(pattern))
    if not hits:
        raise FileNotFoundError(f"{pattern} not found in {root}")
    return hits[0]


def display_geometry(geom_utm_gdf: gpd.GeoDataFrame, min_area_m2=200, tolerance_m=4):
    """Light version for the web map only: drops fragments < min_area and simplifies."""
    parts = geom_utm_gdf.explode(index_parts=False)
    parts = parts[parts.area >= min_area_m2]
    simple = parts.simplify(tolerance_m, preserve_topology=True).to_crs(4326)
    polys = [p for p in simple if p is not None and not p.is_empty and p.geom_type == "Polygon"]
    # Round coordinates to ~1 m to shrink the file (display only; no topology rebuild needed).
    return shapely.transform(shapely.MultiPolygon(polys), lambda xy: xy.round(5))


def write_geojson(path: Path, features: list):
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")


def main(hazard_zip: Path, exposure_zip: Path):
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        zipfile.ZipFile(hazard_zip).extractall(tmp / "hazard")
        with zipfile.ZipFile(exposure_zip) as zf:
            member = next(n for n in zf.namelist() if n.endswith("Freetown_ptf_v2.csv"))
            zf.extract(member, tmp / "exposure")
        buildings_csv = find(tmp / "exposure", "Freetown_ptf_v2.csv")

        # ---------- Buildings (modelled exposure portfolio) ----------
        b = pd.read_csv(buildings_csv, usecols=["Latitude", "Longitude", "NumberOfBuildings",
                                                "BuildingTIV", "Occupancy"])
        b = b.rename(columns={"Latitude": "lat", "Longitude": "lon", "NumberOfBuildings": "n",
                              "BuildingTIV": "value_usd", "Occupancy": "occupancy"})
        bg = gpd.GeoDataFrame(b, geometry=gpd.points_from_xy(b.lon, b.lat), crs=4326)

        # ---------- Wards with modelled Average Annual Loss ----------
        wards = gpd.read_file(find(tmp / "hazard", "Freetown_Admin4_Combined_Flood_AAL.shp")).to_crs(4326)
        wards = wards[["admin4Name", "admin3Name", "AALoss_los", "geometry"]].rename(
            columns={"admin4Name": "ward", "admin3Name": "area", "AALoss_los": "aal_usd"})
        bg = gpd.sjoin(bg, wards[["ward", "geometry"]], how="left", predicate="within").drop(columns="index_right")
        bg["ward"] = bg["ward"].fillna("Outside ward boundaries")

        # ---------- Recorded flash-flood locations (Freetown City Council) ----------
        pts = gpd.read_file(find(tmp / "hazard", "Freetown_City_Council_flash_flood_points.shp")).to_crs(4326)
        areas = gpd.read_file(find(tmp / "hazard", "Freetown_City_Council_flash_flood_areas.shp")).to_crs(4326)

        summary = {"source": SOURCE, "return_periods": {}, "buildings_total": int(bg.n.sum()),
                   "value_total_usd": float((bg.value_usd * 1).sum())}

        for label, rp in RETURN_PERIODS.items():
            ext = gpd.read_file(find(tmp / "hazard", f"Freetown-BL-combined-{label}-VE-02.shp")).to_crs(4326)
            ext["geometry"] = ext.geometry.make_valid()
            parts = ext[["geometry"]].explode(index_parts=False).reset_index(drop=True)

            # Full-resolution overlays
            hit = gpd.sjoin(bg[["geometry"]], parts, how="inner", predicate="intersects").index.unique()
            bg[f"in_{label}"] = bg.index.isin(hit)
            pt_hit = gpd.sjoin(pts[["geometry"]], parts, how="inner", predicate="intersects").index.unique()
            pts[f"in_{label}"] = pts.index.isin(pt_hit)
            # Distance (m) from each recorded flash-flood point to the modelled extent
            full_utm = ext.to_crs(UTM).geometry.union_all()
            pts[f"dist_{label}_m"] = pts.to_crs(UTM).distance(full_utm).round(0)

            area_km2 = float(ext.to_crs(UTM).area.sum() / 1e6)
            exposed = bg[bg[f"in_{label}"]]
            by_occ = exposed.groupby("occupancy").agg(buildings=("n", "sum"), value_usd=("value_usd", "sum"))
            summary["return_periods"][label] = {
                "years": rp,
                "annual_chance_pct": round(100 / rp, 1),
                "extent_km2": round(area_km2, 2),
                "buildings_in_extent": int(exposed.n.sum()),
                "value_in_extent_usd": round(float(exposed.value_usd.sum()), 0),
                "by_occupancy": {k: {"buildings": int(v.buildings), "value_usd": round(float(v.value_usd), 0)}
                                 for k, v in by_occ.iterrows()},
                "recorded_flood_points_inside": int(pts[f"in_{label}"].sum()),
                "recorded_flood_points_within_100m": int((pts[f"dist_{label}_m"] <= 100).sum()),
                "recorded_flood_points_beyond_250m": int((pts[f"dist_{label}_m"] > 250).sum()),
                "recorded_flood_points_median_distance_m": float(pts[f"dist_{label}_m"].median()),
                "recorded_flood_points_total": int(len(pts)),
            }

            disp = display_geometry(ext.to_crs(UTM)[["geometry"]])
            write_geojson(OUT / f"flood_extent_{label}.geojson", [{
                "type": "Feature",
                "properties": {"return_period": label, "annual_chance_pct": round(100 / rp, 1),
                               "note": "Display copy: simplified ~4 m, fragments < 200 m2 removed. "
                                       "Analysis used full resolution."},
                "geometry": json.loads(shapely.to_geojson(disp)),
            }])
            print(f"{label}: {area_km2:.1f} km2, {int(exposed.n.sum()):,} buildings, "
                  f"{int(pts[f'in_{label}'].sum())}/{len(pts)} recorded flood points inside")

        # Per-ward exposure table
        ward_tbl = bg.groupby("ward").agg(
            buildings=("n", "sum"),
            **{f"buildings_{l}": (f"in_{l}", "sum") for l in RETURN_PERIODS},
        ).reset_index()
        for l in RETURN_PERIODS:
            v = bg[bg[f"in_{l}"]].groupby("ward").value_usd.sum()
            ward_tbl[f"value_{l}_usd"] = ward_tbl.ward.map(v).fillna(0).round(0)
        ward_tbl = ward_tbl.merge(wards[["ward", "area", "aal_usd"]], on="ward", how="left")
        ward_tbl.to_csv(OUT / "ward_exposure.csv", index=False)

        # Ward polygons (light) for the choropleth
        w = wards.copy()
        w["geometry"] = shapely.transform(w.to_crs(UTM).simplify(10).to_crs(4326).values, lambda xy: xy.round(5))
        w = w.merge(ward_tbl.drop(columns=["area", "aal_usd"]), on="ward", how="left")
        w.to_file(OUT / "wards.geojson", driver="GeoJSON")

        # Exposed buildings only (for the heat layer), rounded to ~1 m
        keep = bg[bg[[f"in_{l}" for l in RETURN_PERIODS]].any(axis=1)]
        keep[["lat", "lon", "n", "value_usd", "occupancy", "ward"] + [f"in_{l}" for l in RETURN_PERIODS]] \
            .round({"lat": 5, "lon": 5, "value_usd": 0}).to_csv(OUT / "exposed_buildings.csv", index=False)

        # Recorded flash-flood points and areas
        pts["Location"] = pts["Location"].where(pts["Location"].notna(), None)
        pts.to_file(OUT / "recorded_flood_points.geojson", driver="GeoJSON")
        areas.to_file(OUT / "recorded_flood_areas.geojson", driver="GeoJSON")

        (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print("Wrote", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hazard-zip", type=Path, required=True)
    ap.add_argument("--exposure-zip", type=Path, required=True)
    a = ap.parse_args()
    main(a.hazard_zip, a.exposure_zip)
