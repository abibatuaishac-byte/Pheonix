"""Flood alert engine: turns credible sources into categorised, explained alerts and delivers them safely.

Sources and how far each one can raise the risk category
---------------------------------------------------------
official_warning  US National Weather Service active alerts (api.weather.gov), queried at each registered
                  location. Only these can produce HIGH or CRITICAL, and the message names NWS as the source.
model_forecast    Open-Meteo rainfall forecast compared with an ADMINISTRATOR-SET screening threshold
                  (not a validated flood trigger), combined with whether the location lies in a mapped
                  flood hazard area (World Bank model / FEMA zones). Capped at ELEVATED, always labelled
                  "not an official warning".
hazard maps       Used only as context ("your area is in a mapped flood zone"). They never trigger an
                  alert on their own: they are scenarios, not forecasts.

Sierra Leone: no machine-readable official warning feed is integrated, so Freetown alerts can only be
PHOENIX forecast notices (max ELEVATED).

Delivery rules (dispatch): explicit consent, not opted out, SMS enabled, alert level >= the person's chosen
minimum, never the same event twice, a per-person cooldown that only an escalation can break, a daily cap,
and the recipient record is re-read immediately before each send so an opt-out applies at once.
"""
from __future__ import annotations

import json
import os
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pandas as pd

from . import analysis, data, live
from .alert_store import AlertStore, parse_ts, utcnow
from .cities import CITIES
from .geo import LocalProjection
from .recipient_manager import LEVELS, is_active, mask_phone
from .sms_service import automated_real_sms_allowed, get_setting, redact

# ---------------------------------------------------------------- NWS classification
CRITICAL_EVENTS = {"Storm Surge Warning", "Flash Flood Emergency"}
HIGH_EVENTS = {"Flash Flood Warning", "Flood Warning", "Coastal Flood Warning", "Lakeshore Flood Warning",
               "Hurricane Warning", "Tropical Storm Warning"}
ELEVATED_EVENTS = {"Flood Watch", "Flash Flood Watch", "Coastal Flood Watch", "Lakeshore Flood Watch",
                   "Storm Surge Watch", "Hurricane Watch", "Tropical Storm Watch"}
INFO_EVENTS = {"Flood Advisory", "Coastal Flood Advisory", "Lakeshore Flood Advisory", "Hydrologic Outlook",
               "Flood Statement", "Coastal Flood Statement", "Flash Flood Statement", "Hurricane Local Statement",
               "Tropical Cyclone Local Statement"}

PRECAUTIONS_SMS = {
    4: "Follow official instructions now; move to higher ground if told to; never enter flood water.",
    3: "Avoid flood water and low roads; be ready to move to higher ground; follow official instructions.",
    2: "Stay alert; keep clear of drains, streams and low roads; check official updates.",
    1: "Be aware of possible heavy rain; check official updates.",
}
PRECAUTIONS_FULL = {
    4: "Follow instructions from official emergency services immediately. If told to evacuate, do so. Move to "
       "higher ground. Never walk, swim or drive through flood water. Keep your phone charged.",
    3: "Avoid flood water and low-lying roads, underpasses, drains and stream banks. Prepare to move to higher "
       "ground; move valuables and medicines up. Follow official instructions.",
    2: "Stay alert to changing conditions. Keep clear of drains, streams and low roads during heavy rain. Check "
       "official weather updates and know your route to higher ground.",
    1: "Heavy rain is possible. Clear blocked drains around your home if safe to do so and check official updates.",
}
TAIL = " Reply STOP to opt out, HELP for help."
TEST_PREFIX = "TEST ONLY - NOT A REAL ALERT. "
MAX_SMS_CHARS = 306   # two GSM-7 segments


def nws_level(props: dict) -> int | None:
    """Map one NWS alert to a PHOENIX level. None = not flood-relevant or not an actual alert."""
    if props.get("status") != "Actual" or props.get("messageType") == "Cancel":
        return None
    ev = props.get("event") or ""
    text = " ".join(str(props.get(k) or "") for k in ("headline", "description")).upper()
    params = props.get("parameters") or {}
    catastrophic = any("CATASTROPHIC" in str(v).upper() for v in params.get("flashFloodDamageThreat", []))
    if ev in CRITICAL_EVENTS or (ev == "Flash Flood Warning" and ("FLASH FLOOD EMERGENCY" in text or catastrophic)):
        return 4
    if ev in HIGH_EVENTS:
        return 4 if props.get("severity") == "Extreme" and "Flood" in ev else 3
    if ev in ELEVATED_EVENTS:
        return 2
    if ev in INFO_EVENTS:
        return 1
    return None


