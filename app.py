"""PHOENIX - AI-Powered Global Urban Disaster Intelligence Platform.

Run:  streamlit run app.py

One app, many cities.  City-specific data lives in data/; all analysis is shared
(phoenix_core/).  Every number on screen is computed from files on disk or a live
API response, or is explicitly labelled ILLUSTRATIVE.
"""
from __future__ import annotations

import hashlib
import json

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from phoenix_core import analysis, assistant, data, live, maps, theme
from phoenix_core.cities import CITIES, City, Scenario
from phoenix_core.geo import LocalProjection, looks_like_wgs84, union_of_features

st.set_page_config(page_title="PHOENIX | Urban Disaster Intelligence", page_icon="🔥", layout="wide",
                   initial_sidebar_state="expanded")
st.markdown(theme.CSS, unsafe_allow_html=True)

SECTIONS = ["Overview", "Live Intelligence", "Flood Risk Analysis", "Infrastructure Exposure",
            "WHAT IF? Scenario Simulator", "AI Intelligence Assistant", "Flood Alerts & SMS Notifications", "Data Sources and Methodology"]
SECTION_ICONS = ["🛰️", "📡", "🌊", "🏥", "🧪", "🤖", "🚨", "📚"]
BUFFERS = [0, 50, 100, 250, 500]
UPLOAD_KEY = "UPLOAD"


def money(v):
    return f"${v / 1e9:,.2f}B" if v >= 1e9 else (f"${v / 1e6:,.0f}M" if v >= 1e6 else f"${v:,.0f}")


# =============================================================== cached data access
@st.cache_data(show_spinner=False)
def load_fc(path: str) -> dict:
    return data.read_geojson(path)


@st.cache_resource(show_spinner=False)
def proj_for(slug: str) -> LocalProjection:
    c = CITIES[slug]
    return LocalProjection(*c.center)


@st.cache_resource(show_spinner=False)
def study_area(slug: str):
    c = CITIES[slug]
    if c.boundary_file.exists():
        return union_of_features(load_fc(str(c.boundary_file))["features"])
    return None


@st.cache_resource(show_spinner="Preparing hazard layer…")
def hazard_index(slug: str, scen_key: str):
    c = CITIES[slug]
    sc = next((s for s in c.available_scenarios if s.key == scen_key), None)
    if sc is None:
        return None
    geom = data.hazard_geometry(sc)
    return None if geom is None else analysis.HazardIndex.build(geom, proj_for(slug))


@st.cache_resource(show_spinner="Preparing uploaded hazard layer…")
def upload_index(slug: str, digest: str, _fc: dict):
    geom = union_of_features(_fc["features"])
    return None if geom is None else analysis.HazardIndex.build(geom, proj_for(slug))


@st.cache_data(show_spinner=False)
def facilities(slug: str) -> pd.DataFrame:
    return data.facilities(CITIES[slug])


@st.cache_resource(show_spinner=False)
def roads(slug: str):
    return data.roads(CITIES[slug])


@st.cache_data(show_spinner=False)
def grid(slug: str, spacing: int = 500) -> pd.DataFrame:
    sa = study_area(slug)
    return analysis.grid_points(sa, proj_for(slug), spacing) if sa is not None else pd.DataFrame(columns=["lat", "lon"])


def get_hazard(slug, scen_key):
    if scen_key == UPLOAD_KEY:
        up = st.session_state.get("upload_hazard")
        return upload_index(slug, up["digest"], up["fc"]) if up else None
    return hazard_index(slug, scen_key) if scen_key else None


@st.cache_data(show_spinner="Running exposure analysis…")
def exposure(slug: str, scen_key: str | None, buffer_m: int, upload_digest: str | None, assets_digest: str | None):
    fx = facilities(slug)
    up_assets = st.session_state.get("upload_assets")
    if up_assets is not None and assets_digest:
        fx = pd.concat([fx, up_assets], ignore_index=True)
    return analysis.facility_exposure(fx, get_hazard(slug, scen_key), buffer_m)


@st.cache_data(show_spinner="Measuring road network exposure…")
def road_exposure(slug: str, scen_key: str | None, buffer_m: int, upload_digest: str | None):
    hz = get_hazard(slug, scen_key)
    return analysis.road_exposure(roads(slug), hz, buffer_m) if hz is not None else {"assessed": False}


@st.cache_data(show_spinner="Simulating service disruption…")
def service_gap(slug, scen_key, buffer_m, cats: tuple, upload_digest, assets_digest):
    fx = exposure(slug, scen_key, buffer_m, upload_digest, assets_digest)
    return analysis.service_gap(fx, list(cats), grid(slug), proj_for(slug))


@st.cache_data(show_spinner=False)
def hazard_stats(slug, scen_key, upload_digest):
    hz, sa = get_hazard(slug, scen_key), study_area(slug)
    if hz is None or sa is None:
        return None
    hs = analysis.hazard_area_stats(hz, sa)
    if slug == "freetown" and scen_key in ("RP20", "RP100"):
        # Use the World Bank full-resolution extent area; the map copy drops fragments < 200 m2.
        km2 = data.freetown_summary()["return_periods"][scen_key]["extent_km2"]
        hs = {**hs, "hazard_km2": km2, "share": km2 / hs["study_km2"], "display_km2": hs["hazard_km2"]}
    return hs


@st.cache_data(show_spinner="Measuring flood zones…")
def zone_table(slug: str, scen_key: str) -> pd.DataFrame:
    from shapely.geometry import shape
    sc = next(s for s in CITIES[slug].available_scenarios if s.key == scen_key)
    pr = proj_for(slug)
    return pd.DataFrame([{"Zone": f["properties"]["label"],
                          "Annual chance": "1% (SFHA)" if f["properties"]["group"] == "SFHA" else "0.2%",
                          "Area (km²)": round(pr.forward(shape(f["geometry"])).area / 1e6, 2)}
                         for f in data.read_geojson(sc.file)["features"]]).sort_values("Area (km²)", ascending=False)


@st.cache_data(ttl=900, show_spinner="Fetching live forecast…")
def live_precip(lat, lon):
    return live.precipitation_forecast(lat, lon)


@st.cache_data(ttl=900, show_spinner="Fetching river discharge forecast…")
def live_river(lat, lon):
    return live.river_discharge(lat, lon)


@st.cache_data(ttl=300, show_spinner="Checking official alerts…")
def live_alerts(lat, lon):
    return live.nws_alerts(lat, lon)


