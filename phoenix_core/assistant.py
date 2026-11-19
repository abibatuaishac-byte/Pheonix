"""PHOENIX AI Intelligence Assistant.

Two modes, chosen automatically:

1. Grounded analyst (always available, no API key): answers from a *fact sheet*
   built from the exposure analysis currently on screen.  Every number it
   states is read from that fact sheet, so it cannot invent statistics.
2. Claude (optional): if ANTHROPIC_API_KEY is set, the same fact sheet is sent
   to Claude with instructions to answer only from it and to flag gaps.

The fact sheet is also shown to the user, so every answer can be audited.
"""
from __future__ import annotations

import json
import os
import re

import requests

ANTHROPIC_MODEL = os.environ.get("PHOENIX_CLAUDE_MODEL", "claude-sonnet-5-5")


def fmt_int(v) -> str:
    return f"{int(v):,}"


def build_fact_sheet(city, scenario, buffer_m, fac_summary, fx, road_info, extra: dict) -> dict:
    """Collect everything the assistant may say, as plain data."""
    exposed = fx[fx.get("exposed", False) == True] if "exposed" in fx else fx.iloc[0:0]  # noqa: E712
    top = (exposed.sort_values("dist_to_hazard_m")
           [["display_name", "category", "status", "dist_to_hazard_m"]].head(25).to_dict("records")
           if len(exposed) else [])
    facts = {
        "city": f"{city.name}, {city.country}",
        "study_area": city.study_area_note,
        "hazard": "Flooding",
        "scenario": {"label": scenario.label, "type": scenario.kind,
                     "description": scenario.description} if scenario else None,
        "hazard_caveat": city.hazard_caveat,
        "exposure_definition": ("A facility is 'exposed' if it lies inside the hazard area"
                                + (f" or within {int(buffer_m)} m of it" if buffer_m else "") + "."),
        "facilities_by_category": fac_summary.to_dict("records") if fac_summary is not None else [],
        "facilities_total": int(len(fx)),
        "facilities_exposed": int(len(exposed)),
        "exposed_facilities_sample": top,
        "roads": ({k: (v.to_dict("records") if hasattr(v, "to_dict") and k == "by_class" else
                       v.to_dict() if hasattr(v, "to_dict") else v)
                   for k, v in road_info.items()} if road_info and road_info.get("assessed") else None),
        "missing_data": city.missing,
        "datasets": [{"name": d.name, "source": d.source, "date": d.date, "available": d.available}
                     for d in city.datasets],
    }
    facts.update(extra or {})
    return facts


# ---------------------------------------------------------------- grounded analyst
def _pct(x) -> str:
    return f"{x:.0%}"


def _cat_lines(facts, keywords) -> list[str]:
    rows = [r for r in facts["facilities_by_category"] if any(k in r["Category"].lower() for k in keywords)]
    return [f"- **{r['Category']}**: {fmt_int(r['Exposed'])} of {fmt_int(r['Total'])} exposed "
            f"({_pct(r['Share exposed'])}); {fmt_int(r['Inside'])} inside the hazard area." for r in rows]


def _named(facts, keywords, limit=8) -> list[str]:
    out = []
    for r in facts["exposed_facilities_sample"]:
        if any(k in r["category"].lower() for k in keywords):
            where = "inside" if r["dist_to_hazard_m"] <= 0 else f"{r['dist_to_hazard_m']:.0f} m from the edge"
            out.append(f"  - {r['display_name']} ({r['category']}, {where})")
        if len(out) >= limit:
            break
    return out


def _briefing(f) -> str:
    sc = f["scenario"]
    lines = [f"### Situation brief: {f['city']}",
             f"**Scenario:** {sc['label']} ({sc['type']} hazard information, not a forecast)." if sc else
             "**No verified hazard scenario is loaded for this city**, so no exposure can be calculated.",
             f"**Study area:** {f['study_area']}.", ""]
    if sc:
        lines.append(f"Of **{fmt_int(f['facilities_total'])}** mapped critical facilities, "
                     f"**{fmt_int(f['facilities_exposed'])}** are exposed. {f['exposure_definition']}")
        lines += _cat_lines(f, ["hospital", "clinic", "school", "fire", "police", "shelter"])
        if f.get("roads"):
            r = f["roads"]
            lines.append(f"- **Major roads (at grade):** {r['exposed_km']} km of {r['total_km']} km of mapped road lies "
                         "in the hazard area (bridges and elevated sections excluded).")
        if f.get("buildings"):
            b = f["buildings"]
            lines.append(f"- **Buildings (modelled):** {fmt_int(b['in_extent'])} buildings worth about "
                         f"${b['value_usd'] / 1e6:,.0f}M are inside the extent.")
        if f.get("priority_wards"):
            lines.append("- **Wards with most exposed buildings:** " + ", ".join(
                f"{w['ward']} ({fmt_int(w['buildings'])})" for w in f["priority_wards"][:5]) + ".")
    lines += ["", f"_{f['hazard_caveat']}_"]
    return "\n".join(lines)