def nws_event_key(props: dict) -> str:
    """Updates of one warning share a key (via 'references'), so a person is not re-alerted for each update."""
    refs = props.get("references") or []
    root = min((r.get("identifier") or "" for r in refs), default="") or props.get("id") or props.get("@id")
    return "nws:" + str(root)


def _known_nws_key(store: AlertStore, props: dict) -> str:
    """Follow the update chain. NWS updates sometimes reference only the previous message, so every NWS id we see
    is remembered (source_ids) and mapped to the event key of the first message in the chain."""
    ids = [str(r.get("identifier")) for r in (props.get("references") or []) if r.get("identifier")]
    ids.append(str(props.get("id")))
    ph = ",".join("?" * len(ids))
    row = store.one(f"SELECT event_key FROM source_ids WHERE source_id IN ({ph}) LIMIT 1", ids)
    key = row["event_key"] if row else nws_event_key(props)
    with store.tx() as c:
        c.executemany("INSERT OR IGNORE INTO source_ids(source_id, event_key) VALUES(?, ?)", [(i, key) for i in ids])
    return key


def nws_raw(lat: float, lon: float) -> dict:
    """Active NWS alerts at a point, full properties (live.nws_alerts keeps only a summary)."""
    try:
        j = live._get("https://api.weather.gov/alerts/active", {"point": f"{lat:.4f},{lon:.4f}"},
                      headers={"Accept": "application/geo+json"})
        return {"ok": True, "features": j.get("features", [])}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


# ---------------------------------------------------------------- hazard context (existing analysis modules)
@lru_cache(maxsize=16)
def _hazard_indexes(slug: str):
    c = CITIES[slug]
    proj = LocalProjection(*c.center)
    out = []
    for s in c.available_scenarios:
        try:
            out.append((s, analysis.HazardIndex.build(data.hazard_geometry(s), proj)))
        except Exception:
            continue
    return out


@lru_cache(maxsize=1)
def _ward_table() -> pd.DataFrame:
    return data.freetown_wards().set_index("ward")


def hazard_context(slug: str, lat: float, lon: float, zone_type: str = "point", area_label: str = "") -> dict:
    """Is this registered location in a mapped flood hazard area? (Scenario information, not a forecast.)"""
    if slug == "freetown" and zone_type == "ward":
        try:
            w = _ward_table().loc[area_label]
            share100 = float(w["buildings_RP100"] / w["buildings"]) if w["buildings"] else 0.0
            share20 = float(w["buildings_RP20"] / w["buildings"]) if w["buildings"] else 0.0
            in_h = share100 > 0
            return {"in_hazard": in_h, "text": (
                f"{share100:.0%} of modelled buildings in {area_label} ward are inside the 1-in-100-year flood extent "
                f"({share20:.0%} in the 1-in-20-year extent; World Bank model)." if in_h else
                f"No modelled buildings in {area_label} ward fall inside the World Bank flood extents.")}
        except KeyError:
            pass
    hits = []
    for s, hz in _hazard_indexes(slug):
        d = float(hz.distance_to_points([lon], [lat])[0])
        if d <= 0:
            hits.append(s)
    if hits:
        s = min(hits, key=lambda s: (s.annual_chance_pct or 99) * -1)  # most frequent (narrowest) scenario
        kind = "FEMA regulatory flood zone" if s.kind == "regulatory" else "modelled flood extent"
        return {"in_hazard": True, "text": f"The registered location is inside the {s.label} ({kind})."}
    return {"in_hazard": False, "text": "The registered location is outside the mapped flood hazard areas."}


# ---------------------------------------------------------------- forecast
def rain_next_24h(fc: dict, now: datetime | None = None) -> float | None:
    """Sum of hourly forecast precipitation over the next 24 h from an Open-Meteo response."""
    try:
        d = fc["data"]
        off = timedelta(seconds=d.get("utc_offset_seconds", 0))
        now_local = ((now or datetime.now(timezone.utc)) + off).replace(tzinfo=None, minute=0, second=0, microsecond=0)
        times = [datetime.fromisoformat(t) for t in d["hourly"]["time"]]
        vals = d["hourly"]["precipitation"]
        sel = [v for t, v in zip(times, vals) if now_local <= t < now_local + timedelta(hours=24) and v is not None]
        return float(sum(sel)) if sel else None
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------- message formatting
def gsm_safe(s: str) -> str:
    s = s.replace("–", "-").replace("—", "-").replace("’", "'").replace("“", '"').replace("”", '"').replace("…", "...")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


DEMO_SOURCE_TYPE = "demonstration"
TEST_RESEND_MINUTES = 10   # a second test to the same person within this time is refused