# =============================================================== sidebar
with st.sidebar:
    st.markdown('<div style="font-weight:800;font-size:1.35rem;letter-spacing:.08em;color:#f8fafc">PHOENIX</div>'
                '<div class="px-muted" style="margin-bottom:.8rem">Turning Disaster Data Into Actionable Intelligence</div>',
                unsafe_allow_html=True)
    slug = st.selectbox("City", list(CITIES), format_func=lambda s: f"{CITIES[s].name}, {CITIES[s].country}",
                        key="city")
    city: City = CITIES[slug]
    section = st.radio("Navigate", SECTIONS, format_func=lambda s: f"{SECTION_ICONS[SECTIONS.index(s)]}  {s}",
                       key="section", label_visibility="collapsed")
    st.divider()

    with st.expander("Bring your own data", expanded=False):
        hz_file = st.file_uploader("Flood hazard GeoJSON (EPSG:4326)", type=["geojson", "json"], key=f"hz_{slug}")
        asset_file = st.file_uploader("Infrastructure CSV: asset_name, asset_type, lat, lon", type=["csv"],
                                      key=f"as_{slug}")
    # --- uploaded hazard layer (becomes a selectable scenario)
    st.session_state["upload_hazard"] = None
    upload_digest = None
    if hz_file is not None:
        raw = hz_file.getvalue()
        try:
            fc = json.loads(raw)
            feats = fc.get("features") or []
            if not feats:
                st.warning("Uploaded GeoJSON has no features.")
            elif not looks_like_wgs84(feats):
                st.error("Uploaded GeoJSON is not in longitude/latitude (EPSG:4326), so it was not drawn or analysed. "
                         "Re-export it as EPSG:4326.")
            elif union_of_features(feats) is None:
                st.warning("Uploaded GeoJSON contains no polygons; it can be shown but not used for exposure.")
            else:
                upload_digest = hashlib.sha1(raw).hexdigest()
                st.session_state["upload_hazard"] = {"fc": fc, "digest": upload_digest, "name": hz_file.name}
        except Exception as exc:
            st.error(f"GeoJSON could not be read: {exc}")
    # --- uploaded assets
    st.session_state["upload_assets"] = None
    assets_digest = None
    if asset_file is not None:
        try:
            parsed, msg = data.parse_uploaded_assets(pd.read_csv(asset_file))
            if parsed is None:
                st.error(msg)
            else:
                if msg:
                    st.warning(msg)
                parsed["category"] = "Uploaded: " + parsed["category"]
                st.session_state["upload_assets"] = parsed
                assets_digest = hashlib.sha1(asset_file.getvalue()).hexdigest()
        except Exception as exc:
            st.error(f"Could not read CSV: {exc}")

    scen_opts = [s.key for s in city.available_scenarios]
    if st.session_state["upload_hazard"]:
        scen_opts.append(UPLOAD_KEY)
    scen_lookup = {s.key: s for s in city.available_scenarios}
    if st.session_state["upload_hazard"]:
        scen_lookup[UPLOAD_KEY] = Scenario(UPLOAD_KEY, f"Uploaded layer: {st.session_state['upload_hazard']['name']}",
                                           "Uploaded", "user-supplied", None, None,
                                           "Hazard polygons uploaded in this session; source and date unverified.")
    if scen_opts:
        scen_key = st.radio("Flood scenario", scen_opts, format_func=lambda k: scen_lookup[k].label,
                            key=f"scen_{slug}")
    else:
        scen_key = None
        st.info("No verified flood hazard data is loaded for this city, so exposure is not calculated.")
    scenario: Scenario | None = scen_lookup.get(scen_key)
    buffer_m = st.select_slider("Screening distance from hazard edge", BUFFERS, value=0, key="buffer",
                                format_func=lambda b: "Inside only" if b == 0 else f"+{b} m",
                                help="Also flag assets within this distance of the hazard area. Useful because "
                                     "hazard edges are uncertain and nearby assets may lose access.")
    basemap = st.selectbox("Basemap", [b[0] for b in maps.BASEMAPS], key="basemap")
    st.caption("Hazard layers show **scenarios**, not forecasts. Live forecasts are in *Live Intelligence*.")

# =============================================================== shared context for this run
fx = exposure(slug, scen_key, buffer_m, upload_digest, assets_digest)
fsum = analysis.summarise_facilities(fx) if scenario else analysis.summarise_facilities(fx.iloc[0:0])
rinfo = road_exposure(slug, scen_key, buffer_m, upload_digest) if scenario else {"assessed": False}
hstats = hazard_stats(slug, scen_key, upload_digest) if scenario else None
boundary_fc = load_fc(str(city.boundary_file)) if city.boundary_file.exists() else None
is_freetown = slug == "freetown"
ft_summary = data.freetown_summary() if is_freetown else None
ft_cur = ft_summary["return_periods"].get(scen_key) if (is_freetown and scen_key in ("RP20", "RP100")) else None
n_exposed = int(fx["exposed"].sum()) if (scenario and "exposed" in fx) else None



def hazard_fc_for(key, display=True):
    """Hazard GeoJSON for the map. Uses the lighter *_display copy when one exists (analysis never does)."""
    if key == UPLOAD_KEY:
        return st.session_state["upload_hazard"]["fc"]
    f = scen_lookup[key].file
    disp = f.with_name(f.stem + "_display.geojson")
    return load_fc(str(disp if (display and disp.exists()) else f))


# =============================================================== header
status_tone = "ok" if city.has_flood_analysis else ("warn" if city.available_scenarios else "bad")
st.markdown(
    f'<div class="px-top"><div><div class="brand">PHOENIX <span>·</span> {theme.esc(section)}</div>'
    f'<div class="tag">AI-Powered Global Urban Disaster Intelligence Platform</div></div>'
    f'<div class="ctx"><b>{theme.esc(city.name)}</b>, {theme.esc(city.region)}, {theme.esc(city.country)}<br>'
    f'{theme.chip("Hazard: Flooding")}{theme.chip(city.analysis_status, status_tone)}'
    + (theme.chip(f"Scenario: {scenario.short}") if scenario else "") + "</div></div>",
    unsafe_allow_html=True)


