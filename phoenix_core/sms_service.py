"""SMS delivery through Twilio.

The application always uses TwilioProvider. It is operational only when these environment variables are set:
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER
    (optional: TWILIO_MESSAGING_SERVICE_SID - send through a Messaging Service, e.g. for US A2P 10DLC)
Without them, sending is disabled and the app says so. Nothing is ever simulated in the app.

MockProvider exists ONLY for the automated test-suite (tests inject it directly). The application has no
setting or environment variable that selects it.

Twilio is called over plain HTTPS with `requests` (no extra package). Credentials are read from the environment
only, never written to disk, logged or shown.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import requests

FINAL_OK = {"delivered"}
FINAL_BAD = {"failed", "undelivered", "canceled"}
IN_FLIGHT = {"queued", "accepted", "scheduled", "sending", "sent"}
# Twilio error codes with a specific meaning for us
OPTED_OUT_CODE = "21610"        # recipient replied STOP: Twilio blocks further messages
INVALID_NUMBER_CODES = {"21211", "21614", "21217"}


# ---------------------------------------------------------------- settings: environment, then Streamlit secrets
_SECTIONS = ("twilio", "phoenix", "sms")


def _secrets():
    """Streamlit secrets if a secrets file / Cloud secrets exist; None otherwise (never raises)."""
    try:
        import streamlit as st
        sec = st.secrets
        sec.keys()           # forces loading; raises if no secrets are configured
        return sec
    except Exception:
        return None


def _from_secrets(name: str):
    sec = _secrets()
    if sec is None:
        return None
    short = name.lower().split("_", 1)[1] if "_" in name else name.lower()   # TWILIO_AUTH_TOKEN -> auth_token
    try:
        for key in (name, name.lower()):
            if key in sec and not hasattr(sec[key], "keys"):
                return str(sec[key])
        for section in _SECTIONS:
            if section in sec and hasattr(sec[section], "keys"):
                block = sec[section]
                for key in (name, name.lower(), short, short.upper()):
                    if key in block:
                        return str(block[key])
    except Exception:
        return None
    return None


def get_setting(name: str, default: str = "") -> str:
    """Read a server-side setting: environment variable first, then Streamlit secrets.

    Streamlit secrets may be top level (TWILIO_AUTH_TOKEN = "...") or in a section, e.g.
        [twilio]
        account_sid = "AC..."
        auth_token = "..."
        phone_number = "+1..."
    Values are never logged or displayed.
    """
    v = os.environ.get(name)
    if v is not None and v.strip():
        return v.strip()
    v = _from_secrets(name)
    return v.strip() if v and v.strip() else default


def setting_source(name: str) -> str:
    """Where a setting comes from (for the admin Setup page) - never the value."""
    if os.environ.get(name, "").strip():
        return "environment variable"
    if (_from_secrets(name) or "").strip():
        return "Streamlit secrets"
    return "not set"


_PHONE_RE = re.compile(r"\+?\d[\d\s\-\(\)\.]{6,}\d")
_SID_RE = re.compile(r"\b(AC|SK|MG|PN)[0-9a-fA-F]{32}\b")


def redact(text) -> str:
    """Remove secrets and phone numbers from any text before it is stored, logged or shown.

    Masks: the configured Twilio Auth Token and Account SID (exact values), any Twilio-style SID
    (AC/SK/MG/PN + 32 hex), and anything that looks like a phone number (keeps the last 2 digits).
    """
    if text is None:
        return ""
    t = str(text)
    for k in ("TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID"):
        v = get_setting(k)
        if len(v) >= 6:
            t = t.replace(v, "[redacted]")
    t = _SID_RE.sub(lambda m: m.group(1) + "…[redacted]", t)
    return _PHONE_RE.sub(lambda m: "•••" + re.sub(r"\D", "", m.group(0))[-2:], t)


def mask_number(p: str | None) -> str:
    if not p:
        return ""
    return p[:2] + "•" * max(0, len(p) - 4) + p[-2:]


@dataclass
class SendResult:
    ok: bool                 # accepted by the provider (NOT the same as delivered)
    status: str              # simulated | queued | sent | delivered | failed | ...
    provider: str
    sid: str | None = None
    error_code: str | None = None
    error_message: str | None = None


# Plain-language meaning of common Twilio error codes (shown instead of raw provider text)
ERROR_HELP = {
    "21211": "Invalid phone number", "21214": "Number cannot receive SMS", "21408": "Sending to this country is not enabled",
    "21610": "Recipient replied STOP (opted out)", "21612": "Route to this number not available",
    "21614": "Not a mobile number", "21608": "Trial account: recipient number not verified",
    "30003": "Handset unreachable", "30004": "Message blocked", "30005": "Unknown or inactive number",
    "30006": "Landline or unreachable carrier", "30007": "Filtered by carrier", "30008": "Unknown delivery error",
    "30034": "US sender not registered (A2P 10DLC)", "NETWORK": "Could not reach Twilio",
    "CONFIG": "SMS not configured", "EXCEPTION": "Unexpected sending error",
}


def explain_error(code) -> str:
    return ERROR_HELP.get(str(code or ""), f"Provider error {code}" if code else "")


class MockProvider:
    """TEST-ONLY fake provider. Records messages in memory; never used by the application.

    fail_numbers: numbers that should fail (to exercise failure handling);
    fail_codes:   optional {number: error_code} for specific provider errors (e.g. 21610 opt-out).
    """
    name = "mock"
    real = False

    def __init__(self, fail_numbers=(), fail_codes: dict | None = None, raise_on_send=False):
        self.outbox: list[dict] = []
        self.fail_numbers = set(fail_numbers)
        self.fail_codes = dict(fail_codes or {})
        self.raise_on_send = raise_on_send
        self._n = 0

    def configured(self) -> tuple[bool, str]:
        return True, "Test provider (automated tests only)."

    def send(self, to: str, body: str) -> SendResult:
        if self.raise_on_send:
            raise ConnectionError("simulated network outage")
        self._n += 1
        sid = f"MOCK{self._n:06d}"
        self.outbox.append({"to": to, "body": body, "sid": sid})
        if to in self.fail_numbers or to in self.fail_codes:
            return SendResult(False, "failed", self.name, sid, self.fail_codes.get(to, "MOCK-FAIL"),
                              "Simulated provider failure")
        return SendResult(True, "simulated", self.name, sid)

    def fetch_status(self, sid: str) -> SendResult:
        return SendResult(True, "simulated", self.name, sid)


class TwilioProvider:
    name = "twilio"
    real = True
    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages"

    def __init__(self, sid=None, token=None, from_number=None, messaging_service_sid=None, timeout=20):
        self.sid = (sid or get_setting("TWILIO_ACCOUNT_SID")).strip()
        self.token = (token or get_setting("TWILIO_AUTH_TOKEN")).strip()
        self.from_number = (from_number or get_setting("TWILIO_PHONE_NUMBER")).strip()
        self.messaging_service_sid = (messaging_service_sid or get_setting("TWILIO_MESSAGING_SERVICE_SID")).strip()
        self.timeout = timeout

    def missing_settings(self) -> list[str]:
        """Names of missing server settings - for the administrator setup page only."""
        out = [n for n, v in (("TWILIO_ACCOUNT_SID", self.sid), ("TWILIO_AUTH_TOKEN", self.token),
                              ("TWILIO_PHONE_NUMBER", self.from_number)) if not v]
        if self.sid and not self.sid.startswith("AC"):
            out.append("TWILIO_ACCOUNT_SID (must start with AC)")
        return out

    def configured(self) -> tuple[bool, str]:
        """User-facing: no setting names, no secrets."""
        if self.missing_settings():
            return False, "SMS delivery is not configured on this server."
        return True, "Twilio credentials are configured on this server."

    @property
    def sender_masked(self) -> str:
        return mask_number(self.from_number)

    def verify(self) -> tuple[bool, str]:
        """Check the credentials and sender number with Twilio WITHOUT sending a message."""
        ok, msg = self.configured()
        if not ok:
            return False, msg
        base = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}"
        try:
            r = requests.get(base + ".json", auth=(self.sid, self.token), timeout=self.timeout)
            if r.status_code == 401:
                return False, "Twilio rejected the account credentials."
            if r.status_code >= 400:
                return False, f"Twilio account check failed (error {r.status_code})."
            acct = r.json()
            if acct.get("status") != "active":
                return False, f"Twilio account status is '{acct.get('status')}', not active."
            r2 = requests.get(base + "/IncomingPhoneNumbers.json", auth=(self.sid, self.token), timeout=self.timeout,
                              params={"PhoneNumber": self.from_number})
            nums = r2.json().get("incoming_phone_numbers", []) if r2.status_code < 400 else []
            if not nums:
                return False, "The account is valid, but the configured sender number is not on this Twilio account."
            caps = nums[0].get("capabilities", {})
            if caps and not caps.get("sms", False):
                return False, "The configured sender number cannot send SMS."
            trial = acct.get("type") == "Trial"
            return True, ("Connected: account and sender number verified with Twilio (no message sent)."
                          + (" Trial account: Twilio only delivers to verified recipient numbers and adds a trial "
                             "prefix to messages." if trial else ""))
        except (requests.RequestException, ValueError) as exc:
            return False, "Could not reach Twilio: " + redact(str(exc))[:150]

    def send(self, to: str, body: str) -> SendResult:
        ok, msg = self.configured()
        if not ok:
            return SendResult(False, "failed", self.name, error_code="CONFIG", error_message=msg)
        try:
            payload = {"To": to, "Body": body}
            if self.messaging_service_sid:
                payload["MessagingServiceSid"] = self.messaging_service_sid
            else:
                payload["From"] = self.from_number
            r = requests.post(self.API.format(sid=self.sid) + ".json", auth=(self.sid, self.token), timeout=self.timeout,
                              data=payload)
            j = r.json() if r.content else {}
        except (requests.RequestException, ValueError) as exc:
            return SendResult(False, "failed", self.name, error_code="NETWORK", error_message=redact(exc)[:200])
        if r.status_code >= 400:
            return SendResult(False, "failed", self.name, error_code=str(j.get("code", r.status_code)),
                              error_message=redact(j.get("message", r.text))[:300])
        return SendResult(True, j.get("status", "queued"), self.name, j.get("sid"),
                          str(j["error_code"]) if j.get("error_code") else None, redact(j.get("error_message")) or None)

    def fetch_status(self, sid: str) -> SendResult:
        try:
            r = requests.get(self.API.format(sid=self.sid) + f"/{sid}.json", auth=(self.sid, self.token),
                             timeout=self.timeout)
            j = r.json()
        except (requests.RequestException, ValueError) as exc:
            return SendResult(False, "unknown", self.name, sid, "NETWORK", redact(exc)[:200])
        if r.status_code >= 400:
            return SendResult(False, "unknown", self.name, sid, str(j.get("code", r.status_code)), redact(j.get("message")))
        st = j.get("status", "unknown")
        return SendResult(st not in FINAL_BAD, st, self.name, sid,
                          str(j["error_code"]) if j.get("error_code") else None, redact(j.get("error_message")) or None)


def get_provider():
    """The application's SMS provider: always Twilio (operational only when configured)."""
    return TwilioProvider()


def automated_real_sms_allowed() -> bool:
    """Automatic (monitor-driven) sending through a REAL provider needs a second, explicit switch."""
    return get_setting("PHOENIX_ALLOW_AUTOMATED_SMS") == "1"