def build_message(alert: dict, test: bool = False) -> str:
    city = CITIES[alert["city"]].name if alert["city"] in CITIES else alert["city"]
    if alert["source_type"] == DEMO_SOURCE_TYPE:
        # Fixed, self-explanatory text: clearly a demonstration, never an emergency alert.
        return gsm_safe(f"PHOENIX DEMONSTRATION - not an official emergency alert. This is a test message from the "
                        f"PHOENIX flood information prototype ({city}). No action is needed." + TAIL)
    prec = PRECAUTIONS_SMS[alert["level"]]
    pre = TEST_PREFIX if test else ""
    if alert["source_type"] == "official_warning":
        head = f"PHOENIX flood alert ({alert['category']}) {city}: {alert['headline']}."
        body = f" {prec}"
    else:
        head = f"PHOENIX flood risk notice ({alert['category']}) {city} - {alert['area_label']}: {alert['headline']}."
        body = f" Not an official warning. {prec}"
    msg = gsm_safe(pre + head + body + TAIL)
    if len(msg) > MAX_SMS_CHARS:  # shorten the headline, never the safety parts
        room = MAX_SMS_CHARS - len(gsm_safe(pre + body + TAIL)) - 3
        msg = gsm_safe(pre + head)[:room].rstrip() + "..." + gsm_safe(body + TAIL)
    return msg


def _short_time(iso: str | None) -> str:
    d = parse_ts(iso)
    return f" until {d.strftime('%d %b %H:%M')} (UTC{d.strftime('%z')[:3]})" if d else ""