# =============================================================== helpers
def render_map(height=560, *, hazard=True, all_scenarios=False, facilities_mode="all", show_roads=False,
               freetown_layers=(), key="map"):
    m = maps.base_map(city.center, city.zoom, basemap)
    maps.add_boundary(m, boundary_fc)
    legend = [("line", "#22d3ee", "Study area boundary")]
    if hazard and scenario:
        keys = scen_opts if all_scenarios else [scen_key]
        for k in keys:
            s = scen_lookup[k]
            maps.add_hazard(m, hazard_fc_for(k), f"Hazard: {s.label}",
                            "upload" if k == UPLOAD_KEY else ("modelled" if s.kind == "modelled" else "SFHA"),
                            show=(k == scen_key))
        if city.data_dir.joinpath("fema_levee.geojson").exists() and all_scenarios:
            lv = city.data_dir / "fema_levee_display.geojson"
            maps.add_hazard(m, load_fc(str(lv if lv.exists() else city.data_dir / "fema_levee.geojson")),
                            "FEMA Zone X: reduced risk due to levee", "LEVEE", show=False)
        if scenario.kind == "regulatory":
            legend += [("fill", maps.HAZARD_STYLE["SFHA"], "FEMA 1% annual chance (SFHA)"),
                       ("fill", maps.HAZARD_STYLE["X500"], "FEMA 0.2% annual chance")]
        elif scen_key == UPLOAD_KEY:
            legend.append(("fill", maps.HAZARD_STYLE["upload"], "Uploaded hazard layer"))
        else:
            legend.append(("fill", maps.HAZARD_STYLE["modelled"], f"Modelled flood extent ({scenario.short})"))
    if is_freetown and scen_key in ("RP20", "RP100"):
        if "wards" in freetown_layers:
            wards = load_fc(str(data.FREETOWN_PROCESSED / "wards.geojson"))
            col = f"buildings_{scen_key}"
            top = max((f["properties"].get(col) or 0) for f in wards["features"]) or 1
            import folium
            folium.GeoJson(wards, name="Wards: modelled buildings exposed", show=False,
                           style_function=lambda f, col=col, top=top: {
                               "color": "#334155", "weight": 0.8, "fillColor": "#f97316",
                               "fillOpacity": 0.05 + 0.6 * ((f["properties"].get(col) or 0) / top)},
                           tooltip=folium.GeoJsonTooltip(fields=["ward", col, "aal_usd"], localize=True,
                                                         aliases=["Ward", "Buildings in extent",
                                                                  "Modelled avg. annual loss (USD)"])).add_to(m)
        if "buildings" in freetown_layers:
            b = data.freetown_buildings()
            maps.add_building_heat(m, b[b[f"in_{scen_key}"]])
        if "records" in freetown_layers:
            import folium
            folium.GeoJson(load_fc(str(data.FREETOWN_PROCESSED / "recorded_flood_areas.geojson")),
                           name="Recorded flash-flood areas (City Council)",
                           style_function=lambda f: {"color": "#c084fc", "weight": 2, "dashArray": "6 4", "fillOpacity": 0.05},
                           tooltip="Recorded flash-flood area (Freetown City Council)").add_to(m)
            fg = folium.FeatureGroup(name="Recorded flash-flood sites (City Council)")
            for f in load_fc(str(data.FREETOWN_PROCESSED / "recorded_flood_points.geojson"))["features"]:
                p = f["properties"]
                lon, lat = f["geometry"]["coordinates"][:2]
                d = p.get(f"dist_{scen_key}_m")
                nm = p.get("Location") or f"Unnamed site #{p.get('id')}"
                folium.CircleMarker([lat, lon], radius=6, weight=2, fill=True, fill_opacity=0.9,
                                    color="#4ade80" if (d is not None and d <= 100) else "#c084fc",
                                    tooltip=f"{nm} · {d:.0f} m from modelled extent",
                                    popup=f"<b>{nm}</b><br>Recorded flash-flood site<br>Distance to {scen_key} extent: {d:.0f} m"
                                    ).add_to(fg)
            fg.add_to(m)
            legend += [("ring", "#4ade80", "Recorded site ≤100 m from model"),
                       ("ring", "#c084fc", "Recorded site >100 m (possible blind spot)")]
    if show_roads and roads(slug):
        maps.add_roads(m, roads(slug), rinfo if rinfo.get("assessed") else None)
        legend += [("line", "#94a3b8", "Major roads"), ("line", "#f97316", "Road section in hazard area")]
    if facilities_mode != "none" and not fx.empty:
        maps.add_facilities(m, fx, scenario.label if scenario else None, only_exposed=(facilities_mode == "exposed"))
        cats = sorted(fx["category"].unique()) if facilities_mode == "all" else sorted(fx[fx.get("exposed", False) == True]["category"].unique())  # noqa: E712
        legend += maps.facility_legend_items(cats[:8])
    maps.add_legend(m, legend)
    import folium
    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, use_container_width=True, height=height, returned_objects=[], key=f"{key}_{slug}")


def key_findings() -> list[str]:
    out = []
    if not scenario:
        out.append(f"No verified flood hazard dataset is loaded for {city.name}. PHOENIX does not estimate exposure "
                   "without one; see Data Sources for what is missing.")
        if not fx.empty:
            out.append(f"{len(fx):,} critical facilities from OpenStreetMap are mapped and ready for analysis.")
        return out
    s = fsum.set_index("Category") if not fsum.empty else pd.DataFrame()
    health = [c for c in ("Hospital", "Clinic / health centre") if c in s.index]
    if health:
        out.append("<b>Health:</b> " + "; ".join(
            f"{int(s.loc[c, 'Exposed'])} of {int(s.loc[c, 'Total'])} {c.lower()}s" for c in health) + " are exposed.")
    if not fsum.empty:
        worst = fsum[fsum["Total"] >= 5].sort_values("Share exposed", ascending=False).head(1)
        if len(worst) and worst["Share exposed"].iloc[0] > 0:
            w = worst.iloc[0]
            out.append(f"<b>Highest share exposed:</b> {w['Category']} ({w['Share exposed']:.0%}, "
                       f"{int(w['Exposed'])} of {int(w['Total'])}).")
    if rinfo.get("assessed"):
        out.append(f"<b>Access:</b> {rinfo['exposed_km']} km of {rinfo['total_km']} km of major roads lie in the hazard "
                   f"area; {rinfo['bridges_exposed']} mapped bridge segments affected.")
    if ft_cur:
        out.append(f"<b>Buildings:</b> {ft_cur['buildings_in_extent']:,} modelled buildings "
                   f"({money(ft_cur['value_in_extent_usd'])} replacement value) are inside the {scenario.short} extent.")
        out.append(f"<b>Model blind spots:</b> {ft_cur['recorded_flood_points_beyond_250m']} of "
                   f"{ft_cur['recorded_flood_points_total']} City Council recorded flash-flood sites lie more than 250 m "
                   "from the modelled extent: start field checks there.")
    if hstats:
        water = (" City limits include open water, and FEMA zones over water (e.g. VE on lakes and bays) are counted."
                 if scenario.kind == "regulatory" else "")
        out.append(f"<b>Hazard footprint:</b> {hstats['hazard_km2']:.1f} km², {hstats['share']:.0%} of the study area.{water}")
    if slug == "new_orleans" and (city.data_dir / "fema_levee.geojson").exists():
        out.append("<b>Levee context:</b> much of New Orleans is FEMA Zone X 'reduced risk due to levee'. It is not in "
                   "the 1%/0.2% scenarios, but residual risk from levee failure or pump outage remains (Katrina, 2005).")
    return out


