"""Loaders that turn files on disk into objects the analysis engine understands.

These are plain functions; app.py wraps them in Streamlit caches.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import shapely
from shapely.geometry import shape

from .cities import FREETOWN_PROCESSED, City, Scenario
from .geo import looks_like_wgs84, union_of_features


def read_geojson(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def union_cache_path(path: Path) -> Path:
    return Path(path).with_suffix(".union.wkb")


def hazard_geometry(scenario: Scenario):
    """Union of all polygons in the scenario file (WGS84). Raises if not lon/lat.

    Dissolving thousands of FEMA polygons is slow, so build_city_data.py stores the result next to the
    GeoJSON as <name>.union.wkb; it is used only if it is newer than the GeoJSON.
    """
    cache = union_cache_path(scenario.file)
    if cache.exists() and cache.stat().st_mtime >= Path(scenario.file).stat().st_mtime:
        return shapely.from_wkb(cache.read_bytes())
    fc = read_geojson(scenario.file)
    feats = fc.get("features", [])
    if not looks_like_wgs84(feats):
        raise ValueError(f"{scenario.file.name} is not in WGS84 lon/lat; refusing to analyse it.")
    geom = union_of_features(feats)
    try:
        if geom is not None:
            cache.write_bytes(shapely.to_wkb(geom))
    except OSError:
        pass
    return geom


def facilities(city: City) -> pd.DataFrame:
    cols = ["osm_id", "name", "category", "lat", "lon", "operator", "addr", "source"]
    if not city.facilities_file.exists():
        return pd.DataFrame(columns=cols)
    fc = read_geojson(city.facilities_file)
    rows = []
    for f in fc["features"]:
        lon, lat = f["geometry"]["coordinates"][:2]
        p = f["properties"]
        rows.append({"osm_id": p.get("osm_id"), "name": p.get("name"), "category": p.get("category"),
                     "lat": lat, "lon": lon, "operator": p.get("operator"), "addr": p.get("addr"),
                     "source": "OpenStreetMap"})
    df = pd.DataFrame(rows, columns=cols)
    df["display_name"] = df["name"].fillna("").where(df["name"].notna(), None)
    df["display_name"] = [n if isinstance(n, str) and n else f"Unnamed {c.lower()} ({oid})"
                          for n, c, oid in zip(df["name"], df["category"], df["osm_id"])]
    return df


def roads(city: City) -> list[dict]:
    if not city.roads_file.exists():
        return []
    fc = read_geojson(city.roads_file)
    out = []
    for f in fc["features"]:
        g = shape(f["geometry"])
        p = f["properties"]
        out.append({"geometry": g, "class": p.get("class"), "name": p.get("name"), "bridge": p.get("bridge"),
                    "osm_id": p.get("osm_id")})
    return out


def file_meta(path: Path) -> dict:
    """Top-level metadata stored next to features (e.g. osm_timestamp, retrieved)."""
    if not path.exists():
        return {}
    fc = read_geojson(path)
    return {k: v for k, v in fc.items() if k not in ("features", "type")}


# ---------------------------------------------------------------- Freetown-specific (World Bank)
def freetown_summary() -> dict:
    return read_geojson(FREETOWN_PROCESSED / "summary.json")


def freetown_buildings() -> pd.DataFrame:
    return pd.read_csv(FREETOWN_PROCESSED / "exposed_buildings.csv")


def freetown_wards() -> pd.DataFrame:
    return pd.read_csv(FREETOWN_PROCESSED / "ward_exposure.csv")


def parse_uploaded_assets(df: pd.DataFrame) -> tuple[pd.DataFrame | None, str | None]:
    """Validate an uploaded infrastructure CSV (asset_name, asset_type, lat, lon)."""
    required = {"asset_name", "asset_type", "lat", "lon"}
    if not required.issubset(df.columns):
        return None, "CSV must include: asset_name, asset_type, lat, lon."
    df = df.copy()
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    bad = df["lat"].isna() | df["lon"].isna() | ~df["lat"].between(-90, 90) | ~df["lon"].between(-180, 180)
    msg = f"{int(bad.sum())} row(s) skipped: lat/lon missing, not numeric or out of range." if bad.any() else None
    df = df[~bad].copy()
    df["name"] = df["asset_name"].astype(str)
    df["display_name"] = df["name"]
    df["category"] = df["asset_type"].astype(str)
    df["osm_id"] = None
    df["source"] = "Uploaded by user"
    return df, msg
