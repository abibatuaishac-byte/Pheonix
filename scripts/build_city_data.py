"""Turn raw downloads into the app-ready files in data/cities/<slug>/.

Raw inputs (written by scripts/fetch_city_data.py, one folder per run):
    phoenix_fema_<slug>.json            FEMA NFHL flood hazard zones (GeoJSON, EPSG:4326)
    phoenix_tiger_places.json           US Census TIGERweb incorporated-place boundaries
    phoenix_osm_facilities_<slug>.json  Overpass JSON (out center tags)
    phoenix_osm_roads_<slug>.json       Overpass JSON (out geom tags)

Outputs per city:
    boundary.geojson      study-area boundary
    fema_sfha.geojson     1% annual-chance zones (SFHA), dissolved by zone   [US only]
    fema_500yr.geojson    SFHA + 0.2% annual-chance zones, dissolved by zone [US only]
    facilities.geojson    critical facilities (points), clipped to study area
    roads.geojson         major roads (lines), clipped to study area
    manifest.json         sources, retrieval dates, counts, FIRM panel versions

Nothing is invented: facilities without a name keep name = null, and records
without coordinates are dropped and counted in the manifest.

Usage:
    python scripts/build_city_data.py --raw <folder with raw files> [--city new_orleans ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

import shapely
from shapely.geometry import LineString, Point, box, mapping, shape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from phoenix_core.geo import union_of_features  # noqa: E402

OUT = ROOT / "data" / "cities"
FREETOWN_WARDS = ROOT / "data" / "processed" / "wards.geojson"

# Study areas: (TIGER GEOID or None, optional clip box lon/lat)
STUDY = {
    "freetown": {"geoid": None, "clip": None},
    "new_orleans": {"geoid": "2255000", "clip": None},
    "houston": {"geoid": "4835000", "clip": (-95.50, 29.68, -95.25, 29.84)},
    "miami": {"geoid": "1245000", "clip": None},
}

CATEGORY_RULES = [  # first match wins
    ("Hospital", lambda t: t.get("amenity") == "hospital" or t.get("healthcare") == "hospital"),
    ("Clinic / health centre", lambda t: t.get("amenity") in ("clinic", "doctors") or t.get("healthcare") in ("clinic", "centre")),
    ("School", lambda t: t.get("amenity") == "school"),
    ("College / university", lambda t: t.get("amenity") in ("college", "university")),
    ("Fire station", lambda t: t.get("amenity") == "fire_station"),
    ("Police", lambda t: t.get("amenity") == "police"),
    ("Emergency shelter", lambda t: t.get("social_facility") == "shelter"),
    ("Emergency assembly point", lambda t: t.get("emergency") == "assembly_point"),
]
ROAD_CLASS = {"motorway": "Motorway", "trunk": "Trunk road", "primary": "Primary road", "secondary": "Secondary road"}


OSM_TYPE = {"nodes": "node", "node": "node", "ways_poly": "way", "ways_line": "way", "way": "way",
            "relations": "relation", "relation": "relation"}


def load_osm(raw: Path, kind: str, slug: str) -> dict | None:
    """Return Overpass-style {"elements": [...], "timestamp": str, "via": str} from either source.

    Accepts the Overpass JSON written by fetch_city_data.py (phoenix_osm_<kind>_<slug>.json) or a
    HOT OSM Raw Data API export (phoenix_hot_<kind>_<slug>.zip / .geojson). Both are OpenStreetMap data.
    """
    ov = raw / f"phoenix_osm_{kind}_{slug}.json"
    hot = [p for p in (raw / f"phoenix_hot_{kind}_{slug}.zip", raw / f"phoenix_hot_{kind}_{slug}.geojson") if p.exists()]
    if hot:
        p = hot[0]
        meta = {}
        if p.suffix == ".zip":
            with zipfile.ZipFile(p) as z:
                name = next(n for n in z.namelist() if n.endswith(".geojson") and "clipping" not in n)
                fc = json.loads(z.read(name))
                for n in z.namelist():
                    if n.lower().endswith("readme.txt"):
                        for line in z.read(n).decode("utf-8", "replace").splitlines():
                            if line.startswith("Exported Timestamp"):
                                meta["last_update"] = line.split(":", 1)[1].strip() + " UTC (export time)"
        else:
            fc = json.loads(p.read_text(encoding="utf-8"))
        els = []
        for f in fc["features"]:
            pr = dict(f.get("properties") or {})
            tags = pr.pop("tags", None) or {k: v for k, v in pr.items() if k not in ("osm_id", "osm_type")}
            typ = OSM_TYPE.get(str(pr.get("osm_type", "")).lower(), "node")
            g = f.get("geometry") or {}
            e = {"type": typ, "id": pr.get("osm_id"), "tags": {k: v for k, v in tags.items() if v not in (None, "")}}
            if g.get("type") == "Point":
                e["lat"], e["lon"] = g["coordinates"][1], g["coordinates"][0]
            elif g.get("type") == "LineString":
                e["type"] = "way"
                e["geometry"] = [{"lon": x, "lat": y} for x, y in g["coordinates"]]
            elif g.get("type") == "MultiLineString":
                for i, part in enumerate(g["coordinates"]):
                    els.append({**e, "type": "way", "id": f"{e['id']}_{i}",
                                "geometry": [{"lon": x, "lat": y} for x, y in part]})
                continue
            else:
                continue
            els.append(e)
        stamp_file = raw / "hot_last_updated.txt"
        ts = (meta.get("last_update") or meta.get("lastUpdated") or fc.get("lastUpdated")
              or (stamp_file.read_text().strip() if stamp_file.exists() else ""))
        return {"elements": els, "timestamp": str(ts), "via": "HOT OSM Raw Data API (api-prod.raw-data.hotosm.org)"}
    if ov.exists():
        src = json.loads(ov.read_text(encoding="utf-8"))
        return {"elements": src["elements"], "timestamp": (src.get("osm3s") or {}).get("timestamp_osm_base", ""),
                "via": "Overpass API", "failed_tiles": src.get("failed_tiles", [])}
    return None


def categorise(tags: dict) -> str | None:
    for name, rule in CATEGORY_RULES:
        if rule(tags):
            return name
    return None


def write_fc(path: Path, features: list[dict], **extra) -> None:
    path.write_text(json.dumps({"type": "FeatureCollection", **extra, "features": features},
                               separators=(",", ":")), encoding="utf-8")


def round_geom(g, nd=5):
    return shapely.transform(g, lambda xy: xy.round(nd))


# ---------------------------------------------------------------- study areas
def study_area(slug: str, raw: Path):
    cfg = STUDY[slug]
    if slug == "freetown":
        wards = json.loads(FREETOWN_WARDS.read_text(encoding="utf-8"))["features"]
        geom = union_of_features(wards)
        props = {"name": "Freetown City Council wards", "source": "World Bank Freetown Flood Risk Assessment"}
        return geom, props
    places = json.loads((raw / "phoenix_tiger_places.json").read_text(encoding="utf-8"))
    feat = next(f for f in places["features"] if f["properties"]["GEOID"] == cfg["geoid"])
    geom = shapely.make_valid(shape(feat["geometry"]))
    props = {"name": feat["properties"]["NAME"], "geoid": cfg["geoid"], "source": "US Census Bureau TIGERweb",
             "retrieved": places.get("retrieved", "")[:10]}
    if cfg["clip"]:
        geom = geom.intersection(box(*cfg["clip"]))
        props["name"] += " - study area"
        props["clip_bbox"] = list(cfg["clip"])
    return geom, props


# ---------------------------------------------------------------- FEMA
def display_feature(f: dict) -> dict:
    g = shape(f["geometry"]).simplify(0.0001, preserve_topology=True)
    parts = [p for p in shapely.get_parts(g) if p.geom_type == "Polygon" and p.area * 1.2e10 >= 1000]
    g = round_geom(shapely.MultiPolygon(parts), 5) if parts else g
    return {"type": "Feature", "geometry": mapping(g), "properties": f["properties"]}


def fema_zone_group(p: dict) -> str | None:
    if p.get("SFHA_TF") == "T":
        return "SFHA"
    sub = (p.get("ZONE_SUBTY") or "").upper()
    if p.get("FLD_ZONE") == "X" and "0.2" in sub:
        return "X500"
    if "LEVEE" in sub:
        return "LEVEE"
    return None


def build_fema(slug: str, raw: Path, area, out: Path) -> dict:
    src = json.loads((raw / f"phoenix_fema_{slug}.json").read_text(encoding="utf-8"))
    groups: dict[tuple, list] = {}
    panels = Counter()
    for f in src["features"]:
        p = f["properties"]
        grp = fema_zone_group(p)
        if grp is None or not f.get("geometry"):
            continue
        panels[(p.get("DFIRM_ID"), p.get("VERSION_ID"))] += 1
        sub = p.get("ZONE_SUBTY") or ""
        groups.setdefault((grp, p["FLD_ZONE"], sub), []).append(f)
    area_p = shapely.prepared.prep(area) if hasattr(shapely, "prepared") else None  # noqa: F841
    dissolved = []
    for (grp, zone, sub), feats in sorted(groups.items()):
        g = union_of_features(feats)
        if g is None:
            continue
        g = g.intersection(area)
        if g.is_empty:
            continue
        # Display/analysis copy: ~4 m simplification, slivers < 100 m2 dropped (same approach as Freetown).
        parts = [p for p in shapely.get_parts(g.simplify(0.00004, preserve_topology=True))
                 if p.geom_type == "Polygon" and p.area * 1.2e10 >= 100]
        if not parts:
            continue
        g = round_geom(shapely.make_valid(shapely.MultiPolygon(parts)).buffer(0), 5)
        label = zone + (f" ({sub.title()})" if sub else "")
        dissolved.append({"type": "Feature", "geometry": mapping(g),
                          "properties": {"group": grp, "zone": zone, "subtype": sub or None, "label": label}})
    sfha = [f for f in dissolved if f["properties"]["group"] == "SFHA"]
    x500 = [f for f in dissolved if f["properties"]["group"] in ("SFHA", "X500")]
    levee = [f for f in dissolved if f["properties"]["group"] == "LEVEE"]
    meta = {"source": src.get("source"), "retrieved": src.get("retrieved", "")[:10]}
    write_fc(out / "fema_sfha.geojson", sfha, **meta)
    write_fc(out / "fema_500yr.geojson", x500, **meta)
    if levee:
        write_fc(out / "fema_levee.geojson", levee, **meta)
    # Lighter copies for the web map only (~10 m, parts < 1000 m2 dropped). Analysis uses the files above.
    for name in ("fema_sfha", "fema_500yr"):  # precompute dissolved analysis geometry (see data.hazard_geometry)
        u = union_of_features(json.loads((out / f"{name}.geojson").read_text(encoding="utf-8"))["features"])
        (out / f"{name}.union.wkb").write_bytes(shapely.to_wkb(u))
    for name, feats in (("fema_sfha", sfha), ("fema_500yr", x500), ("fema_levee", levee)):
        if feats:
            write_fc(out / f"{name}_display.geojson", [display_feature(f) for f in feats], **meta, display_only=True)
    return {
        "retrieved": src.get("retrieved", "")[:10],
        "source_service": src.get("source"),
        "raw_features": len(src["features"]),
        "zones": {f["properties"]["label"]: f["properties"]["group"] for f in dissolved},
        "firm_panels": [{"DFIRM_ID": k[0], "VERSION_ID": k[1], "features": v} for k, v in panels.most_common()],
    }


# ---------------------------------------------------------------- OSM
def build_facilities(slug: str, raw: Path, area, out: Path) -> dict:
    src = load_osm(raw, "facilities", slug)
    shapely.prepare(area)
    feats, dropped_coords, outside, uncategorised = [], 0, 0, 0
    seen = set()
    for e in src["elements"]:
        key = f"{e['type']}/{e['id']}"
        if key in seen:
            continue
        seen.add(key)
        tags = e.get("tags", {})
        if e.get("lat") is not None:  # nodes, or HOT centroids of polygons
            lat, lon = e.get("lat"), e.get("lon")
        else:
            c = e.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            dropped_coords += 1
            continue
        cat = categorise(tags)
        if cat is None:
            uncategorised += 1
            continue
        if not area.contains(Point(lon, lat)):
            outside += 1
            continue
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                      "properties": {"osm_id": key, "name": tags.get("name"), "category": cat,
                                     "operator": tags.get("operator"), "emergency": tags.get("emergency"),
                                     "beds": tags.get("beds"), "addr": " ".join(filter(None, [
                                         tags.get("addr:housenumber") or tags.get("addr_housenumber"),
                                         tags.get("addr:street") or tags.get("addr_street")])) or None}})
    ts = src["timestamp"]
    write_fc(out / "facilities.geojson", feats, osm_timestamp=ts, via=src["via"],
             license="ODbL 1.0 (c) OpenStreetMap contributors")
    return {"osm_timestamp": ts, "via": src["via"], "incomplete_tiles": src.get("failed_tiles", []), "count": len(feats), "by_category": dict(Counter(f["properties"]["category"] for f in feats)),
            "unnamed": sum(1 for f in feats if not f["properties"]["name"]),
            "dropped_no_coordinates": dropped_coords, "outside_study_area": outside, "uncategorised": uncategorised}


def build_roads(slug: str, raw: Path, area, out: Path) -> dict:
    src = load_osm(raw, "roads", slug)
    if src is None:
        return {"count": 0, "missing": True}
    shapely.prepare(area)
    feats = []
    for e in src["elements"]:
        if e["type"] != "way" or not e.get("geometry"):
            continue
        tags = e.get("tags", {})
        cls = ROAD_CLASS.get(tags.get("highway"))
        if not cls:
            continue
        line = LineString([(pt["lon"], pt["lat"]) for pt in e["geometry"]])
        if not area.intersects(line):
            continue
        g = line.intersection(area)
        if g.is_empty or g.length == 0:
            continue
        g = g.simplify(0.00002)  # ~2 m: keeps shape, shrinks the web map payload
        feats.append({"type": "Feature", "geometry": mapping(round_geom(g, 5)),
                      "properties": {"osm_id": f"way/{e['id']}", "class": cls, "name": tags.get("name") or tags.get("ref"),
                                     "bridge": tags.get("bridge") not in (None, "no")}})
    ts = src["timestamp"]
    write_fc(out / "roads.geojson", feats, osm_timestamp=ts, via=src["via"],
             license="ODbL 1.0 (c) OpenStreetMap contributors")
    return {"osm_timestamp": ts, "via": src["via"], "count": len(feats), "bridges": sum(f["properties"]["bridge"] for f in feats)}


def build(slug: str, raw: Path) -> dict:
    out = OUT / slug
    out.mkdir(parents=True, exist_ok=True)
    area, props = study_area(slug, raw)
    write_fc(out / "boundary.geojson", [{"type": "Feature", "geometry": mapping(round_geom(area)), "properties": props}])
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest.update({"city": slug, "built_on": date.today().isoformat(), "study_area": props})
    retrieved = manifest.setdefault("retrieved", {})
    if slug != "freetown":
        retrieved["boundary"] = props.get("retrieved", "")
        if (raw / f"phoenix_fema_{slug}.json").exists():
            manifest["fema"] = build_fema(slug, raw, area, out)
            retrieved["fema"] = manifest["fema"]["retrieved"]
    if load_osm(raw, "facilities", slug) is not None:
        manifest["facilities"] = build_facilities(slug, raw, area, out)
        retrieved["osm"] = manifest["facilities"]["osm_timestamp"][:10]
    if load_osm(raw, "roads", slug) is not None:
        manifest["roads"] = build_roads(slug, raw, area, out)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, type=Path)
    ap.add_argument("--city", nargs="*", default=list(STUDY))
    a = ap.parse_args()
    for s in a.city:
        m = build(s, a.raw)
        print(s, json.dumps({k: v for k, v in m.items() if k in ("facilities", "roads")}, indent=None)[:400])
