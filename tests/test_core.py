"""Unit tests for the PHOENIX analysis engine.  Run: python -m pytest -q"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Polygon, box

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phoenix_core import analysis, assistant, data  # noqa: E402
from phoenix_core.cities import CITIES  # noqa: E402
from phoenix_core.geo import LocalProjection, looks_like_wgs84, union_of_features  # noqa: E402

PROJ = LocalProjection(8.47, -13.23)
# ~1.1 km square hazard just east of the projection centre
HAZ = box(-13.23, 8.47, -13.22, 8.48)


@pytest.fixture
def hz():
    return analysis.HazardIndex.build(HAZ, PROJ)


def fac(rows):
    return pd.DataFrame(rows, columns=["display_name", "category", "lat", "lon"]).assign(source="test", osm_id=None)


def test_projection_distance_is_metric():
    a, b = PROJ.points([-13.23, -13.22], [8.47, 8.47])
    d = a.distance(b)
    assert 1095 < d < 1105  # 0.01 deg lon at 8.47N ~= 1100 m


def test_projection_roundtrip():
    g = Polygon([(-13.2, 8.4), (-13.1, 8.4), (-13.1, 8.5)])
    back = PROJ.inverse(PROJ.forward(g))
    assert back.equals_exact(g, 1e-9)


def test_facility_inside_outside_and_buffer(hz):
    f = fac([["in", "Hospital", 8.475, -13.225],          # inside
             ["near", "School", 8.475, -13.2195],          # ~55 m east of edge
             ["far", "School", 8.475, -13.20]])            # ~2 km away
    r0 = analysis.facility_exposure(f, hz, 0)
    assert list(r0["exposed"]) == [True, False, False]
    assert r0.loc[0, "dist_to_hazard_m"] == 0
    assert 45 < r0.loc[1, "dist_to_hazard_m"] < 65
    r100 = analysis.facility_exposure(f, hz, 100)
    assert list(r100["exposed"]) == [True, True, False]
    assert r100.loc[1, "status"] == "Within 100 m"


def test_no_hazard_means_not_assessed():
    r = analysis.facility_exposure(fac([["x", "Hospital", 8.4, -13.2]]), None, 0)
    assert r.loc[0, "status"] == "Not assessed" and not r["exposed"].any()


def test_summary_counts(hz):
    f = fac([["a", "Hospital", 8.475, -13.225], ["b", "Hospital", 8.40, -13.10], ["c", "School", 8.475, -13.2195]])
    s = analysis.summarise_facilities(analysis.facility_exposure(f, hz, 100)).set_index("Category")
    assert s.loc["Hospital", "Total"] == 2 and s.loc["Hospital", "Exposed"] == 1
    assert s.loc["School", "Near edge"] == 1 and s.loc["School", "Inside"] == 0


def test_road_length_inside(hz):
    road = {"geometry": LineString([(-13.235, 8.475), (-13.215, 8.475)]), "class": "Primary road",
            "name": "Test Rd", "bridge": False}
    bridge = {"geometry": LineString([(-13.235, 8.476), (-13.215, 8.476)]), "class": "Primary road",
              "name": "Test Bridge", "bridge": True}
    r = analysis.road_exposure([road, bridge], hz, 0)
    assert abs(r["total_km"] - 2.2) < 0.05            # bridge excluded from totals
    assert abs(r["exposed_km"] - 1.1) < 0.05
    assert r["bridge_segments_excluded"] == 1 and len(r["exposed_geoms"]) == 1


def test_duplicate_osm_facilities_are_merged():
    df = pd.DataFrame({"osm_id": ["way/1", "way/2", "node/3", "node/4", "node/5"],
                       "name": ["Hope School", "hope school ", "Hope School", None, None],
                       "category": ["School", "School", "School", "Clinic / health centre", "Clinic / health centre"],
                       "lat": [8.47, 8.4702, 8.48, 8.47, 8.47], "lon": [-13.23, -13.23, -13.23, -13.23, -13.23]})
    out, merged = data.merge_duplicate_facilities(df, (8.47, -13.23))
    assert merged == 1                                 # 22 m apart merged; 1.1 km apart kept; unnamed never merged
    assert len(out) == 4 and (out["name"].fillna("").str.lower().str.strip() == "hope school").sum() == 2


def test_no_raw_json_dumps_in_app():
    src = (ROOT / "app.py").read_text(encoding="utf-8") + \
        (ROOT / "phoenix_core" / "alerts_ui.py").read_text(encoding="utf-8")
    assert "st.json(" not in src


def test_assistant_without_external_ai(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    city = CITIES["new_orleans"]
    facts = assistant.build_fact_sheet(city, city.scenarios[0], 0, analysis.summarise_facilities(pd.DataFrame()),
                                       pd.DataFrame(columns=["display_name", "category", "exposed"]), {"assessed": False}, {})
    ans, mode = assistant.answer("give me a situation brief", facts)
    assert mode == "Built-in PHOENIX analyst" and "{" not in ans


def test_assistant_external_failure_is_graceful(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    def fail(*a, **k):
        raise RuntimeError("401 invalid x-api-key sk-test")
    monkeypatch.setattr(assistant, "claude_answer", fail)
    city = CITIES["freetown"]
    facts = assistant.build_fact_sheet(city, city.scenarios[0], 0, analysis.summarise_facilities(pd.DataFrame()),
                                       pd.DataFrame(columns=["display_name", "category", "exposed"]), {"assessed": False}, {})
    ans, mode = assistant.answer("summary", facts)
    assert "sk-test" not in ans and "401" not in ans and "unavailable" in mode


def test_service_gap_gets_worse_when_facility_lost(hz):
    f = fac([["inside clinic", "Clinic / health centre", 8.475, -13.225],
             ["far clinic", "Clinic / health centre", 8.475, -13.19]])
    fx = analysis.facility_exposure(f, hz, 0)
    demand = pd.DataFrame({"lat": [8.475], "lon": [-13.226]})
    g = analysis.service_gap(fx, ["Clinic / health centre"], demand, PROJ)
    assert g["facilities_lost"] == 1 and g["median_after_m"] > g["median_before_m"]
    assert g["share_further"] == 1.0


def test_projected_coordinates_rejected():
    utm = [{"geometry": {"type": "Point", "coordinates": [700000, 936000]}}]
    wgs = [{"geometry": {"type": "Point", "coordinates": [-13.2, 8.4]}}]
    assert not looks_like_wgs84(utm) and looks_like_wgs84(wgs)


def test_union_repairs_invalid_polygon():
    bowtie = {"geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}}
    u = union_of_features([bowtie])
    assert u.is_valid and u.area > 0


def test_uploaded_asset_validation():
    df = pd.DataFrame({"asset_name": ["a", "b", "c"], "asset_type": ["x"] * 3,
                       "lat": [8.4, "bad", 95], "lon": [-13.2, -13.2, -13.2]})
    ok, msg = data.parse_uploaded_assets(df)
    assert len(ok) == 1 and "2 row" in msg
    bad, msg = data.parse_uploaded_assets(pd.DataFrame({"name": [1]}))
    assert bad is None


def test_assistant_never_answers_exposure_without_scenario():
    city = CITIES["freetown"]
    facts = assistant.build_fact_sheet(city, None, 0, analysis.summarise_facilities(pd.DataFrame()),
                                       pd.DataFrame(columns=["display_name", "category", "exposed"]),
                                       {"assessed": False}, {})
    out = assistant.grounded_answer("Which hospitals are exposed?", facts)
    assert "cannot say" in out


def test_assistant_numbers_come_from_facts(hz):
    city = CITIES["freetown"]
    f = fac([["Alpha Hospital", "Hospital", 8.475, -13.225], ["Beta Hospital", "Hospital", 8.40, -13.10]])
    fx = analysis.facility_exposure(f, hz, 0)
    sc = city.scenarios[0]
    facts = assistant.build_fact_sheet(city, sc, 0, analysis.summarise_facilities(fx), fx, {"assessed": False}, {})
    out = assistant.grounded_answer("which hospitals are exposed", facts)
    assert "1 of 2" in out and "Alpha Hospital" in out and "Beta Hospital" not in out


# ---------------------------------------------------------------- real-data integrity checks
@pytest.mark.parametrize("slug", list(CITIES))
def test_city_files_are_wgs84_and_inside_study_area(slug):
    c = CITIES[slug]
    for s in c.available_scenarios:
        assert looks_like_wgs84(data.read_geojson(s.file)["features"]), s.file
    if c.facilities_file.exists():
        df = data.facilities(c)
        assert df["lat"].between(-90, 90).all() and df["lon"].between(-180, 180).all()
        lat0, lon0 = c.center
        assert (abs(df["lat"] - lat0) < 0.5).all() and (abs(df["lon"] - lon0) < 0.5).all()
        assert df["category"].notna().all()


def test_freetown_summary_matches_building_file():
    s = data.freetown_summary()
    b = data.freetown_buildings()
    for rp in ("RP20", "RP100"):
        assert int(b[f"in_{rp}"].sum()) == s["return_periods"][rp]["buildings_in_extent"]


def test_sfha_inside_500yr_for_us_cities():
    for slug in ("new_orleans", "houston", "miami"):
        c = CITIES[slug]
        if len(c.available_scenarios) < 2:
            continue
        a = data.hazard_geometry(c.available_scenarios[0])
        b = data.hazard_geometry(c.available_scenarios[1])
        assert a.difference(b).area / a.area < 0.001, slug
