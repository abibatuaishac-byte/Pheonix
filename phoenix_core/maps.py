"""Folium map construction shared by every section and city."""
from __future__ import annotations

import html

import folium
import pandas as pd
from branca.element import MacroElement, Template
from folium.plugins import HeatMap
from shapely.geometry import mapping

# Category styling: colour + letter badge (distinct shape/letter, not colour alone)
CATEGORY_STYLE = {
    "Hospital": ("#ef4444", "H"),
    "Clinic / health centre": ("#f472b6", "C"),
    "School": ("#facc15", "S"),
    "College / university": ("#a3e635", "U"),
    "Fire station": ("#fb923c", "F"),
    "Police": ("#60a5fa", "P"),
    "Emergency shelter": ("#34d399", "Sh"),
    "Emergency assembly point": ("#2dd4bf", "A"),
}
DEFAULT_STYLE = ("#c084fc", "•")

HAZARD_STYLE = {  # fill colours: blues for hazard, never used for infrastructure
    "SFHA": "#2563eb", "X500": "#38bdf8", "LEVEE": "#64748b", "modelled": "#3b82f6", "upload": "#ef4444",
}

BASEMAPS = [
    ("Street map (OpenStreetMap)", "OpenStreetMap", None),
    ("Satellite imagery (Esri)",
     "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
     "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"),
    ("Dark canvas (Esri)",
     "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
     "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors"),
]


def base_map(center, zoom, basemap: str = "Street map (OpenStreetMap)") -> folium.Map:
    """Keyless basemaps only (CARTO now needs an API key and shows 'API KEY REQUIRED')."""
    m = folium.Map(location=list(center), zoom_start=zoom, tiles=None, control_scale=True, prefer_canvas=True)
    for name, tiles, attr in BASEMAPS:
        show = name == basemap  # only the chosen basemap is switched on; the others stay in the layer control
        if attr is None:
            folium.TileLayer(tiles, name=name, show=show).add_to(m)
        else:
            folium.TileLayer(tiles=tiles, attr=attr, name=name, show=show,
                             max_native_zoom=16 if "Canvas" in tiles else 18, max_zoom=19).add_to(m)
    return m


def add_boundary(m, boundary_fc: dict | None, name="Study area boundary"):
    if not boundary_fc:
        return
    folium.GeoJson(boundary_fc, name=name, style_function=lambda f: {
        "color": "#22d3ee", "weight": 2, "dashArray": "6 4", "fillOpacity": 0}, tooltip=name).add_to(m)


def add_hazard(m, fc: dict, name: str, kind: str, show=True):
    def style(f):
        grp = (f.get("properties") or {}).get("group")
        col = HAZARD_STYLE.get(grp) or HAZARD_STYLE.get(kind, "#3b82f6")
        return {"color": col, "weight": 0.6, "fillColor": col, "fillOpacity": 0.42 if grp != "X500" else 0.30}
    props = (fc.get("features") or [{}])[0].get("properties") or {}
    tooltip = folium.GeoJsonTooltip(fields=["label"], aliases=["Flood zone"]) if "label" in props else name
    folium.GeoJson(fc, name=name, style_function=style, tooltip=tooltip, show=show).add_to(m)


def _badge(color, letter, exposed):
    ring = "#ff2d2d" if exposed else "#0b1220"
    w = 3 if exposed else 1.5
    return folium.DivIcon(icon_size=(22, 22), icon_anchor=(11, 11), html=(
        f'<div style="width:20px;height:20px;border-radius:50%;background:{color};border:{w}px solid {ring};'
        f'display:flex;align-items:center;justify-content:center;font:700 10px/1 Inter,Arial,sans-serif;'
        f'color:#0b1220;box-shadow:0 0 4px rgba(0,0,0,.6)">{html.escape(letter)}</div>'))


