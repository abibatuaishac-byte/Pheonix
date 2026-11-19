"""Recipient registration, consent and opt-out handling.

Data minimisation: we keep only what is needed to send a relevant alert - a name (or nickname),
an international phone number, city, a neighbourhood label and an *approximate* location
(rounded to ~100 m), alert-level preference and the consent / opt-out record.
Phone numbers are never shown in full in the UI (see mask_phone).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .alert_store import AlertStore, utcnow

LEVELS = {1: "INFORMATIONAL", 2: "ELEVATED", 3: "HIGH", 4: "CRITICAL"}
LEVEL_BY_NAME = {v: k for k, v in LEVELS.items()}

# Expected country calling code per city (numbers from elsewhere are allowed but flagged).
CITY_COUNTRY_CODE = {"freetown": "232", "new_orleans": "1", "houston": "1", "miami": "1"}

E164 = re.compile(r"^\+[1-9]\d{7,14}$")
CONSENT_METHODS = ["Signed paper form", "Online form", "Verbal consent (recorded by administrator)",
                   "SMS keyword opt-in (START/JOIN)"]
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
START_WORDS = {"START", "UNSTOP", "YES", "JOIN"}
HELP_WORDS = {"HELP", "INFO"}


class ValidationError(ValueError):
    pass


def normalise_phone(raw: str) -> str:
    """Strip spaces, dashes, dots and brackets; convert a leading 00 to +."""
    s = re.sub(r"[\s\-\.\(\)]", "", (raw or "").strip())
    if s.startswith("00"):
        s = "+" + s[2:]
    return s


def validate_phone(raw: str, city: str | None = None) -> tuple[str, str | None]:
    """Return (E.164 number, warning or None). Raises ValidationError if invalid."""
    p = normalise_phone(raw)
    if not E164.match(p):
        raise ValidationError("Phone number must be in international format, e.g. +232 76 123456 or +1 504 555 0123.")
    if p.startswith("+1"):
        nat = p[2:]
        if len(nat) != 10 or nat[0] in "01" or nat[3] in "01":
            raise ValidationError("US numbers must be +1 followed by a valid 10-digit number (area code and exchange "
                                  "cannot start with 0 or 1).")
    elif p.startswith("+232"):
        if len(p[4:]) != 8:
            raise ValidationError("Sierra Leone numbers must be +232 followed by 8 digits.")
    warn = None
    cc = CITY_COUNTRY_CODE.get(city or "")
    if cc and not p.startswith("+" + cc):
        warn = f"This number is not a +{cc} number; check that the recipient can receive SMS from this service."
    return p, warn


def mask_phone(p: str | None) -> str:
    if not p:
        return ""
    return p[:4] + "•" * max(0, len(p) - 6) + p[-2:]


@dataclass
class RecipientInput:
    name: str
    phone: str
    city: str
    area_label: str
    zone_type: str
    lat: float
    lon: float
    min_level: int = 2
    sms_enabled: bool = True
    consent: bool = False
    consent_method: str = ""
    recorded_by: str = "administrator"


def _clean_name(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip())
    if not n or len(n) > 80:
        raise ValidationError("Enter a name or nickname (1-80 characters).")
    if re.search(r"\d{5,}", n):
        raise ValidationError("The name field must not contain phone or ID numbers.")
    return n


def register(store: AlertStore, r: RecipientInput) -> tuple[int, str | None]:
    """Create a recipient. Requires explicit consent. Returns (id, warning)."""
    name = _clean_name(r.name)
    phone, warn = validate_phone(r.phone, r.city)
    if not r.consent:
        raise ValidationError("Explicit consent is required before a person can be registered for alerts.")
    if r.consent_method not in CONSENT_METHODS:
        raise ValidationError("Record how consent was given.")
    if r.min_level not in LEVELS:
        raise ValidationError("Choose a valid minimum alert level.")
    if not (-90 <= r.lat <= 90 and -180 <= r.lon <= 180):
        raise ValidationError("Location coordinates are out of range.")
    area = (r.area_label or "").strip()[:80]
    if not area:
        raise ValidationError("Enter the neighbourhood or area.")
    now = utcnow()
    try:
        with store.tx() as c:
            rid = c.execute(
                """INSERT INTO recipients(name, phone, city, area_label, zone_type, lat, lon, min_level, sms_enabled,
                   consent, consent_at, consent_method, consent_recorded_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (name, phone, r.city, area, r.zone_type, round(r.lat, 3), round(r.lon, 3), int(r.min_level),
                 int(r.sms_enabled), 1, now, r.consent_method, r.recorded_by, now, now)).lastrowid
    except sqlite3.IntegrityError:
        raise ValidationError("This phone number is already registered for this city.") from None
    return rid, warn


