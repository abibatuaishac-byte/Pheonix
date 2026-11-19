"""Download the public datasets PHOENIX uses (run on a machine with internet access).

Writes raw files that scripts/build_city_data.py turns into app-ready data:
    phoenix_tiger_places.json, phoenix_fema_<slug>.json,
    phoenix_osm_facilities_<slug>.json, phoenix_osm_roads_<slug>.json

Sources (all keyless, public):
  * FEMA NFHL flood hazard zones - Esri Living Atlas "USA Flood Hazard Areas" mirror of the NFHL
    (the FEMA hazards.fema.gov MapServer has the same schema but is often very slow).
  * US Census TIGERweb incorporated places.
  * OpenStreetMap via Overpass (several public mirrors are tried; queries are tiled and
    tiles that time out are split, so dense cities still download).

Usage:
    python scripts/fetch_city_data.py --out data/raw [--city freetown new_orleans houston miami] [--skip-osm]
    python scripts/build_city_data.py --raw data/raw
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

UA = {"User-Agent": "PHOENIX-hackathon/1.0 (BAU Innovate 2026; contact: project team)"}
FEMA = ("https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
        "USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query")
TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/4/query"
OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]

# Overpass bbox = south,west,north,east. FEMA filter = where clause or lon/lat envelope.
CITIES = {
    "freetown": {"bbox": (8.28, -13.30, 8.50, -13.06)},
    "new_orleans": {"bbox": (29.86, -90.14, 30.20, -89.62), "fema_where": "DFIRM_ID='22071C'", "geoid": "2255000"},
    "houston": {"bbox": (29.68, -95.50, 29.84, -95.25), "fema_env": "-95.50,29.68,-95.25,29.84", "geoid": "4835000"},
    "miami": {"bbox": (25.70, -80.32, 25.86, -80.14), "fema_env": "-80.32,25.70,-80.14,25.86", "geoid": "1245000"},
}
FAC = ('(nwr["amenity"~"^(hospital|clinic|doctors|school|college|university|fire_station|police)$"]({b});'
       'nwr["healthcare"~"^(hospital|clinic|centre)$"]({b});nwr["emergency"="assembly_point"]({b});'
       'nwr["social_facility"="shelter"]({b}););out center tags qt;')
RDS = 'way["highway"~"^(motorway|trunk|primary|secondary)$"]({b});out geom tags qt;'
FEMA_FIELDS = "OBJECTID,FLD_ZONE,ZONE_SUBTY,SFHA_TF,STATIC_BFE,DFIRM_ID,VERSION_ID,SOURCE_CIT,STUDY_TYP"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fema(slug: str, cfg: dict, out: Path) -> None:
    params = {"outFields": FEMA_FIELDS, "returnGeometry": "true", "outSR": "4326", "geometryPrecision": "6",
              "maxAllowableOffset": "0.00002", "orderByFields": "OBJECTID", "f": "geojson", "resultRecordCount": "250"}
    if "fema_where" in cfg:
        params["where"] = cfg["fema_where"]
    else:
        params.update({"where": "1=1", "geometry": cfg["fema_env"], "geometryType": "esriGeometryEnvelope",
                       "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"})
    feats, off = {}, 0
    while True:
        params["resultOffset"] = str(off)
        j = requests.get(FEMA, params=params, headers=UA, timeout=180).json()
        if "error" in j:
            raise RuntimeError(j["error"])
        for f in j["features"]:
            feats[f["properties"]["OBJECTID"]] = f
        print(f"  FEMA {slug}: {len(feats)} features")
        if len(j["features"]) < 250:
            break
        off += len(j["features"])
    (out / f"phoenix_fema_{slug}.json").write_text(json.dumps({
        "type": "FeatureCollection", "source": FEMA.rsplit("/query", 1)[0], "retrieved": now(),
        "features": list(feats.values())}), encoding="utf-8")


def tiger(out: Path) -> None:
    ids = ",".join(f"'{c['geoid']}'" for c in CITIES.values() if "geoid" in c)
    j = requests.get(TIGER, headers=UA, timeout=120, params={
        "where": f"GEOID IN ({ids})", "outFields": "GEOID,NAME,BASENAME,STATE,AREALAND,AREAWATER,FUNCSTAT",
        "returnGeometry": "true", "outSR": "4326", "maxAllowableOffset": "0.0002", "geometryPrecision": "5",
        "f": "geojson"}).json()
    j["retrieved"], j["source"] = now(), TIGER.rsplit("/query", 1)[0]
    (out / "phoenix_tiger_places.json").write_text(json.dumps(j), encoding="utf-8")


def overpass(query: str) -> dict:
    last = None
    for attempt in range(2):
        for url in OVERPASS:
            try:
                r = requests.post(url, data={"data": query}, headers=UA, timeout=90)
                if r.ok:
                    return r.json()
                last = f"{url} HTTP {r.status_code}"
            except requests.RequestException as exc:
                last = f"{url} {exc}"
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(last)


def split(b, n=2):
    s, w, nn, e = b
    return [(s + (nn - s) * i / n, w + (e - w) * k / n, s + (nn - s) * (i + 1) / n, w + (e - w) * (k + 1) / n)
            for i in range(n) for k in range(n)]


def osm(slug: str, kind: str, cfg: dict, out: Path) -> None:
    body = FAC if kind == "facilities" else RDS
    els, failed, meta = {}, [], None

    def grab(b, depth):
        nonlocal meta
        bb = ",".join(f"{x:.4f}" for x in b)
        try:
            j = overpass("[out:json][timeout:55];" + body.format(b=bb))
            meta = j.get("osm3s")
            for e in j["elements"]:
                els[f"{e['type']}{e['id']}"] = e
        except RuntimeError:
            if depth >= 4:
                failed.append(bb)
                return
            for t in split(b):
                grab(t, depth + 1)

    for t in split(cfg["bbox"], 3):
        grab(t, 1)
        print(f"  OSM {kind} {slug}: {len(els)} elements, {len(failed)} failed tiles")
    (out / f"phoenix_osm_{kind}_{slug}.json").write_text(json.dumps({
        "osm3s": meta, "bbox": cfg["bbox"], "failed_tiles": failed, "elements": list(els.values())}), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("data/raw"))
    ap.add_argument("--city", nargs="*", default=list(CITIES))
    ap.add_argument("--skip-osm", action="store_true")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if any("geoid" in CITIES[c] for c in a.city):
        print("TIGERweb boundaries"); tiger(a.out)
    for c in a.city:
        cfg = CITIES[c]
        print(c)
        if "geoid" in cfg:
            fema(c, cfg, a.out)
        if not a.skip_osm:
            osm(c, "facilities", cfg, a.out)
            osm(c, "roads", cfg, a.out)
    print("Done. Now run: python scripts/build_city_data.py --raw", a.out)