INTENTS = [
    ("health", ["hospital", "health", "clinic", "medical"]),
    ("schools", ["school", "education", "college", "universit", "student"]),
    ("emergency", ["fire", "police", "emergency service", "shelter", "responder"]),
    ("roads", ["road", "bridge", "access", "route", "evacuat", "transport"]),
    ("priority", ["priority", "ward", "where", "neighbourhood", "neighborhood", "area", "worst", "hotspot", "most"]),
    ("data", ["data", "source", "limitation", "reliab", "confiden", "accura", "trust", "missing", "method"]),
    ("live", ["forecast", "rain", "live", "now", "today", "alert", "warning", "current", "river"]),
    ("actions", ["recommend", "action", "should", "prepare", "plan", "do next", "priorit"]),
    ("compare", ["compare", "differen", "versus", " vs", "what if", "worse"]),
    ("brief", ["summary", "summar", "brief", "overview", "situation", "status", "exposed", "impact", "affected"]),
]


def classify(q: str) -> str:
    ql = q.lower()
    for name, words in INTENTS:
        if any(w in ql for w in words):
            return name
    return "help"


def grounded_answer(question: str, f: dict) -> str:
    intent = classify(question)
    sc = f["scenario"]
    if intent != "data" and intent != "help" and not sc:
        return ("No verified flood hazard dataset is loaded for this city yet, so I cannot say what would be "
                "affected. See *Data Sources and Methodology* for what is available and missing.")
    if intent == "brief":
        return _briefing(f)
    if intent == "health":
        lines = [f"**Health facilities under the {sc['label']}:**"] + _cat_lines(f, ["hospital", "clinic"])
        named = _named(f, ["hospital", "clinic"])
        if named:
            lines += ["", "Closest to or inside the hazard area:"] + named
        g = f.get("service_gap_health")
        if g and g.get("assessed") and g.get("facilities_lost"):
            lines.append(f"\nIndicative only: if the {g['facilities_lost']} exposed health facilities could not operate, "
                         f"about {g['share_further']:.0%} of the study area would be further (straight-line) from its "
                         "nearest working health facility. This is a screening estimate, not a prediction of service "
                         "interruption.")
        lines.append(f"\n_{f['exposure_definition']} OSM facility coverage may be incomplete._")
        return "\n".join(lines)
    if intent == "schools":
        lines = [f"**Education facilities under the {sc['label']}:**"] + _cat_lines(f, ["school", "college"])
        named = _named(f, ["school", "college"])
        return "\n".join(lines + (["", "Examples:"] + named if named else []))
    if intent == "emergency":
        lines = [f"**Emergency services under the {sc['label']}:**"] + _cat_lines(
            f, ["fire", "police", "shelter", "assembly"])
        named = _named(f, ["fire", "police", "shelter", "assembly"])
        return "\n".join(lines + (["", "Affected sites:"] + named if named else []))
    if intent == "roads":
        r = f.get("roads")
        if not r:
            return "Road network data is not loaded for this city, so access disruption was not assessed."
        lines = [f"**Major road network under the {sc['label']}:** {r['exposed_km']} km of {r['total_km']} km "
                 f"({(r['exposed_km'] / r['total_km'] if r['total_km'] else 0):.0%}) lies inside the hazard area."]
        for row in r["by_class"]:
            lines.append(f"- {row['class']}: {row['exposed_km']} of {row['total_km']} km")
        if r.get("top_named_km"):
            lines.append("\nMost affected named roads (km in hazard area): " + ", ".join(
                f"{k} ({v})" for k, v in list(r["top_named_km"].items())[:6]) + ".")
        lines.append(f"\n{r.get('bridge_segments_excluded', 0)} bridge / elevated segments are excluded because "
                     "their height above flood level is not known. _Being in the hazard area does not mean a road "
                     "would be closed; depth and drainage are not modelled. Divided roads count each direction._")
        return "\n".join(lines)
    if intent == "priority":
        if f.get("priority_wards"):
            lines = ["**Wards to investigate first** (ranked by modelled buildings inside the extent):"]
            for w in f["priority_wards"][:8]:
                lines.append(f"- {w['ward']}: {fmt_int(w['buildings'])} buildings ({w['share']:.0%} of the ward)")
            if f.get("model_gap"):
                lines.append(f"\nAlso: {f['model_gap']}")
            return "\n".join(lines)
        lines = ["**Exposed critical facilities to review first** (inside the hazard area first):"]
        lines += _named(f, [""], limit=12) or ["- none in the current scenario"]
        return "\n".join(lines)
    if intent == "data":
        lines = ["**Data behind this analysis:**"]
        for d in f["datasets"]:
            lines.append(f"- {'✅' if d['available'] else '❌'} {d['name']} - {d['source']} ({d['date']})")
        lines += ["", "**Not available / not modelled:**"] + [f"- {m}" for m in f["missing_data"]]
        lines.append(f"\n_{f['hazard_caveat']}_")
        return "\n".join(lines)
    if intent == "live":
        lv = f.get("live")
        if not lv:
            return ("No live data has been loaded in this session. Open **Live Intelligence** to fetch the current "
                    "forecast. Remember the hazard maps in PHOENIX are scenarios, not forecasts.")
        return "**Live feeds (forecasts, fetched " + lv.get("fetched", "?") + "):**\n" + "\n".join(
            f"- {x}" for x in lv.get("lines", [])) + "\n\n_Forecasts are model output; check official national services._"
    if intent == "compare":
        cmp_ = f.get("scenario_comparison")
        if not cmp_:
            return "Only one scenario is available for this city."
        lines = ["**Scenario comparison (exposed critical facilities):**"]
        for row in cmp_:
            lines.append(f"- {row['scenario']}: {fmt_int(row['exposed'])} facilities"
                         + (f", {fmt_int(row['buildings'])} modelled buildings" if row.get("buildings") is not None else ""))
        return "\n".join(lines)
    if intent == "actions":
        lines = [_briefing(f), "", "**Suggested next steps (decision support, not instructions):**"]
        cats = {r["Category"]: r for r in f["facilities_by_category"]}
        if any(k in cats and cats[k]["Exposed"] for k in ("Hospital", "Clinic / health centre")):
            lines.append("- Confirm with the exposed health facilities whether they have flood plans, backup power and "
                         "ground-floor critical equipment.")
        if any(k in cats and cats[k]["Exposed"] for k in ("School", "College / university")):
            lines.append("- Check whether exposed schools are designated as shelters; if so, reconsider their suitability.")
        if f.get("roads") and f["roads"]["exposed_km"]:
            lines.append("- Identify alternative access routes to facilities on the most affected named roads.")
        if f.get("model_gap"):
            lines.append("- Field-verify recorded flood sites that the model does not capture.")
        lines.append("- Validate OSM facility locations with the responsible agency before operational use.")
        return "\n".join(lines)
    return ("I can answer from the analysis on screen. Try:\n- *Give me a situation brief*\n- *Which hospitals are "
            "exposed?*\n- *How much of the road network is affected?*\n- *Where should we look first?*\n- *Compare "
            "the scenarios*\n- *What data is this based on and what is missing?*\n- *What does the forecast show?*")