def update(store: AlertStore, rid: int, **fields):
    allowed = {"name", "area_label", "zone_type", "lat", "lon", "min_level", "sms_enabled"}
    bad = set(fields) - allowed
    if bad:
        raise ValidationError(f"Cannot update: {', '.join(sorted(bad))}")
    if "name" in fields:
        fields["name"] = _clean_name(fields["name"])
    if "min_level" in fields and fields["min_level"] not in LEVELS:
        raise ValidationError("Choose a valid minimum alert level.")
    for k in ("lat", "lon"):
        if k in fields:
            fields[k] = round(float(fields[k]), 3)
    if "sms_enabled" in fields:
        fields["sms_enabled"] = int(bool(fields["sms_enabled"]))
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    with store.tx() as c:
        c.execute(f"UPDATE recipients SET {sets} WHERE id=?", (*fields.values(), utcnow(), rid))


def opt_out(store: AlertStore, rid: int, source: str = "administrator"):
    """Takes effect immediately: every send path re-reads this flag just before sending."""
    with store.tx() as c:
        c.execute("UPDATE recipients SET opted_out=1, opted_out_at=?, opt_out_source=?, updated_at=? WHERE id=?",
                  (utcnow(), source, utcnow(), rid))


def opt_back_in(store: AlertStore, rid: int, method: str):
    """Re-subscribing needs a fresh, explicit consent record."""
    if method not in CONSENT_METHODS:
        raise ValidationError("Record how the person gave consent again.")
    with store.tx() as c:
        c.execute("UPDATE recipients SET opted_out=0, opted_out_at=NULL, opt_out_source=NULL, consent=1, "
                  "consent_at=?, consent_method=?, updated_at=? WHERE id=?", (utcnow(), method, utcnow(), rid))


def delete(store: AlertStore, rid: int):
    """Erase the person (right to erasure). Delivery history keeps only the masked number."""
    with store.tx() as c:
        c.execute("DELETE FROM alert_matches WHERE recipient_id=?", (rid,))
        c.execute("DELETE FROM recipients WHERE id=?", (rid,))


def get(store: AlertStore, rid: int) -> dict | None:
    return store.one("SELECT * FROM recipients WHERE id=?", (rid,))


def list_recipients(store: AlertStore, city: str | None = None) -> list[dict]:
    if city:
        return store.q("SELECT * FROM recipients WHERE city=? ORDER BY name", (city,))
    return store.q("SELECT * FROM recipients ORDER BY city, name")


def is_active(r: dict) -> bool:
    return bool(r["consent"]) and not r["opted_out"] and bool(r["sms_enabled"])


def handle_inbound(store: AlertStore, from_phone: str, body: str, help_contact: str = "") -> str | None:
    """Process an inbound SMS keyword (STOP / START / HELP). Returns the reply text, if any.

    Wire this to the provider's inbound-message webhook in a deployment; in the local prototype the
    administrator can record an inbound reply from the Recipients tab.
    Twilio long codes in the US/Canada also apply STOP/HELP automatically on Twilio's side.
    """
    try:
        phone = normalise_phone(from_phone)
    except Exception:
        return None
    word = re.sub(r"[^A-Z]", "", (body or "").strip().upper().split(" ")[0] if body else "")
    rows = store.q("SELECT id FROM recipients WHERE phone=?", (phone,))
    if word in STOP_WORDS:
        for r in rows:
            opt_out(store, r["id"], source=f"SMS keyword {word}")
        return "PHOENIX: you are unsubscribed and will receive no more flood alerts. Reply START to resubscribe."
    if word in START_WORDS:
        for r in rows:
            opt_back_in(store, r["id"], "SMS keyword opt-in (START/JOIN)")
        return ("PHOENIX flood alerts: you are subscribed again. Reply STOP to opt out, HELP for help." if rows else
                "PHOENIX: this number is not registered. Ask your PHOENIX administrator to register you.")
    if word in HELP_WORDS:
        contact = f" Contact: {help_contact}." if help_contact else ""
        return ("PHOENIX flood information service (not a government agency)." + contact +
                " In an emergency call local emergency services. Reply STOP to opt out.")
    return None