def scenario_comparison(buffer=None) -> pd.DataFrame:
    b = buffer_m if buffer is None else buffer
    rows = []
    for k in scen_opts:
        e = exposure(slug, k, b, upload_digest, assets_digest)
        row = {"Scenario": scen_lookup[k].label, "Exposed facilities": int(e["exposed"].sum())}
        for c in ("Hospital", "Clinic / health centre", "School", "Fire station", "Police"):
            row[c] = int(e.loc[e["category"] == c, "exposed"].sum())
        if roads(slug):
            row["Road km in hazard"] = road_exposure(slug, k, b, upload_digest).get("exposed_km")
        if is_freetown and k in ("RP20", "RP100"):
            row["Modelled buildings in extent"] = ft_summary["return_periods"][k]["buildings_in_extent"]
        rows.append(row)
    return pd.DataFrame(rows)


def dark_chart(ch):
    return ch.configure(background="transparent").configure_axis(
        labelColor="#cbd5e1", titleColor="#94a3b8", gridColor="#1e2a44", domainColor="#334155"
    ).configure_view(stroke=None).configure_legend(labelColor="#cbd5e1", titleColor="#94a3b8")


def exposure_bar(df: pd.DataFrame):
    if df.empty:
        return
    near_lbl = f"Within {buffer_m} m of edge"
    parts = [df[["Category", "Inside"]].rename(columns={"Inside": "n"}).assign(Status="Inside hazard area")]
    if buffer_m:
        parts.append(df[["Category", "Near edge"]].rename(columns={"Near edge": "n"}).assign(Status=near_lbl))
    parts.append(df.assign(n=df["Total"] - df["Exposed"])[["Category", "n"]].assign(Status="Not exposed"))
    long = pd.concat(parts)
    order = list(df.sort_values("Exposed", ascending=False)["Category"])
    domain = ["Inside hazard area"] + ([near_lbl] if buffer_m else []) + ["Not exposed"]
    rng = ["#f97316"] + (["#fbbf24"] if buffer_m else []) + ["#334155"]
    ch = alt.Chart(long).mark_bar().encode(
        y=alt.Y("Category:N", sort=order, title=None, axis=alt.Axis(labelLimit=220, labelOverlap=False)),
        x=alt.X("n:Q", title="Facilities", stack="zero"),
        color=alt.Color("Status:N", scale=alt.Scale(domain=domain, range=rng),
                        legend=alt.Legend(orient="bottom", title=None, columns=3, labelLimit=200)),
        order=alt.Order("Status:N", sort="ascending"),
        tooltip=["Category", "Status", "n"]).properties(height=36 * df.shape[0] + 20)
    st.altair_chart(dark_chart(ch), width="stretch")


def facts_for_assistant() -> dict:
    extra = {}
    if ft_cur:
        extra["buildings"] = {"in_extent": ft_cur["buildings_in_extent"], "value_usd": ft_cur["value_in_extent_usd"],
                              "note": "Modelled building locations (World Bank exposure model), not surveyed."}
        wt = data.freetown_wards()
        wt = wt[wt.ward != "Outside ward boundaries"].sort_values(f"buildings_{scen_key}", ascending=False).head(10)
        extra["priority_wards"] = [{"ward": r.ward, "buildings": int(getattr(r, f"buildings_{scen_key}")),
                                    "share": getattr(r, f"buildings_{scen_key}") / r.buildings if r.buildings else 0}
                                   for r in wt.itertuples()]
        extra["model_gap"] = (f"{ft_cur['recorded_flood_points_beyond_250m']} of {ft_cur['recorded_flood_points_total']} "
                              "recorded flash-flood sites are >250 m from the modelled extent.")
    if scenario and len(scen_opts) > 1:
        cmp_ = scenario_comparison()
        extra["scenario_comparison"] = [{"scenario": r["Scenario"], "exposed": r["Exposed facilities"],
                                         "buildings": r.get("Modelled buildings in extent")} for _, r in cmp_.iterrows()]
    if scenario and not fx.empty and fx["category"].isin(["Hospital", "Clinic / health centre"]).any():
        extra["service_gap_health"] = service_gap(slug, scen_key, buffer_m, ("Hospital", "Clinic / health centre"),
                                                  upload_digest, assets_digest)
    if st.session_state.get(f"live_lines_{slug}"):
        extra["live"] = st.session_state[f"live_lines_{slug}"]
    return assistant.build_fact_sheet(city, scenario, buffer_m, fsum, fx, rinfo, extra)


# =============================================================== sections
def page_overview():
    kpis = [("Facilities analysed", f"{len(fx):,}", "OpenStreetMap critical facilities", "cyan")]
    if scenario:
        kpis.append(("Facilities exposed", f"{n_exposed:,}",
                     f"inside hazard area{'' if not buffer_m else f' or within {buffer_m} m'}", "alert"))
    else:
        kpis.append(("Facilities exposed", "Not assessed", "no verified hazard data", "alert"))
    if hstats:
        kpis.append(("Hazard area", f"{hstats['hazard_km2']:.1f} km²", f"{hstats['share']:.0%} of study area", "accent"))
    if ft_cur:
        kpis.append(("Modelled buildings in extent", f"{ft_cur['buildings_in_extent']:,}",
                     money(ft_cur["value_in_extent_usd"]) + " replacement value", "alert"))
    if rinfo.get("assessed"):
        kpis.append(("Major roads in hazard", f"{rinfo['exposed_km']} km", f"of {rinfo['total_km']} km mapped", "accent"))
    st.markdown(theme.cards(kpis), unsafe_allow_html=True)

    left, right = st.columns([1.55, 1])
    with left:
        st.markdown("### Map preview")
        render_map(height=470, facilities_mode="exposed" if scenario else "all",
                   freetown_layers=("records",), key="ov_map")
        st.caption("Showing exposed facilities only. Full layers in *Flood Risk Analysis* and *Infrastructure Exposure*.")
    with right:
        st.markdown("### Key findings")
        for f in key_findings():
            st.markdown(f'<div class="px-find">{f}</div>', unsafe_allow_html=True)
        st.markdown("### City profile")
        st.markdown(f"{city.context}<br><span class='px-muted'>{city.population_note}</span>", unsafe_allow_html=True)
        st.markdown(f"<span class='px-muted'>Study area: {theme.esc(city.study_area_note)}</span>", unsafe_allow_html=True)

    st.markdown("### Data status")
    rows = [{"Dataset": d.name, "Role": d.role.title(), "Status": "Available" if d.available else "Missing",
             "Source": d.source, "Date": d.date} for d in city.datasets]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.markdown(theme.note("<b>Limitations.</b> " + theme.esc(city.hazard_caveat) +
                           " Exposure is a screening result (location relative to a hazard area), not a damage "
                           "estimate. OpenStreetMap completeness varies.", warn=True), unsafe_allow_html=True)


