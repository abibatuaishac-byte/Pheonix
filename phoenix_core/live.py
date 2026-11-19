"""Live intelligence feeds (no API keys required).

* Open-Meteo Forecast API  - hourly precipitation forecast (model forecast).
* Open-Meteo Flood API     - GloFAS modelled river discharge forecast.
* NWS api.weather.gov      - active official alerts (US cities only).

Every function returns ``{"ok": False, "error": ...}`` instead of raising, so the
dashboard keeps working offline.  Nothing here is ever mixed with the historical
or probabilistic hazard layers: the UI labels these as FORECAST / ALERT data.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

TIMEOUT = 12
HEADERS = {"User-Agent": "PHOENIX-hackathon-prototype/1.0 (BAU Innovate 2026)"}


def _get(url: str, params: dict | None = None, headers: dict | None = None) -> dict:
    r = requests.get(url, params=params, timeout=TIMEOUT, headers={**HEADERS, **(headers or {})})
    r.raise_for_status()
    return r.json()


def precipitation_forecast(lat: float, lon: float) -> dict:
    try:
        j = _get("https://api.open-meteo.com/v1/forecast", {
            "latitude": lat, "longitude": lon,
            "hourly": "precipitation,precipitation_probability",
            "daily": "precipitation_sum,precipitation_probability_max",
            "current": "precipitation,rain,temperature_2m",
            "forecast_days": 7, "timezone": "auto",
        })
        return {"ok": True, "data": j, "fetched": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    except Exception as exc:  # network, HTTP, JSON
        return {"ok": False, "error": str(exc)}


def river_discharge(lat: float, lon: float) -> dict:
    try:
        j = _get("https://flood-api.open-meteo.com/v1/flood", {
            "latitude": lat, "longitude": lon,
            "daily": "river_discharge,river_discharge_median,river_discharge_max",
            "past_days": 30, "forecast_days": 30,
        })
        return {"ok": True, "data": j, "fetched": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def nws_alerts(lat: float, lon: float) -> dict:
    try:
        j = _get("https://api.weather.gov/alerts/active", {"point": f"{lat:.4f},{lon:.4f}"},
                 headers={"Accept": "application/geo+json"})
        alerts = [{
            "event": f["properties"].get("event"),
            "severity": f["properties"].get("severity"),
            "urgency": f["properties"].get("urgency"),
            "headline": f["properties"].get("headline"),
            "onset": f["properties"].get("onset"),
            "expires": f["properties"].get("expires"),
            "area": f["properties"].get("areaDesc"),
            "sender": f["properties"].get("senderName"),
        } for f in j.get("features", [])]
        return {"ok": True, "alerts": alerts, "fetched": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


FLOOD_WORDS = ("flood", "flash", "hurricane", "storm surge", "tropical", "coastal")


def is_flood_related(event: str | None) -> bool:
    return bool(event) and any(w in event.lower() for w in FLOOD_WORDS)