# ---------------------------------------------------------------- optional Claude
SYSTEM = (
    "You are the PHOENIX disaster-intelligence assistant for emergency managers. Answer ONLY from the JSON fact "
    "sheet provided. Never invent numbers, names, places or forecasts. If the fact sheet does not contain the "
    "answer, say so and name the missing dataset. Always distinguish modelled/regulatory hazard scenarios from "
    "live forecasts. Be concise, use short bullet points, and end with one line on data limitations."
)


def claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def claude_answer(question: str, facts: dict, history: list[dict]) -> str:
    msgs = [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
    msgs.append({"role": "user", "content": f"FACT SHEET:\n{json.dumps(facts, default=str)[:60000]}\n\nQUESTION: {question}"})
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
        "content-type": "application/json"},
        json={"model": ANTHROPIC_MODEL, "max_tokens": 900, "system": SYSTEM, "messages": msgs})
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []))


def answer(question: str, facts: dict, history: list[dict] | None = None) -> tuple[str, str]:
    """Return (answer_markdown, mode)."""
    if claude_available():
        try:
            return claude_answer(question, facts, history or []), "Claude (external AI, grounded on PHOENIX data)"
        except Exception as exc:  # fall back, but say so
            return grounded_answer(question, facts) + ("\n\n_(The external AI service was unavailable, so the built-in "
                                                       "analyst answered.)_"), "Built-in PHOENIX analyst (external AI unavailable)"
    return grounded_answer(question, facts), "Built-in PHOENIX analyst"