def page_live():
    st.markdown(theme.note("<b>FORECAST / ALERT DATA.</b> Everything on this page is fetched live from public APIs and "
                           "is model output or official alerts. It is shown separately from the historical and "
                           "probabilistic hazard scenarios used elsewhere in PHOENIX."), unsafe_allow_html=True)
    if st.button("Refresh live data"):
        live_precip.clear(), live_river.clear(), live_alerts.clear()
    lat, lon = city.center
    lines = []
    pr = live_precip(lat, lon)
    c1, c2 = st.columns([1.3, 1])
    with c1:
        st.markdown(f"### Rainfall forecast · {city.name}")
        if not pr["ok"]:
            st.warning("Forecast unavailable: Open-Meteo could not be reached (check the internet connection).")
            st.caption(pr["error"][:200])
        else:
            d = pr["data"]
            daily = pd.DataFrame({"date": pd.to_datetime(d["daily"]["time"]),
                                  "mm": d["daily"]["precipitation_sum"],
                                  "prob": d["daily"].get("precipitation_probability_max")})
            hourly = pd.DataFrame({"time": pd.to_datetime(d["hourly"]["time"]),
                                   "mm": d["hourly"]["precipitation"]}).head(72)
            cur = d.get("current", {})
            next24 = float(np.nansum(hourly["mm"].head(24)))
            next72 = float(np.nansum(hourly["mm"]))
            wk = float(np.nansum(daily["mm"]))
            st.markdown(theme.cards([
                ("Now", f"{cur.get('precipitation', 0):.1f} mm/h", f"{cur.get('temperature_2m', '–')} °C", "cyan"),
                ("Next 24 h", f"{next24:.0f} mm", "forecast total", "accent"),
                ("Next 72 h", f"{next72:.0f} mm", "forecast total", "accent"),
                ("Next 7 days", f"{wk:.0f} mm", f"max daily {np.nanmax(daily['mm']):.0f} mm", "accent")]),
                unsafe_allow_html=True)
            ch = alt.Chart(daily).mark_bar(color="#3b82f6").encode(
                x=alt.X("date:T", title=None, axis=alt.Axis(format="%a %d")), y=alt.Y("mm:Q", title="mm / day"),
                tooltip=[alt.Tooltip("date:T", format="%a %d %b"), "mm", alt.Tooltip("prob", title="max prob. %")]
            ).properties(height=210)
            st.altair_chart(dark_chart(ch), width="stretch")
            st.caption(f"Open-Meteo forecast (blend of national weather models) for {d['latitude']:.3f}, "
                       f"{d['longitude']:.3f}, timezone {d.get('timezone')}. Fetched {pr['fetched']} UTC.")
            lines.append(f"Rainfall forecast: {next24:.0f} mm next 24 h, {next72:.0f} mm next 72 h, {wk:.0f} mm next 7 days "
                         f"(Open-Meteo, {city.name} centre).")
    with c2:
        st.markdown("### River discharge forecast (GloFAS)")
        if city.river_point:
            rlat, rlon, rlabel = city.river_point
            rv = live_river(rlat, rlon)
            if not rv["ok"]:
                st.warning("River forecast unavailable: the GloFAS service could not be reached.")
                st.caption(rv["error"][:200])
            else:
                dd = rv["data"]["daily"]
                rdf = pd.DataFrame({"date": pd.to_datetime(dd["time"]), "Forecast / reanalysis": dd["river_discharge"],
                                    "Long-term median": dd.get("river_discharge_median")})
                if rdf["Forecast / reanalysis"].notna().sum() == 0:
                    st.info("No modelled discharge at this cell.")
                else:
                    today = pd.Timestamp.now().normalize()
                    now_v = rdf.loc[rdf["date"] <= today, "Forecast / reanalysis"].dropna()
                    med_v = rdf.loc[rdf["date"] <= today, "Long-term median"].dropna()
                    ratio = (now_v.iloc[-1] / med_v.iloc[-1]) if len(now_v) and len(med_v) and med_v.iloc[-1] else None
                    long = rdf.melt("date", var_name="Series", value_name="m³/s").dropna()
                    ch = alt.Chart(long).mark_line(strokeWidth=2).encode(
                        x=alt.X("date:T", title=None), y=alt.Y("m³/s:Q"),
                        color=alt.Color("Series:N", scale=alt.Scale(range=["#22d3ee", "#64748b"]),
                                        legend=alt.Legend(orient="bottom", title=None)),
                        tooltip=["date:T", "Series", "m³/s"])
                    rule = alt.Chart(pd.DataFrame({"d": [today]})).mark_rule(color="#f97316", strokeDash=[4, 3]).encode(x="d:T")
                    st.altair_chart(dark_chart((ch + rule).properties(height=230)), width="stretch")
                    st.caption(f"{rlabel}. Orange line = today; right of it is forecast. Copernicus GloFAS via "
                               "Open-Meteo, ~5 km grid. Fetched " + rv["fetched"] + " UTC.")
                    if ratio:
                        st.markdown(theme.chip(f"Today ≈ {ratio:.1f}× long-term median", "live"), unsafe_allow_html=True)
                        lines.append(f"GloFAS discharge at {rlabel}: about {ratio:.1f}× the long-term median today.")
        else:
            st.info(city.river_note or "No river forecast point configured for this city.")
    if city.nws_alerts:
        st.markdown("### Official alerts · US National Weather Service")
        al = live_alerts(lat, lon)
        if not al["ok"]:
            st.warning("NWS alerts unavailable: api.weather.gov could not be reached.")
            st.caption(al["error"][:200])
        elif not al["alerts"]:
            st.markdown(theme.chip("No active NWS alerts for the city centre", "ok"), unsafe_allow_html=True)
            st.caption(f"Checked {al['fetched']} UTC. No alert is not the same as no risk.")
            lines.append("No active NWS alerts at the city centre.")
        else:
            adf = pd.DataFrame(al["alerts"])
            adf["Flood-related"] = adf["event"].map(lambda e: "Yes" if live.is_flood_related(e) else "")
            st.dataframe(adf[["event", "severity", "urgency", "onset", "expires", "Flood-related", "headline"]],
                         width="stretch", hide_index=True)
            lines += [f"NWS alert: {a['event']} ({a['severity']}) until {a['expires']}" for a in al["alerts"][:5]]
    else:
        st.markdown("### Official alerts")
        st.info(f"No machine-readable official alert feed is integrated for {city.country}. Monitor the national "
                "disaster management and meteorological agencies directly.")
    if lines:
        st.session_state[f"live_lines_{slug}"] = {"fetched": pr.get("fetched", "?"), "lines": lines}