# ---------------------------------------------------------------- alert persistence
def upsert_alert(store: AlertStore, a: dict, matches: dict[int, str]) -> tuple[int, bool]:
    """Insert (or touch) an alert; link matched recipients. Returns (alert_id, is_new)."""
    now = utcnow()
    with store.tx() as c:
        row = c.execute("SELECT id FROM alerts WHERE event_key=? AND level=?", (a["event_key"], a["level"])).fetchone()
        if row:
            aid, new = row["id"], False
            c.execute("UPDATE alerts SET last_seen_at=?, expires_at=COALESCE(?, expires_at) WHERE id=?",
                      (now, a.get("expires_at"), aid))
        else:
            prev = c.execute("SELECT id, level FROM alerts WHERE event_key=? ORDER BY level DESC LIMIT 1",
                             (a["event_key"],)).fetchone()
            aid = c.execute(
                """INSERT INTO alerts(created_at, last_seen_at, city, area_label, level, category, source, source_type,
                   event_key, headline, explanation, precautions, expires_at, is_test, escalated_from, raw)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now, now, a["city"], a["area_label"][:200], a["level"], LEVELS[a["level"]], a["source"],
                 a["source_type"], a["event_key"], a["headline"], a["explanation"], a["precautions"],
                 a.get("expires_at"), int(a.get("is_test", 0)),
                 prev["id"] if (prev and prev["level"] < a["level"]) else None, json.dumps(a.get("raw"))[:20000])
            ).lastrowid
            new = True
        for rid, reason in matches.items():
            c.execute("INSERT OR IGNORE INTO alert_matches(alert_id, recipient_id, reason) VALUES(?,?,?)",
                      (aid, rid, reason))
    return aid, new


def _join_areas(labels) -> str:
    u = sorted(set(labels))
    return ", ".join(u[:4]) + (f" (+{len(u) - 4} more)" if len(u) > 4 else "")


def evaluate(store: AlertStore, cities=None, nws_fetch=nws_raw, forecast_fetch=live.precipitation_forecast,
             now: datetime | None = None) -> dict:
    """Check every source for the registered, active recipients and store resulting alerts.

    Never sends anything. Returns {"new_alert_ids", "alert_ids", "sources": {city: {...}}}.
    """
    st = store.settings()
    recips = [r for r in store.q("SELECT * FROM recipients") if is_active(r)]
    cities = cities or sorted({r["city"] for r in recips})
    result = {"new_alert_ids": [], "alert_ids": [], "sources": {}}
    for slug in cities:
        city = CITIES.get(slug)
        rs = [r for r in recips if r["city"] == slug]
        src = result["sources"].setdefault(slug, {"recipients": len(rs)})
        if not rs or city is None:
            continue
        locs: dict[tuple, list[dict]] = {}
        for r in rs:
            locs.setdefault((round(r["lat"], 3), round(r["lon"], 3)), []).append(r)
        ctx = {r["id"]: hazard_context(slug, r["lat"], r["lon"], r["zone_type"], r["area_label"]) for r in rs}
        covered_by_official: set[int] = set()

        # 1) official warnings (US cities)
        if city.nws_alerts:
            nws_alerts: dict[str, dict] = {}
            ok_n = err_n = 0
            for (lat, lon), group in locs.items():
                res = nws_fetch(lat, lon)
                if not res.get("ok"):
                    err_n += 1
                    continue
                ok_n += 1
                for f in res["features"]:
                    p = f.get("properties", {})
                    lvl = nws_level(p)
                    if lvl is None:
                        continue
                    key = _known_nws_key(store, p)
                    entry = nws_alerts.setdefault((key, lvl), {"props": p, "recips": {}})
                    for r in group:
                        entry["recips"][r["id"]] = ctx[r["id"]]["text"]
                        covered_by_official.add(r["id"])
            src["nws"] = {"ok_queries": ok_n, "failed_queries": err_n, "flood_alerts": len(nws_alerts)}
            for (key, lvl), e in nws_alerts.items():
                p = e["props"]
                until = p.get("ends") or p.get("expires")
                area = (p.get("areaDesc") or city.name)[:200]
                a = {"city": slug, "area_label": area, "level": lvl, "source": f"US National Weather Service ({p.get('senderName', 'NWS')})",
                     "source_type": "official_warning", "event_key": key,
                     "headline": f"US National Weather Service {p.get('event')} in effect for your registered area{_short_time(until)}",
                     "explanation": (f"Official {p.get('event')} issued by {p.get('senderName', 'NWS')} "
                                     f"(severity {p.get('severity')}, urgency {p.get('urgency')}, certainty "
                                     f"{p.get('certainty')}). {p.get('headline') or ''}").strip(),
                     "precautions": (p.get("instruction") or PRECAUTIONS_FULL[lvl])[:1200],
                     "expires_at": until, "raw": {k: p.get(k) for k in ("id", "event", "sent", "expires", "ends",
                                                                         "severity", "urgency", "areaDesc")}}
                aid, new = upsert_alert(store, a, e["recips"])
                result["alert_ids"].append(aid)
                if new:
                    result["new_alert_ids"].append(aid)
        else:
            src["nws"] = "No machine-readable official warning feed integrated for this country."

        # 2) PHOENIX rainfall-forecast notices (capped at ELEVATED, labelled as not official)
        if st["forecast_alerts_enabled"]:
            thr = float(st["forecast_threshold_mm_24h"])
            cells: dict[tuple, list[dict]] = {}
            for r in rs:
                if r["id"] in covered_by_official:
                    continue   # an official warning already covers this person
                cells.setdefault((round(r["lat"] * 20) / 20, round(r["lon"] * 20) / 20), []).append(r)
            fc_info = []
            for (clat, clon), group in cells.items():
                fc = forecast_fetch(clat, clon)
                mm = rain_next_24h(fc, now) if fc.get("ok") else None
                fc_info.append({"cell": [clat, clon], "ok": bool(fc.get("ok")), "mm_24h": mm})
                if mm is None or mm < thr:
                    continue
                day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
                for in_h, lvl in ((True, 2), (False, 1)):
                    sub = [r for r in group if ctx[r["id"]]["in_hazard"] == in_h]
                    if not sub:
                        continue
                    a = {"city": slug, "area_label": _join_areas(r["area_label"] for r in sub), "level": lvl,
                         "source": "Open-Meteo rainfall forecast + PHOENIX hazard maps",
                         "source_type": "model_forecast", "event_key": f"fc:{slug}:{clat:.2f},{clon:.2f}:{day}",
                         "headline": (f"forecast about {mm:.0f} mm of rain in the next 24 h (Open-Meteo model)"
                                      + (" and your area is in a mapped flood zone" if in_h else "")),
                         "explanation": (f"Model forecast of {mm:.0f} mm in the next 24 h at {clat:.2f}, {clon:.2f} "
                                         f"meets the administrator's screening threshold of {thr:.0f} mm. This "
                                         "threshold is a PHOENIX setting, not a validated flood prediction. "
                                         + ("Registered locations lie in mapped flood hazard areas (scenario maps, "
                                            "not forecasts)." if in_h else
                                            "Registered locations are outside mapped flood hazard areas.")
                                         + " No official warning was found for these locations."),
                         "precautions": PRECAUTIONS_FULL[lvl],
                         # a forecast notice is only relevant for the 24 h window it describes
                         "expires_at": ((now or datetime.now(timezone.utc)) + timedelta(hours=24)).isoformat(timespec="seconds"),
                         "raw": {"mm_24h": mm, "threshold_mm": thr, "cell": [clat, clon]}}
                    aid, new = upsert_alert(store, a, {r["id"]: ctx[r["id"]]["text"] for r in sub})
                    result["alert_ids"].append(aid)
                    if new:
                        result["new_alert_ids"].append(aid)
            src["forecast"] = fc_info
    return result


# ---------------------------------------------------------------- delivery
SUCCESS_STATES = ("simulated", "queued", "accepted", "sending", "sent", "delivered")


def _eligibility(store: AlertStore, r: dict | None, alert: dict, st: dict, now: datetime) -> str | None:
    """Return a skip reason, or None if the person may be messaged about this alert now."""
    if r is None:
        return "recipient_deleted"
    if not r["consent"]:
        return "no_consent"
    if r["opted_out"]:
        return "opted_out"
    if not r["sms_enabled"]:
        return "sms_disabled"
    if alert["level"] < r["min_level"]:
        return "below_preference"
    ph = ",".join("?" * len(SUCCESS_STATES))
    if store.one(f"SELECT id FROM deliveries WHERE recipient_id=? AND event_key=? AND is_test=0 AND level>=? "
                 f"AND status IN ({ph})", (r["id"], alert["event_key"], alert["level"], *SUCCESS_STATES)):
        return "duplicate"
    last = store.one(f"SELECT created_at, level FROM deliveries WHERE recipient_id=? AND is_test=0 AND status IN ({ph}) "
                     "ORDER BY id DESC LIMIT 1", (r["id"], *SUCCESS_STATES))
    escalation = bool(last) and alert["level"] > last["level"]
    cooldown = float(st["cooldown_minutes"])
    if last and not escalation and cooldown > 0:
        if now - parse_ts(last["created_at"]) < timedelta(minutes=cooldown):
            return "cooldown"
    day_n = store.one(f"SELECT COUNT(*) n FROM deliveries WHERE recipient_id=? AND is_test=0 AND status IN ({ph}) "
                      "AND created_at>=?", (r["id"], *SUCCESS_STATES,
                                             (now - timedelta(hours=24)).isoformat(timespec="seconds")))["n"]
    if day_n >= int(st["max_messages_per_day"]) and not (alert["level"] == 4 and alert["source_type"] == "official_warning"):
        return "daily_cap"
    return None


def _record(store, alert, r, status, provider=None, res=None, skip=None, test=False, message=None):
    now = utcnow()
    with store.tx() as c:
        if status == "skipped":  # one skip record per person+alert+reason is enough for the audit trail
            if c.execute("SELECT 1 FROM deliveries WHERE alert_id=? AND recipient_id=? AND status='skipped' AND skip_reason=?",
                         (alert["id"], r["id"] if r else -1, skip)).fetchone():
                return
        c.execute("""INSERT INTO deliveries(alert_id, recipient_id, event_key, level, created_at, updated_at, status,
                     skip_reason, provider, provider_sid, error_code, error_message, attempts, is_test, phone_masked, message)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (alert["id"], r["id"] if r else -1, alert["event_key"], alert["level"], now, now, status, skip,
                   provider, res.sid if res else None, res.error_code if res else None,
                   redact(res.error_message)[:300] if (res and res.error_message) else None, 1 if res else 0, int(test),
                   mask_phone(r["phone"]) if r else None, message))