def add_facilities(m, fx: pd.DataFrame, scenario_label: str | None, only_exposed=False, prefix=""):
    """One toggleable layer per category; exposed facilities get a red ring."""
    if fx.empty:
        return
    df = fx[fx["exposed"]] if (only_exposed and "exposed" in fx) else fx
    for cat, grp in df.groupby("category"):
        color, letter = CATEGORY_STYLE.get(cat, DEFAULT_STYLE)
        n_exp = int(grp["exposed"].sum()) if "exposed" in grp else 0
        fg = folium.FeatureGroup(name=f"{prefix}{cat} ({len(grp)}, {n_exp} exposed)")
        for r in grp.itertuples():
            exposed = bool(getattr(r, "exposed", False))
            d = getattr(r, "dist_to_hazard_m", float("nan"))
            status = getattr(r, "status", "Not assessed")
            dist_txt = "inside" if d == 0 else (f"{d:,.0f} m from hazard edge" if pd.notna(d) else "not assessed")
            name = html.escape(str(r.display_name))
            popup = (f"<b>{name}</b><br>{html.escape(cat)}<br>"
                     f"<b>{html.escape(status)}</b> ({dist_txt})"
                     + (f"<br>Scenario: {html.escape(scenario_label)}" if scenario_label else "")
                     + f"<br><span style='color:#64748b'>Source: {html.escape(str(r.source))}"
                     + (f" · {html.escape(str(r.osm_id))}" if isinstance(r.osm_id, str) else "") + "</span>")
            folium.Marker([r.lat, r.lon], icon=_badge(color, letter, exposed),
                          tooltip=f"{r.display_name} · {status}",
                          popup=folium.Popup(popup, max_width=300)).add_to(fg)
        fg.add_to(m)


def add_roads(m, roads: list[dict], road_info: dict | None):
    if not roads:
        return
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": mapping(r["geometry"]),
         "properties": {"name": r.get("name") or "Unnamed road", "class": r["class"]}} for r in roads]}
    folium.GeoJson(fc, name="Major roads", style_function=lambda f: {"color": "#94a3b8", "weight": 1.6, "opacity": 0.75},
                   tooltip=folium.GeoJsonTooltip(fields=["name", "class"], aliases=["Road", "Class"])).add_to(m)
    if road_info and road_info.get("exposed_geoms"):
        efc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": mapping(e["geometry"]),
             "properties": {"name": e["name"], "class": e["class"], "km": e["km"],
                            "bridge": "Bridge" if e["bridge"] else ""}} for e in road_info["exposed_geoms"]]}
        folium.GeoJson(efc, name="Road sections in hazard area",
                       style_function=lambda f: {"color": "#f97316", "weight": 4, "opacity": 0.95},
                       tooltip=folium.GeoJsonTooltip(fields=["name", "class", "km", "bridge"],
                                                     aliases=["Road", "Class", "km in hazard area", ""])).add_to(m)


def add_building_heat(m, b: pd.DataFrame, name="Modelled buildings inside extent (density)"):
    if b.empty:
        return
    HeatMap(b[["lat", "lon", "n"]].values.tolist(), name=name, radius=9, blur=12, min_opacity=0.35).add_to(m)


def add_legend(m, items: list[tuple[str, str, str]], title="Legend"):
    """items: (kind, colour, label) where kind in {'fill','badge','line','ring'}."""
    rows = []
    for kind, col, label in items:
        if kind == "fill":
            sw = f'<span style="display:inline-block;width:14px;height:10px;background:{col};opacity:.7;margin-right:6px"></span>'
        elif kind == "line":
            sw = f'<span style="display:inline-block;width:16px;height:3px;background:{col};margin:0 6px 3px 0"></span>'
        elif kind == "ring":
            sw = f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;border:3px solid {col};margin-right:6px"></span>'
        else:
            col, letter = col.split("|")
            sw = (f'<span style="display:inline-flex;width:14px;height:14px;border-radius:50%;background:{col};'
                  f'color:#0b1220;font:700 8px Arial;align-items:center;justify-content:center;margin-right:6px">{letter}</span>')
        rows.append(f"<div style='margin:2px 0'>{sw}{html.escape(label)}</div>")
    tpl = Template("""{% macro html(this, kwargs) %}
    <div style="position:absolute;bottom:34px;left:12px;z-index:9999;background:rgba(11,18,32,.92);color:#e5e7eb;
         padding:8px 10px;border-radius:8px;border:1px solid #1e2a44;font:12px Inter,Arial,sans-serif;max-width:260px">
      <div style="font-weight:600;margin-bottom:4px;color:#67e8f9">""" + html.escape(title) + """</div>
      """ + "".join(rows) + """
    </div>{% endmacro %}""")
    el = MacroElement()
    el._template = tpl
    m.get_root().add_child(el)


def facility_legend_items(categories) -> list:
    items = [("badge", f"{CATEGORY_STYLE.get(c, DEFAULT_STYLE)[0]}|{CATEGORY_STYLE.get(c, DEFAULT_STYLE)[1]}", c)
             for c in categories]
    items.append(("ring", "#ff2d2d", "Red ring = exposed in current scenario"))
    return items