def page_flood():
    if not scenario:
        st.info(f"No verified flood hazard dataset is loaded for {city.name}. Required: a published flood hazard "
                "map (e.g. FEMA NFHL for US cities). See *Data Sources and Methodology*.")
        render_map(height=520, hazard=False, key="fl_map0")
        return
    st.markdown(theme.note(f"<b>{theme.esc(scenario.label)}</b> - {theme.esc(scenario.description)} "
                           f"<br><span class='px-muted'>{theme.esc(city.hazard_caveat)}</span>"), unsafe_allow_html=True)
    layers = ("wards", "buildings", "records") if is_freetown else ()
    c1, c2 = st.columns([1.6, 1])
    with c1:
        render_map(height=600, all_scenarios=True, facilities_mode="none", freetown_layers=layers, key="fl_map")
        st.caption("Use the layer control (top right) to switch scenarios" +
                   (", wards, building density and recorded flood sites." if is_freetown else
                    " and the levee-protected zone." if (city.data_dir / "fema_levee.geojson").exists() else "."))
    with c2:
        st.markdown("### Hazard footprint by scenario")
        rows = []
        for k in scen_opts:
            hs = hazard_stats(slug, k, upload_digest)
            if hs:
                rows.append({"Scenario": scen_lookup[k].short, "Area (km²)": hs["hazard_km2"],
                             "Share of study area": f"{hs['share']:.0%}"})
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        if scenario.kind == "regulatory":
            st.markdown("### FEMA zones in this study area")
            zt = zone_table(slug, "X500" if "X500" in scen_lookup else scen_key)
            st.dataframe(zt, width="stretch", hide_index=True)
            man = city.manifest.get("fema", {})
            if man.get("firm_panels"):
                st.caption("FIRM databases: " + ", ".join(f"{p['DFIRM_ID']} v{p['VERSION_ID']}" for p in man["firm_panels"])
                           + f". Retrieved {man.get('retrieved')}.")
            st.markdown(theme.note("<b>Zone guide.</b> A/AE/AH/AO: 1% annual-chance riverine or ponding flooding. "
                                   "VE: 1% coastal flooding with wave action. Shaded X: 0.2% annual chance. "
                                   "Floodway: channel that must stay clear to pass the base flood."),
                        unsafe_allow_html=True)
        if is_freetown and ft_cur:
            st.markdown("### Model vs. recorded flooding")
            st.markdown(f"Freetown City Council recorded **{ft_cur['recorded_flood_points_total']} flash-flood sites**. "
                        f"Only **{ft_cur['recorded_flood_points_within_100m']}** lie within 100 m of the {scenario.short} "
                        f"extent; **{ft_cur['recorded_flood_points_beyond_250m']}** are more than 250 m away "
                        f"(median {ft_cur['recorded_flood_points_median_distance_m']:.0f} m).")
            st.caption("Likely reasons: blocked drains and local flash flooding are not in the regional model; "
                       "record locations may be approximate. These sites are where field checks should start.")
    if is_freetown and ft_cur:
        st.markdown("### Wards ranked by modelled buildings inside the extent")
        wt = data.freetown_wards()
        wt = wt[wt.ward != "Outside ward boundaries"].sort_values(f"buildings_{scen_key}", ascending=False)
        st.dataframe(pd.DataFrame({
            "Ward": wt["ward"], "Area": wt["area"],
            "Buildings in extent": wt[f"buildings_{scen_key}"].astype(int),
            "Share of ward": (wt[f"buildings_{scen_key}"] / wt["buildings"]).fillna(0),
            "Value in extent (USD)": wt[f"value_{scen_key}_usd"],
            "Modelled avg. annual loss (USD)": wt["aal_usd"]}), width="stretch", hide_index=True, height=320,
            column_config={"Share of ward": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=1),
                           "Value in extent (USD)": st.column_config.NumberColumn(format="$%,.0f"),
                           "Modelled avg. annual loss (USD)": st.column_config.NumberColumn(format="$%,.0f")})
        occ = {"Res": "Residential", "Com": "Commercial", "Ind": "Industrial"}
        st.markdown("**Building use inside the extent:** " + " · ".join(
            f"{occ.get(k, k)} {v['buildings']:,} ({money(v['value_usd'])})" for k, v in ft_cur["by_occupancy"].items()))


def page_infra():
    if fx.empty:
        st.info("No infrastructure data is loaded for this city.")
        return
    if not scenario:
        st.info("Facilities are mapped, but exposure is not calculated without a verified hazard layer.")
    c1, c2 = st.columns([1.6, 1])
    with c1:
        render_map(height=620, facilities_mode="all", show_roads=True, key="inf_map")
    with c2:
        st.markdown("### Exposure by facility type")
        if scenario:
            exposure_bar(fsum)
            st.dataframe(fsum, width="stretch", hide_index=True, column_config={
                "Share exposed": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=1)})
        else:
            st.dataframe(fx["category"].value_counts().rename_axis("Category").reset_index(name="Mapped"),
                         width="stretch", hide_index=True)
        st.caption(f"Source: OpenStreetMap ({city.manifest.get('facilities', {}).get('osm_timestamp', 'date unknown')[:10]}). "
                   f"{city.manifest.get('facilities', {}).get('unnamed', 0)} facilities have no name in OSM and are "
                   "labelled 'Unnamed'.")
    if scenario:
        st.markdown("### Exposed facilities")
        ex = fx[fx["exposed"]].sort_values(["dist_to_hazard_m", "category"])
        cats = st.multiselect("Filter by type", sorted(ex["category"].unique()), key=f"cat_{slug}")
        if cats:
            ex = ex[ex["category"].isin(cats)]
        show = ex[["display_name", "category", "status", "dist_to_hazard_m", "lat", "lon", "source", "osm_id"]].rename(
            columns={"display_name": "Facility", "category": "Type", "status": "Status",
                     "dist_to_hazard_m": "Distance to hazard (m)", "osm_id": "OSM id"})
        st.dataframe(show, width="stretch", hide_index=True, height=300)
        st.download_button("Download exposed facilities (CSV)", show.to_csv(index=False).encode("utf-8"),
                           file_name=f"phoenix_{slug}_{scen_key}_exposed_facilities.csv", mime="text/csv")
        if rinfo.get("assessed"):
            st.markdown("### Road network and bridges")
            r1, r2 = st.columns([1, 1])
            with r1:
                st.dataframe(rinfo["by_class"].rename(columns={"class": "Road class", "total_km": "Total km",
                                                               "exposed_km": "km in hazard area"}),
                             width="stretch", hide_index=True, column_config={
                                 "Share exposed": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=1)})
                st.caption(f"Mapped bridge segments in hazard area: {rinfo['bridges_exposed']} of {rinfo['bridges_total']}.")
            with r2:
                tn = rinfo["top_named_km"]
                if len(tn):
                    st.dataframe(tn.rename_axis("Most affected named roads").reset_index(name="km in hazard area"),
                                 width="stretch", hide_index=True)
            st.caption("Being inside a flood zone does not mean a road is impassable; depth, drainage and elevation "
                       "(e.g. raised highways) are not modelled.")
        elif scenario and not roads(slug):
            st.info("Road network data is not loaded for this city yet.")
        if ft_cur:
            st.markdown("### Buildings (World Bank modelled exposure)")
            st.markdown(theme.cards([
                ("Buildings in extent", f"{ft_cur['buildings_in_extent']:,}", f"of {ft_summary['buildings_total']:,} modelled", "alert"),
                ("Replacement value", money(ft_cur["value_in_extent_usd"]), "inside extent (not a loss)", "accent"),
                ("Residential share", f"{ft_cur['by_occupancy']['Res']['buildings'] / ft_cur['buildings_in_extent']:.0%}",
                 "of exposed buildings", "accent")]), unsafe_allow_html=True)