class ProviderNotConfigured(RuntimeError):
    pass


def _require_configured(provider):
    ok, msg = provider.configured()
    if not ok:
        raise ProviderNotConfigured(msg)


def _send_one(store, provider, alert, r, test=False) -> str:
    msg = build_message(alert, test=test)
    try:
        res = provider.send(r["phone"], msg)
    except Exception as exc:  # provider crashed: record and continue with others
        from .sms_service import SendResult
        res = SendResult(False, "failed", getattr(provider, "name", "?"), error_code="EXCEPTION",
                         error_message=redact(exc)[:200])
    _record(store, alert, r, res.status if res.ok else "failed", provider.name, res, test=test, message=msg)
    from .sms_service import OPTED_OUT_CODE
    if not res.ok and res.error_code == OPTED_OUT_CODE:
        from .recipient_manager import opt_out
        opt_out(store, r["id"], source="Provider reports recipient replied STOP (error 21610)")
    return "sent" if res.ok else "failed"


def dispatch(store: AlertStore, provider, alert_ids=None, now: datetime | None = None) -> dict:
    """Send pending, non-test alerts to matched, eligible recipients. Returns counts."""
    _require_configured(provider)
    now = now or datetime.now(timezone.utc)
    st = store.settings()
    if alert_ids is None:
        alerts = store.q("SELECT * FROM alerts WHERE is_test=0 AND (expires_at IS NULL OR expires_at > ?) ORDER BY level DESC, id",
                         (now.isoformat(timespec="seconds"),))
    else:
        alerts = [store.one("SELECT * FROM alerts WHERE id=?", (i,)) for i in alert_ids]
        alerts = [a for a in alerts if a and not a["is_test"]]
    counts = {"sent": 0, "failed": 0, "skipped": 0, "reasons": {}}
    for a in sorted(alerts, key=lambda a: -a["level"]):  # most severe first
        for m in store.q("SELECT recipient_id FROM alert_matches WHERE alert_id=?", (a["id"],)):
            r = store.one("SELECT * FROM recipients WHERE id=?", (m["recipient_id"],))  # fresh read: opt-outs apply now
            why = _eligibility(store, r, a, st, now)
            if why:
                counts["skipped"] += 1
                counts["reasons"][why] = counts["reasons"].get(why, 0) + 1
                _record(store, a, r, "skipped", skip=why)
                continue
            counts[_send_one(store, provider, a, r)] += 1
    return counts