def page_whatif():
    if not scenario:
        st.info("The simulator needs a verified hazard layer, which is not available for this city.")
    else:
        st.markdown(theme.note("Every result here is <b>computed</b> from the loaded hazard scenario and infrastructure "
                               "data. Distances are straight-line, not travel time."), unsafe_allow_html=True)
        t1, t2, t3 = st.tabs(["Compare scenarios", "Hazard-edge sensitivity", "Service disruption"])
        with t1:
            st.markdown("**What if the flood were larger?** Exposure under every available scenario "
                        f"(screening distance {'inside only' if not buffer_m else f'+{buffer_m} m'}).")
            st.dataframe(scenario_comparison(), width="stretch", hide_index=True)
        with t2:
            st.markdown(f"**What if the hazard edge is uncertain?** Exposed facilities under *{scenario.label}* as the "
                        "screening distance grows.")
            rows = []
            for b in BUFFERS:
                e = exposure(slug, scen_key, b, upload_digest, assets_digest)
                for c, g in e.groupby("category"):
                    rows.append({"Screening distance (m)": b, "Category": c, "Exposed": int(g["exposed"].sum())})
            sdf = pd.DataFrame(rows)
            if sdf.empty:
                st.info("No infrastructure is loaded for this city, so sensitivity cannot be computed.")
                return
            ch = alt.Chart(sdf).mark_line(point=True).encode(
                x=alt.X("Screening distance (m):Q"), y=alt.Y("Exposed:Q", title="Exposed facilities"),
                color=alt.Color("Category:N", legend=alt.Legend(orient="right", title=None)),
                tooltip=["Category", "Screening distance (m)", "Exposed"]).properties(height=320)
            st.altair_chart(dark_chart(ch), width="stretch")
            st.caption("Steep lines mean many facilities sit just outside the mapped hazard edge: a small error in the "
                       "map could change their status.")
        with t3:
            st.markdown("**What if exposed facilities stop operating?** Exposed facilities of the chosen type are "
                        "treated as out of service; PHOENIX measures how far every part of the study area is from the "
                        "nearest one still working.")
            services = {"Health (hospitals + clinics)": ("Hospital", "Clinic / health centre"),
                        "Hospitals only": ("Hospital",), "Fire stations": ("Fire station",), "Police": ("Police",),
                        "Schools (as shelters)": ("School",)}
            avail = {k: v for k, v in services.items() if fx["category"].isin(v).any()}
            if not avail:
                st.info("No facilities of these types are mapped.")
            else:
                choice = st.radio("Service", list(avail), horizontal=True, key=f"svc_{slug}")
                g = service_gap(slug, scen_key, buffer_m, avail[choice], upload_digest, assets_digest)
                if not g.get("assessed"):
                    st.info(g.get("reason", "Not assessed."))
                elif g.get("all_lost"):
                    st.error(f"All {g['facilities_total']} facilities of this type are exposed in this scenario.")
                else:
                    st.markdown(theme.cards([
                        ("Out of service", f"{g['facilities_lost']} of {g['facilities_total']}", "exposed facilities", "alert"),
                        ("Median distance before", f"{g['median_before_m'] / 1000:.2f} km", "to nearest facility", "accent"),
                        ("Median distance after", f"{g['median_after_m'] / 1000:.2f} km",
                         f"90th percentile {g['p90_after_m'] / 1000:.2f} km", "alert"),
                        ("Area served further away", f"{g['share_further']:.0%}", f"of {g['n_demand']:,} sample points", "cyan"),
                    ]), unsafe_allow_html=True)
                    st.caption("Sample points: a regular 500 m grid over the study area (a neutral stand-in for where "
                               "people are; population data is not loaded). Straight-line distance, not road travel time.")
    with st.expander("Teaching scenario: rainfall slider (ILLUSTRATIVE ONLY)", expanded=not scenario):
        st.warning("ILLUSTRATIVE ONLY. Fictional assets and a hand-made score that show how a rainfall-driven tool could "
                   "feel. Not calibrated, not a forecast, and not a probability of flooding.")
        tc1, tc2 = st.columns(2)
        rainfall = tc1.slider("Scenario rainfall (mm / 6 hours)", 10, 180, 100, 10, key="teach_rain")
        antecedent = tc2.selectbox("Ground condition", ["Normal", "Already wet", "Saturated"], key="teach_ground")
        demo = pd.DataFrame([
            {"Asset": "Illustrative health facility A", "Type": "Health facility", "exposure_baseline": 0.78},
            {"Asset": "Illustrative school B", "Type": "School", "exposure_baseline": 0.55},
            {"Asset": "Illustrative road segment C", "Type": "Road", "exposure_baseline": 0.67},
            {"Asset": "Illustrative community site D", "Type": "Community site", "exposure_baseline": 0.38}])
        gf = {"Normal": 1.0, "Already wet": 1.18, "Saturated": 1.35}[antecedent]
        demo["Illustrative score"] = (demo["exposure_baseline"] * (rainfall / 180.0) * gf).clip(0, 1)
        demo["Band"] = pd.cut(demo["Illustrative score"], bins=[-0.001, 0.30, 0.60, 1.0],
                              labels=["Lower", "Elevated", "High"]).astype(str)
        st.dataframe(demo[["Asset", "Type", "Illustrative score", "Band"]], width="stretch", hide_index=True)