def preview_recipients(store: AlertStore, alert_ids=None) -> pd.DataFrame:
    """Who WOULD be messaged now (no sending). Used for the confirmation step."""
    now = datetime.now(timezone.utc)
    st = store.settings()
    rows = []
    q = "SELECT * FROM alerts WHERE is_test=0" + ("" if alert_ids is None else
                                                  f" AND id IN ({','.join(str(int(i)) for i in alert_ids) or '0'})")
    for a in store.q(q):
        for m in store.q("SELECT recipient_id FROM alert_matches WHERE alert_id=?", (a["id"],)):
            r = store.one("SELECT * FROM recipients WHERE id=?", (m["recipient_id"],))
            rows.append({"alert_id": a["id"], "category": a["category"], "recipient": r["name"] if r else "(deleted)",
                         "phone": mask_phone(r["phone"]) if r else "", "would_send": _eligibility(store, r, a, st, now) is None,
                         "reason_if_not": _eligibility(store, r, a, st, now) or ""})
    return pd.DataFrame(rows)


def sample_alert(city: str, level: int, area_label: str, official_style: bool) -> dict:
    """In-memory sample alert, clearly marked as a test (used for previews and test sends)."""
    name = CITIES[city].name
    if official_style:
        head = f"SAMPLE ONLY - example of how an official flood warning for {area_label} would appear"
        stype, src = "official_warning", "TEST - sample of an official-warning message"
    else:
        head = f"SAMPLE ONLY - example rainfall forecast notice for {area_label}"
        stype, src = "model_forecast", "TEST - sample of a PHOENIX forecast notice"
    return {"city": city, "area_label": area_label, "level": level, "category": LEVELS[level], "source": src,
            "source_type": stype, "event_key": f"test:{city}:{utcnow()}:{level}:{int(official_style)}",
            "headline": head, "precautions": PRECAUTIONS_FULL[level], "is_test": 1,
            "explanation": f"Administrator-generated test notification for {name}. Not based on any real warning."}


def demo_alert(city: str, area_label: str) -> dict:
    """The recommended message for live demonstrations: says plainly it is a demonstration, not an alert."""
    return {"city": city, "area_label": area_label, "level": 1, "category": LEVELS[1],
            "source": "TEST - PHOENIX demonstration message", "source_type": DEMO_SOURCE_TYPE,
            "event_key": f"test:demo:{city}:{utcnow()}", "headline": "PHOENIX demonstration message",
            "precautions": "None - demonstration only.", "is_test": 1,
            "explanation": "Administrator-sent demonstration message. Not based on any warning or forecast."}


def make_test_alert(store: AlertStore, city: str, level: int, area_label: str, official_style: bool,
                    demonstration: bool = False) -> int:
    """Store a sample alert for test mode. Test alerts are never dispatched to groups."""
    a = demo_alert(city, area_label) if demonstration else sample_alert(city, level, area_label, official_style)
    aid, _ = upsert_alert(store, a, {})
    return aid


class RepeatSendBlocked(ValueError):
    pass


def last_test_send(store: AlertStore, recipient_id: int) -> dict | None:
    """Most recent test message accepted for this person (failed attempts do not count)."""
    ph = ",".join("?" * len(SUCCESS_STATES))
    return store.one(f"SELECT * FROM deliveries WHERE recipient_id=? AND is_test=1 AND status IN ({ph}) "
                     "ORDER BY id DESC LIMIT 1", (recipient_id, *SUCCESS_STATES))