def page_assistant():
    facts = facts_for_assistant()
    mode = "Claude (grounded on the fact sheet)" if assistant.claude_available() else "Built-in grounded analyst"
    st.markdown(theme.note(f"<b>Mode: {mode}.</b> Answers come only from the analysis currently on screen "
                           f"({theme.esc(city.name)}, {theme.esc(scenario.label if scenario else 'no scenario')}). "
                           "Set the <code>ANTHROPIC_API_KEY</code> environment variable to enable Claude for free-form "
                           "questions; without it the built-in analyst answers common questions deterministically."),
                unsafe_allow_html=True)
    hist_key = f"chat_{slug}"
    hist = st.session_state.setdefault(hist_key, [])
    suggestions = ["Give me a situation brief", "Which hospitals and clinics are exposed?",
                   "How much of the road network is affected?", "Where should we look first?",
                   "Compare the scenarios", "What data is this based on and what is missing?"]
    cols = st.columns(3)
    clicked = None
    for i, s in enumerate(suggestions):
        if cols[i % 3].button(s, key=f"sugg_{i}", width="stretch"):
            clicked = s
    for m in hist:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
    q = st.chat_input("Ask about exposure, priorities, data or the forecast…") or clicked
    if q:
        hist.append({"role": "user", "content": q})
        with st.chat_message("user"):
            st.markdown(q)
        ans, used = assistant.answer(q, facts, hist[:-1])
        hist.append({"role": "assistant", "content": ans})
        with st.chat_message("assistant"):
            st.markdown(ans)
            st.caption(used)
    c1, c2 = st.columns([1, 4])
    if c1.button("Clear conversation"):
        st.session_state[hist_key] = []
        st.rerun()
    with st.expander("Fact sheet used by the assistant (audit trail)"):
        st.json(json.loads(json.dumps(facts, default=str)))


def page_methods():
    st.markdown(f"### Datasets for {city.name}")
    rows = [{"Status": "✅ Available" if d.available else "❌ Missing", "Dataset": d.name, "Role": d.role,
             "Source": d.source, "Coverage": d.coverage, "Date": d.date, "License": d.license,
             "Limitations": d.limitations, "Link": d.url} for d in city.datasets]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 column_config={"Link": st.column_config.LinkColumn(display_text="source")})
    st.markdown("### Not available for this city")
    for m in city.missing:
        st.markdown(f"- {m}")
    man = city.manifest
    if man:
        with st.expander("Processing manifest (counts, retrieval dates, FIRM panels)"):
            st.json(man)
    st.markdown("### Coverage across cities")
    cov = []
    for c in CITIES.values():
        cov.append({"City": f"{c.name}, {c.country}", "Hazard scenarios": len(c.available_scenarios),
                    "Facilities": "✅" if c.facilities_file.exists() else "❌",
                    "Roads": "✅" if c.roads_file.exists() else "❌",
                    "Building exposure": "✅" if c.slug == "freetown" else "❌",
                    "Live alerts": "NWS" if c.nws_alerts else "–", "Status": c.analysis_status})
    st.dataframe(pd.DataFrame(cov), width="stretch", hide_index=True)
    st.markdown("""
### Method
1. **Hazard.** Freetown: World Bank / JBA probabilistic flood extents (1-in-20 and 1-in-100 year, undefended,
   combined river and surface water). US cities: FEMA National Flood Hazard Layer zones, dissolved into the 1%
   annual-chance Special Flood Hazard Area and the 1% + 0.2% area, clipped to the study area.
2. **Infrastructure.** Hospitals, clinics, schools, colleges, fire stations, police, shelters and assembly points,
   and motorway-to-secondary roads from OpenStreetMap (HOT Raw Data API / Overpass extracts). Polygons are reduced to their centre point.
   Names are never invented: unnamed features are labelled *Unnamed* with their OSM id.
3. **Coordinate handling.** All data is stored in WGS84 (EPSG:4326). Distances, areas and lengths are computed in a
   local metric projection centred on each city (error well under 1% at city scale). Uploaded layers that are not
   in longitude/latitude are rejected rather than drawn in the wrong place.
4. **Exposure.** A facility is *exposed* if it is inside the hazard area, or within the screening distance chosen
   in the sidebar. Road exposure is the length of road inside the (buffered) hazard area. Freetown building exposure
   comes from a full-resolution overlay of the World Bank exposure model (`prepare_data.py`).
5. **Service disruption.** Exposed facilities are treated as out of service and straight-line distance from a 500 m
   grid of sample points to the nearest working facility is compared before and after.
6. **Live data.** Open-Meteo precipitation forecast, Copernicus GloFAS river discharge (via Open-Meteo) and US NWS
   alerts are fetched on demand and are never merged with the scenario layers.

### What PHOENIX does not do
- It does not forecast floods. Hazard layers are probabilistic (Freetown) or regulatory (FEMA) scenarios.
- *Exposed* is not *damaged*: flood depth, building vulnerability and flood defences are not modelled
  (except that FEMA's map already reflects accredited levees).
- OpenStreetMap completeness differs between cities, which affects cross-city comparisons.

### Attribution
- World Bank, *Freetown, Sierra Leone Flood Risk Assessment* (JBA Risk Management, GEM Foundation), CC BY 4.0.
- FEMA National Flood Hazard Layer (via the Esri Living Atlas *USA Flood Hazard Areas* mirror of NFHL), public domain.
- US Census Bureau TIGERweb place boundaries, public domain.
- © OpenStreetMap contributors, ODbL 1.0.
- Open-Meteo (CC BY 4.0); Copernicus Emergency Management Service GloFAS; US National Weather Service.
- Basemaps: © OpenStreetMap contributors; Esri World Imagery and Dark Gray Canvas.

### Refreshing the data
`python scripts/fetch_city_data.py --out data/raw` downloads FEMA, TIGERweb and OSM data for the US cities and OSM
for Freetown; `python scripts/build_city_data.py --raw data/raw` rebuilds the app files.
""")


def page_alerts():
    from phoenix_core import alerts_ui  # imported lazily so the rest of PHOENIX never depends on it
    alerts_ui.render(slug)


PAGES = dict(zip(SECTIONS, [page_overview, page_live, page_flood, page_infra, page_whatif, page_assistant, page_alerts,
                            page_methods]))
PAGES[section]()
st.markdown("<div class='px-muted' style='margin-top:2rem;text-align:center'>PHOENIX · BAU Innovate 2026 · "
            "Hackathon prototype · decision support, not an official warning service</div>", unsafe_allow_html=True)