def send_test(store: AlertStore, provider, alert_id: int, recipient_id: int) -> str:
    """Send ONE test message to ONE consenting recipient (the UI requires explicit confirmation first)."""
    _require_configured(provider)
    a = store.one("SELECT * FROM alerts WHERE id=?", (alert_id,))
    r = store.one("SELECT * FROM recipients WHERE id=?", (recipient_id,))
    if not a or not a["is_test"]:
        raise ValueError("Test sends must use a test alert.")
    if r is None or not r["consent"] or r["opted_out"]:
        raise ValueError("Test messages can only go to a registered person with consent who has not opted out.")
    if not r["sms_enabled"]:
        raise ValueError("SMS is turned off for this person.")
    last = last_test_send(store, recipient_id)
    if last:
        mins = (datetime.now(timezone.utc) - parse_ts(last["created_at"])).total_seconds() / 60
        if mins < TEST_RESEND_MINUTES:
            raise RepeatSendBlocked(f"A test message was already sent to this person {mins:.0f} minute(s) ago. "
                                    f"To prevent repeated messages, wait {TEST_RESEND_MINUTES - mins:.0f} more "
                                    "minute(s) before sending another.")
    return _send_one(store, provider, a, r, test=True)


def retry_failed(store: AlertStore, provider, now=None) -> dict:
    _require_configured(provider)
    st = store.settings()
    now = now or datetime.now(timezone.utc)
    out = {"retried": 0, "sent": 0, "failed": 0, "gave_up": 0}
    fails = store.q("SELECT d.* FROM deliveries d WHERE d.status='failed' AND d.is_test=0 AND d.id = "
                    "(SELECT MAX(id) FROM deliveries x WHERE x.recipient_id=d.recipient_id AND x.event_key=d.event_key)")
    for d in fails:
        if d["attempts"] >= int(st["max_attempts"]) or (d["error_code"] in {"21610", "21211", "21614", "21217"}):
            out["gave_up"] += 1
            continue
        a = store.one("SELECT * FROM alerts WHERE id=?", (d["alert_id"],))
        r = store.one("SELECT * FROM recipients WHERE id=?", (d["recipient_id"],))
        why = _eligibility(store, r, a, st, now) if a else "alert_missing"
        if why and why != "cooldown":
            out["gave_up"] += 1
            continue
        out["retried"] += 1
        res = _send_one(store, provider, a, r)
        with store.tx() as c:  # carry the attempt count forward
            c.execute("UPDATE deliveries SET attempts=? WHERE id=(SELECT MAX(id) FROM deliveries WHERE recipient_id=? AND event_key=?)",
                      (d["attempts"] + 1, r["id"], a["event_key"]))
        out[res] += 1
    return out


def refresh_statuses(store: AlertStore, provider) -> int:
    """Ask the provider for the latest status of in-flight real messages (confirms delivery)."""
    from .sms_service import IN_FLIGHT
    n = 0
    for d in store.q(f"SELECT * FROM deliveries WHERE provider=? AND provider_sid IS NOT NULL AND status IN "
                     f"({','.join('?' * len(IN_FLIGHT))})", (provider.name, *IN_FLIGHT)):
        res = provider.fetch_status(d["provider_sid"])
        if res.status and res.status != "unknown" and res.status != d["status"]:
            with store.tx() as c:
                c.execute("UPDATE deliveries SET status=?, error_code=?, error_message=?, updated_at=? WHERE id=?",
                          (res.status, res.error_code, redact(res.error_message)[:300] or None, utcnow(), d["id"]))
            n += 1
    return n


def dashboard_stats(store: AlertStore) -> dict:
    rec = store.q("SELECT * FROM recipients")
    real = "is_test=0"
    s = lambda sql: store.one(sql)["n"]  # noqa: E731
    return {
        "recipients": len(rec),
        "active": sum(is_active(r) for r in rec),
        "opted_out": sum(bool(r["opted_out"]) for r in rec),
        "alerts": s(f"SELECT COUNT(*) n FROM alerts WHERE {real}"),
        "attempted": s(f"SELECT COUNT(*) n FROM deliveries WHERE {real} AND status!='skipped'"),
        "simulated": s(f"SELECT COUNT(*) n FROM deliveries WHERE {real} AND status='simulated'"),
        "awaiting": s(f"SELECT COUNT(*) n FROM deliveries WHERE {real} AND status IN ('queued','accepted','sending','sent')"),
        "delivered": s(f"SELECT COUNT(*) n FROM deliveries WHERE {real} AND status='delivered'"),
        "failed": s(f"SELECT COUNT(*) n FROM deliveries WHERE {real} AND status IN ('failed','undelivered')"),
        "skipped": s(f"SELECT COUNT(*) n FROM deliveries WHERE {real} AND status='skipped'"),
        "tests": s("SELECT COUNT(*) n FROM deliveries WHERE is_test=1"),
    }


# ---------------------------------------------------------------- honest capability status
def _last_source_success(store: AlertStore, kind: str) -> str | None:
    """Most recent monitor run in which at least one request to this source succeeded."""
    for r in store.q("SELECT finished_at, sources FROM monitor_runs WHERE sources IS NOT NULL ORDER BY id DESC LIMIT 200"):
        try:
            src = json.loads(r["sources"] or "{}")
        except ValueError:
            continue
        for v in src.values():
            if kind == "nws" and isinstance(v.get("nws"), dict) and v["nws"].get("ok_queries", 0) > 0:
                return r["finished_at"]
            if kind == "forecast" and any(f.get("ok") for f in v.get("forecast", [])):
                return r["finished_at"]
    return None


def capability_status(store: AlertStore, provider, monitors: list[dict], public: bool = False) -> list[dict]:
    """Status of each capability, based only on evidence recorded on this installation.

    'Operational' is shown only after the capability has actually worked here; otherwise the dependency is named.
    """
    st = store.settings()
    rows = []
    admin = bool(get_setting("PHOENIX_ADMIN_PASSCODE"))
    rows.append({"Capability": "Administrator authentication",
                 "Status": "Enabled" if admin else "Not set up - admin tools disabled",
                 "Evidence / dependency": "Administrator passcode configured on the server." if admin else
                 "An administrator passcode must be configured on the server."})
    n_rec = store.one("SELECT COUNT(*) n FROM recipients")["n"]
    rows.append({"Capability": "Recipient registration, consent and opt-out records",
                 "Status": "Available (local database)",
                 "Evidence / dependency": ("Local database; no external service needed." if public else
                                           f"{n_rec} recipient(s) registered on this installation; no external service needed.")})
    nws_ok = _last_source_success(store, "nws")
    rows.append({"Capability": "Official warnings: US National Weather Service (New Orleans, Houston, Miami)",
                 "Status": f"Operational - last successful query {nws_ok}" if nws_ok else "Not yet verified here",
                 "Evidence / dependency": "Public service. Verified when a check succeeds for a registered US location."})
    rows.append({"Capability": "Official warnings: Sierra Leone",
                 "Status": "Not available",
                 "Evidence / dependency": "No machine-readable official warning feed has been integrated."})
    fc_ok = _last_source_success(store, "forecast")
    rows.append({"Capability": "Rainfall forecast notices (Open-Meteo)",
                 "Status": f"Operational - last successful query {fc_ok}" if fc_ok else "Not yet verified here",
                 "Evidence / dependency": "Public service. The rain threshold is an administrator setting, not a "
                                          "validated flood trigger."})
    ok, msg = provider.configured()
    sent_ok = store.one("SELECT MAX(updated_at) t FROM deliveries WHERE provider='twilio' AND provider_sid IS NOT NULL "
                        "AND status NOT IN ('failed','skipped')")["t"]
    delivered = store.one("SELECT MAX(updated_at) t FROM deliveries WHERE provider='twilio' AND status='delivered'")["t"]
    if not ok:
        sms, ev = "Not configured - sending disabled", "Twilio credentials must be configured on the server."
    elif delivered:
        sms, ev = f"Operational - delivery confirmed by Twilio {delivered}", "A real message was reported delivered."
    elif sent_ok:
        sms, ev = "Message accepted by Twilio - delivery not yet confirmed", f"Last accepted {sent_ok}. Use Refresh delivery status."
    elif st.get("twilio_verified_at"):
        sms, ev = "Credentials verified - no message sent yet", f"{st['twilio_verified_note']} ({st['twilio_verified_at']})"
    else:
        sms, ev = "Configured - not yet verified", "Credentials present; use 'Check connection' (sends no message)."
    rows.append({"Capability": "SMS delivery (Twilio)", "Status": sms, "Evidence / dependency": ev})
    final = store.one("SELECT MAX(updated_at) t FROM deliveries WHERE provider='twilio' AND status IN "
                      "('delivered','undelivered','failed') AND provider_sid IS NOT NULL")["t"]
    rows.append({"Capability": "Delivery-status tracking (Twilio status polling)",
                 "Status": f"Operational - last final status {final}" if final else "Not yet verified here",
                 "Evidence / dependency": "Needs at least one real message; statuses are fetched from Twilio on demand "
                                          "and on each monitor cycle."})
    alive = [m for m in monitors if m["alive"]]
    rows.append({"Capability": "Automatic monitoring",
                 "Status": ("Running: " + ", ".join(m["runner"] for m in alive)) if alive else "Not running",
                 "Evidence / dependency": "Runs only while the in-app monitor or the monitor service is running."})
    auto = st["auto_send"] and ok and automated_real_sms_allowed()
    rows.append({"Capability": "Automatic SMS sending",
                 "Status": "Enabled" if auto else "Off",
                 "Evidence / dependency": "Needs the auto-send setting, a Twilio connection and server approval for "
                                          "unattended sending."})
    return rows
