"""Tests for Flood Alerts & SMS. The SMS provider and all data feeds are mocked: no real messages,
no network calls.  Run: python -m pytest -q tests/test_alerts.py"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phoenix_core import alert_monitor, alerts  # noqa: E402
from phoenix_core import recipient_manager as rm  # noqa: E402
from phoenix_core.alert_store import AlertStore  # noqa: E402
from phoenix_core.sms_service import MockProvider, TwilioProvider, get_provider  # noqa: E402

NOW = datetime.now(timezone.utc)
NO_PT = (29.951, -90.072)        # central New Orleans
FT_PT = (8.4657, -13.2317)       # central Freetown


@pytest.fixture
def store(tmp_path):
    s = AlertStore(tmp_path / "alerts.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def no_real_sms(monkeypatch):
    """Guarantee no test can reach a real provider."""
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TWILIO_MESSAGING_SERVICE_SID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("PHOENIX_ALLOW_AUTOMATED_SMS", raising=False)
    import requests

    def boom(*a, **k):
        raise AssertionError("network call attempted during tests")
    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(requests, "get", boom)


def add(store, phone="+15045550123", city="new_orleans", lat=NO_PT[0], lon=NO_PT[1], min_level=1, **kw):
    r = rm.RecipientInput(name=kw.pop("name", "Test Person"), phone=phone, city=city,
                          area_label=kw.pop("area", "Mid-City"), zone_type=kw.pop("zone_type", "point"),
                          lat=lat, lon=lon, min_level=min_level, consent=kw.pop("consent", True),
                          consent_method=kw.pop("consent_method", rm.CONSENT_METHODS[0]), **kw)
    return rm.register(store, r)[0]


def nws_feature(event="Flash Flood Warning", aid="urn:oid:A1", refs=(), status="Actual", severity="Severe", **p):
    props = {"id": aid, "event": event, "status": status, "messageType": "Alert", "severity": severity,
             "urgency": "Immediate", "certainty": "Observed", "senderName": "NWS New Orleans LA",
             "areaDesc": "Orleans, LA", "headline": f"{event} issued", "description": "", "instruction": None,
             "expires": "2026-11-19T23:30:00-06:00", "ends": None,
             "references": [{"identifier": r} for r in refs], **p}
    return {"properties": props}


def nws_fixed(*features):
    return lambda lat, lon: {"ok": True, "features": list(features)}


def no_nws(lat, lon):
    return {"ok": True, "features": []}


def forecast(mm_per_hour):
    def f(lat, lon):
        start = NOW.replace(tzinfo=None, minute=0, second=0, microsecond=0) - timedelta(hours=2)
        return {"ok": True, "data": {"utc_offset_seconds": 0, "hourly": {
            "time": [(start + timedelta(hours=h)).isoformat(timespec="minutes") for h in range(72)],
            "precipitation": [mm_per_hour] * 72}}}
    return f


def run(store, provider=None, **fetch):
    fetch.setdefault("nws_fetch", no_nws)
    fetch.setdefault("forecast_fetch", forecast(0.0))
    res = alerts.evaluate(store, now=NOW, **fetch)
    sent = alerts.dispatch(store, provider or MockProvider(), alert_ids=res["alert_ids"], now=NOW) if res["alert_ids"] else None
    return res, sent


# ---------------------------------------------------------------- registration & consent
def test_phone_validation():
    assert rm.validate_phone("+1 (504) 555-0123")[0] == "+15045550123"
    assert rm.validate_phone("00232 76 123456", "freetown")[0] == "+23276123456"
    for bad in ["5045550123", "+1 504 555 012", "+1 104 555 0123", "+232 76 1234", "abc"]:
        with pytest.raises(rm.ValidationError):
            rm.validate_phone(bad)
    assert rm.validate_phone("+23276123456", "miami")[1]  # wrong country -> warning, not error


def test_register_requires_consent_and_blocks_duplicates(store):
    with pytest.raises(rm.ValidationError):
        add(store, consent=False)
    add(store)
    with pytest.raises(rm.ValidationError):
        add(store, phone="+1 504 555 0123")      # same number, same city, different formatting
    add(store, city="houston", lat=29.76, lon=-95.37)  # same number, other city is allowed
    assert len(rm.list_recipients(store)) == 2


def test_name_field_cannot_hold_numbers(store):
    with pytest.raises(rm.ValidationError):
        add(store, name="call 5045550123")


def test_mask_never_shows_full_number():
    m = rm.mask_phone("+15045550123")
    assert m.startswith("+150") and m.endswith("23") and "5550" not in m


# ---------------------------------------------------------------- alert generation & location matching
def test_official_warning_generates_high_alert_matched_to_location(store):
    a = add(store)
    b = add(store, phone="+15045550124", lat=29.80, lon=-89.95, area="New Orleans East")
    calls = []

    def nws(lat, lon):
        calls.append((lat, lon))
        return {"ok": True, "features": [nws_feature()] if (lat, lon) == (29.951, -90.072) else []}
    res, sent = run(store, nws_fetch=nws)
    assert len(calls) == 2                           # one query per distinct registered location
    al = store.one("SELECT * FROM alerts")
    assert al["category"] == "HIGH" and al["source_type"] == "official_warning"
    assert "National Weather Service" in al["source"] and al["explanation"] and al["precautions"]
    matched = {m["recipient_id"] for m in store.q("SELECT recipient_id FROM alert_matches")}
    assert matched == {a}                            # only the person inside the warned area
    assert sent["sent"] == 1


@pytest.mark.parametrize("event,expected", [("Flood Watch", 2), ("Flood Advisory", 1), ("Storm Surge Warning", 4),
                                            ("Tornado Warning", None), ("Heat Advisory", None)])
def test_nws_level_mapping(event, expected):
    assert alerts.nws_level(nws_feature(event)["properties"]) == expected


def test_flash_flood_emergency_is_critical_and_test_messages_ignored():
    p = nws_feature(description="...FLASH FLOOD EMERGENCY FOR ORLEANS...")["properties"]
    assert alerts.nws_level(p) == 4
    assert alerts.nws_level(nws_feature(status="Test")["properties"]) is None
    assert alerts.nws_level(nws_feature(messageType="Cancel")["properties"]) is None


def test_forecast_notice_capped_and_labelled(store):
    add(store, phone="+23276123456", city="freetown", lat=FT_PT[0], lon=FT_PT[1], area="Central")
    res, _ = run(store, forecast_fetch=forecast(5.0))        # 120 mm / 24 h, threshold 50
    al = store.q("SELECT * FROM alerts")
    assert len(al) == 1 and al[0]["level"] <= 2 and al[0]["source_type"] == "model_forecast"
    assert "not a validated flood prediction" in al[0]["explanation"]
    msg = alerts.build_message(al[0])
    assert "Not an official warning" in msg and "PHOENIX" in msg


def test_forecast_below_threshold_and_hazard_maps_alone_trigger_nothing(store):
    add(store, phone="+23276123456", city="freetown", lat=FT_PT[0], lon=FT_PT[1])
    res, _ = run(store, forecast_fetch=forecast(1.0))         # 24 mm < 50 mm
    assert res["alert_ids"] == [] and store.q("SELECT * FROM alerts") == []


def test_freetown_never_high_without_official_source(store):
    add(store, phone="+23276123456", city="freetown", lat=FT_PT[0], lon=FT_PT[1])
    run(store, forecast_fetch=forecast(50.0))                 # absurd rain still only ELEVATED max
    assert max(a["level"] for a in store.q("SELECT level FROM alerts")) <= 2


def test_ward_hazard_context_uses_world_bank_ward_data():
    ctx = alerts.hazard_context("freetown", 8.47, -13.23, "ward", "Kroo Town")
    assert "World Bank" in ctx["text"]


def test_official_warning_suppresses_forecast_notice(store):
    add(store)
    run(store, nws_fetch=nws_fixed(nws_feature("Flood Watch")), forecast_fetch=forecast(5.0))
    assert {a["source_type"] for a in store.q("SELECT source_type FROM alerts")} == {"official_warning"}


def test_feed_failure_is_recorded_not_fatal(store):
    add(store)
    res = alert_monitor.run_check(store, MockProvider(), send=False,
                                  nws_fetch=lambda la, lo: {"ok": False, "error": "timeout"},
                                  forecast_fetch=lambda la, lo: {"ok": False, "error": "timeout"})
    assert res["ok"] and res["evaluation"]["sources"]["new_orleans"]["nws"]["failed_queries"] == 1
    assert not res["complete"] and store.last_run(successful_only=True) is None   # not reported as a successful check


# ---------------------------------------------------------------- consent, opt-out, duplicates, cooldown
def test_opted_out_and_disabled_never_messaged(store):
    a = add(store)
    b = add(store, phone="+15045550124", sms_enabled=False)
    rm.opt_out(store, a, "test")
    prov = MockProvider()
    res, sent = run(store, prov, nws_fetch=nws_fixed(nws_feature()))
    assert prov.outbox == [] and sent is None                      # not even matched to an alert
    assert store.q("SELECT * FROM alert_matches") == []
    # and if they were matched earlier, dispatch still refuses them
    with store.tx() as c:
        c.execute("INSERT INTO alerts(created_at,last_seen_at,city,area_label,level,category,source,source_type,"
                  "event_key,headline,explanation,precautions) VALUES(datetime('now'),datetime('now'),'new_orleans',"
                  "'x',3,'HIGH','s','official_warning','k','h','e','p')")
        aid = c.execute("SELECT MAX(id) FROM alerts").fetchone()[0]
        c.executemany("INSERT INTO alert_matches VALUES(?,?,?)", [(aid, a, ""), (aid, b, "")])
    sent = alerts.dispatch(store, prov, alert_ids=[aid], now=NOW)
    assert prov.outbox == [] and set(sent["reasons"]) == {"opted_out", "sms_disabled"}


def test_opt_out_applies_immediately_between_generation_and_send(store):
    a = add(store)
    res = alerts.evaluate(store, nws_fetch=nws_fixed(nws_feature()), forecast_fetch=forecast(0), now=NOW)
    rm.opt_out(store, a, "STOP")                     # after the alert was matched, before sending
    prov = MockProvider()
    alerts.dispatch(store, prov, alert_ids=res["alert_ids"], now=NOW)
    assert prov.outbox == []


def test_stop_start_help_keywords(store):
    a = add(store)
    assert "unsubscribed" in rm.handle_inbound(store, "+1 504 555 0123", "stop")
    assert rm.get(store, a)["opted_out"] == 1
    assert "subscribed again" in rm.handle_inbound(store, "+15045550123", "START")
    assert rm.get(store, a)["opted_out"] == 0
    assert "not a government agency" in rm.handle_inbound(store, "+15045550123", "HELP", "ops@example.org")


def test_below_preference_is_skipped(store):
    add(store, min_level=3)
    prov = MockProvider()
    _, sent = run(store, prov, nws_fetch=nws_fixed(nws_feature("Flood Watch")))  # ELEVATED < HIGH
    assert prov.outbox == [] and sent["reasons"] == {"below_preference": 1}


def test_same_event_never_sent_twice_including_updates(store):
    add(store)
    prov = MockProvider()
    run(store, prov, nws_fetch=nws_fixed(nws_feature(aid="A1")))
    run(store, prov, nws_fetch=nws_fixed(nws_feature(aid="A1")))
    run(store, prov, nws_fetch=nws_fixed(nws_feature(aid="A2", refs=["A1"])))   # NWS update of the same warning
    assert len(prov.outbox) == 1


def test_cooldown_blocks_new_same_level_event_but_escalation_goes_through(store):
    add(store)
    prov = MockProvider()
    run(store, prov, nws_fetch=nws_fixed(nws_feature("Flood Watch", aid="W1")))       # ELEVATED
    run(store, prov, nws_fetch=nws_fixed(nws_feature("Coastal Flood Watch", aid="W2")))  # new ELEVATED event
    assert len(prov.outbox) == 1                                                      # cooldown
    run(store, prov, nws_fetch=nws_fixed(nws_feature("Flash Flood Warning", aid="X1")))  # HIGH = escalation
    assert len(prov.outbox) == 2 and "HIGH" in prov.outbox[-1]["body"]


def test_cooldown_expires(store):
    add(store)
    prov = MockProvider()
    store.set_settings(cooldown_minutes=60)
    res = alerts.evaluate(store, nws_fetch=nws_fixed(nws_feature("Flood Watch", aid="W1")), forecast_fetch=forecast(0), now=NOW)
    alerts.dispatch(store, prov, res["alert_ids"], now=NOW)
    res = alerts.evaluate(store, nws_fetch=nws_fixed(nws_feature("Flood Watch", aid="W2")), forecast_fetch=forecast(0), now=NOW)
    later = datetime.now(timezone.utc) + timedelta(minutes=61)
    alerts.dispatch(store, prov, res["alert_ids"], now=later)
    assert len(prov.outbox) == 2


def test_daily_cap(store):
    add(store)
    store.set_settings(cooldown_minutes=0, max_messages_per_day=2)
    prov = MockProvider()
    for i in range(4):
        run(store, prov, nws_fetch=nws_fixed(nws_feature("Flood Watch", aid=f"W{i}")))
    assert len(prov.outbox) == 2


# ---------------------------------------------------------------- message formatting
def test_message_format_official_and_test():
    a = {"city": "new_orleans", "level": 3, "category": "HIGH", "source_type": "official_warning",
         "area_label": "Orleans, LA", "headline": "US National Weather Service Flash Flood Warning for Orleans, LA "
         "until 19 Nov 23:30 (UTC-06)"}
    m = alerts.build_message(a)
    assert m.startswith("PHOENIX flood alert (HIGH) New Orleans") and "STOP" in m and "HELP" in m
    assert len(m) <= alerts.MAX_SMS_CHARS and m.isascii()
    t = alerts.build_message(a, test=True)
    assert t.startswith("TEST ONLY - NOT A REAL ALERT.")


def test_long_headline_truncated_but_safety_text_kept():
    a = {"city": "freetown", "level": 2, "category": "ELEVATED", "source_type": "model_forecast",
         "area_label": "X" * 80, "headline": "very long " * 40}
    m = alerts.build_message(a)
    assert len(m) <= alerts.MAX_SMS_CHARS and "Not an official warning" in m and m.endswith("HELP for help.")


# ---------------------------------------------------------------- provider failures & delivery status
def test_provider_failure_recorded_and_retried(store):
    add(store)
    prov = MockProvider(fail_numbers={"+15045550123"})
    _, sent = run(store, prov, nws_fetch=nws_fixed(nws_feature()))
    assert sent["failed"] == 1
    d = store.one("SELECT * FROM deliveries WHERE status='failed'")
    assert d["error_code"] == "MOCK-FAIL" and d["phone_masked"] != "+15045550123"
    prov.fail_numbers.clear()
    out = alerts.retry_failed(store, prov, now=NOW)
    assert out["sent"] == 1


def test_provider_exception_does_not_stop_others(store):
    add(store)
    add(store, phone="+15045550124")
    _, sent = run(store, MockProvider(raise_on_send=True), nws_fetch=nws_fixed(nws_feature()))
    assert sent["failed"] == 2


def test_provider_stop_error_marks_opted_out(store):
    a = add(store)
    run(store, MockProvider(fail_codes={"+15045550123": "21610"}), nws_fetch=nws_fixed(nws_feature()))
    assert rm.get(store, a)["opted_out"] == 1


def test_delivery_status_refresh_and_honest_counts(store):
    add(store)

    class FakeReal(MockProvider):
        name, real = "fake-real", True

        def send(self, to, body):
            r = super().send(to, body)
            r.status, r.provider = "queued", self.name
            return r

        def fetch_status(self, sid):
            from phoenix_core.sms_service import SendResult
            return SendResult(True, "delivered", self.name, sid)
    prov = FakeReal()
    run(store, prov, nws_fetch=nws_fixed(nws_feature()))
    s = alerts.dashboard_stats(store)
    assert s["attempted"] == 1 and s["delivered"] == 0 and s["awaiting"] == 1   # not counted until confirmed
    assert alerts.refresh_statuses(store, prov) == 1
    assert alerts.dashboard_stats(store)["delivered"] == 1


def test_mock_sends_are_never_counted_as_delivered(store):
    add(store)
    run(store, MockProvider(), nws_fetch=nws_fixed(nws_feature()))
    s = alerts.dashboard_stats(store)
    assert s["simulated"] == 1 and s["delivered"] == 0


def test_twilio_without_credentials_is_not_configured():
    p = TwilioProvider()
    ok, msg = p.configured()
    assert not ok and "TWILIO" not in msg                 # user-facing text names no settings
    assert "TWILIO_ACCOUNT_SID" in p.missing_settings()   # admin setup page does
    res = TwilioProvider().send("+15045550123", "x")     # must fail cleanly, without network
    assert not res.ok and res.error_code == "CONFIG"


def test_app_provider_is_always_twilio_never_mock(monkeypatch):
    monkeypatch.setenv("PHOENIX_SMS_PROVIDER", "mock")    # old setting must have no effect
    p = get_provider()
    assert isinstance(p, TwilioProvider) and p.real


def test_nothing_sent_or_recorded_when_twilio_not_configured(store):
    add(store)
    res = alerts.evaluate(store, nws_fetch=nws_fixed(nws_feature()), forecast_fetch=forecast(0), now=NOW)
    with pytest.raises(alerts.ProviderNotConfigured):
        alerts.dispatch(store, TwilioProvider(), alert_ids=res["alert_ids"], now=NOW)
    assert store.q("SELECT * FROM deliveries") == []           # no fake 'failed' or 'sent' records
    store.set_settings(auto_send=True)
    ok, why = alert_monitor.may_auto_send(store, TwilioProvider())
    assert not ok and "Twilio connection" in why


class FakeResp:
    def __init__(self, code, payload):
        self.status_code, self._p, self.content, self.text = code, payload, b"x", str(payload)

    def json(self):
        return self._p


def twilio(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "1" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15045550000")
    return TwilioProvider()


def test_twilio_send_request_format_and_success(monkeypatch):
    import requests
    calls = []

    def post(url, auth, timeout, data):
        calls.append((url, auth, data))
        return FakeResp(201, {"sid": "SM123", "status": "queued", "error_code": None, "error_message": None})
    monkeypatch.setattr(requests, "post", post)
    p = twilio(monkeypatch)
    r = p.send("+15045550123", "hello")
    url, auth, data = calls[0]
    assert url == f"https://api.twilio.com/2010-04-01/Accounts/{'AC' + '1' * 32}/Messages.json"
    assert auth == ("AC" + "1" * 32, "secret-token")
    assert data == {"To": "+15045550123", "Body": "hello", "From": "+15045550000"}
    assert r.ok and r.status == "queued" and r.sid == "SM123"


def test_twilio_messaging_service_used_when_set(monkeypatch):
    import requests
    seen = {}
    monkeypatch.setattr(requests, "post", lambda url, auth, timeout, data: seen.update(data) or
                        FakeResp(201, {"sid": "SM1", "status": "accepted"}))
    monkeypatch.setenv("TWILIO_MESSAGING_SERVICE_SID", "MG123")
    twilio(monkeypatch).send("+15045550123", "x")
    assert seen.get("MessagingServiceSid") == "MG123" and "From" not in seen


def test_twilio_error_responses(monkeypatch):
    import requests
    p = twilio(monkeypatch)
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(400, {"code": 21211, "message": "Invalid 'To' Phone Number"}))
    r = p.send("+15045550123", "x")
    assert not r.ok and r.error_code == "21211" and "Invalid" in r.error_message

    def down(*a, **k):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(requests, "post", down)
    r = p.send("+15045550123", "x")
    assert not r.ok and r.error_code == "NETWORK"


def test_twilio_status_fetch(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda url, auth, timeout: FakeResp(200, {"status": "undelivered",
                                                                                  "error_code": 30003, "error_message": "Unreachable"}))
    r = twilio(monkeypatch).fetch_status("SM123")
    assert r.status == "undelivered" and r.error_code == "30003" and not r.ok


def test_twilio_verify_checks_account_and_number_without_sending(monkeypatch):
    import requests

    def get(url, auth, timeout, params=None):
        if url.endswith("/IncomingPhoneNumbers.json"):
            return FakeResp(200, {"incoming_phone_numbers": [{"capabilities": {"sms": True}}]})
        return FakeResp(200, {"status": "active", "type": "Full"})
    monkeypatch.setattr(requests, "get", get)       # requests.post still raises: nothing is sent
    ok, msg = twilio(monkeypatch).verify()
    assert ok and "no message sent" in msg
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(401, {}))
    assert not twilio(monkeypatch).verify()[0]


def test_real_delivery_flow_with_status_tracking(store, monkeypatch):
    """Full path through TwilioProvider (HTTP faked): send -> queued -> refresh -> delivered."""
    import requests
    add(store)
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(201, {"sid": "SM9", "status": "queued"}))
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, {"status": "delivered"}))
    p = twilio(monkeypatch)
    res = alerts.evaluate(store, nws_fetch=nws_fixed(nws_feature()), forecast_fetch=forecast(0), now=NOW)
    out = alerts.dispatch(store, p, alert_ids=res["alert_ids"], now=NOW)
    assert out["sent"] == 1
    s = alerts.dashboard_stats(store)
    assert s["attempted"] == 1 and s["delivered"] == 0 and s["awaiting"] == 1
    caps = {c["Capability"]: c["Status"] for c in alerts.capability_status(store, p, [])}
    assert caps["SMS delivery (Twilio)"].startswith("Message accepted")
    assert alerts.refresh_statuses(store, p) == 1
    caps = {c["Capability"]: c["Status"] for c in alerts.capability_status(store, p, [])}
    assert caps["SMS delivery (Twilio)"].startswith("Operational")
    assert caps["Delivery-status tracking (Twilio status polling)"].startswith("Operational")


def test_capability_status_never_claims_unverified(store):
    caps = {c["Capability"]: c["Status"] for c in alerts.capability_status(store, TwilioProvider(), [])}
    assert caps["SMS delivery (Twilio)"].startswith("Not configured")
    assert caps["Official warnings: US National Weather Service (New Orleans, Houston, Miami)"] == "Not yet verified here"
    assert caps["Delivery-status tracking (Twilio status polling)"] == "Not yet verified here"
    assert caps["Automatic monitoring"] == "Not running"
    assert caps["Administrator authentication"].startswith("Not set up")


# ---------------------------------------------------------------- test mode & automation guards
def test_test_mode_single_recipient_and_marked(store):
    a = add(store)
    add(store, phone="+15045550124")
    aid = alerts.make_test_alert(store, "new_orleans", 3, "Mid-City", official_style=True)
    prov = MockProvider()
    alerts.send_test(store, prov, aid, a)
    assert len(prov.outbox) == 1 and prov.outbox[0]["body"].startswith("TEST ONLY - NOT A REAL ALERT.")
    # test alerts are never included in a group dispatch
    alerts.dispatch(store, prov, now=NOW)
    assert len(prov.outbox) == 1
    assert alerts.dashboard_stats(store)["attempted"] == 0 and alerts.dashboard_stats(store)["tests"] == 1


def test_test_send_refuses_opted_out(store):
    a = add(store)
    rm.opt_out(store, a, "x")
    aid = alerts.make_test_alert(store, "new_orleans", 2, "Mid-City", official_style=False)
    with pytest.raises(ValueError):
        alerts.send_test(store, MockProvider(), aid, a)


def test_monitor_does_not_send_unless_enabled(store, monkeypatch):
    add(store)
    prov = MockProvider()
    kw = dict(nws_fetch=nws_fixed(nws_feature()), forecast_fetch=forecast(0))
    alert_monitor.run_check(store, prov, **kw)
    assert prov.outbox == []                                   # auto_send off by default
    store.set_settings(auto_send=True)
    alert_monitor.run_check(store, prov, **kw)
    assert len(prov.outbox) == 1


def test_real_provider_auto_send_needs_second_switch(store, monkeypatch):
    store.set_settings(auto_send=True)

    class FakeReal(MockProvider):
        real = True   # a configured real provider
    ok, _ = alert_monitor.may_auto_send(store, FakeReal())
    assert not ok
    monkeypatch.setenv("PHOENIX_ALLOW_AUTOMATED_SMS", "1")
    assert alert_monitor.may_auto_send(store, FakeReal())[0]


def test_monitor_status_uses_heartbeat(store):
    store.heartbeat("worker", True, 15)
    assert alert_monitor.monitor_status(store)[0]["alive"]
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert alert_monitor.monitor_status(store, now=later)[0]["state"] == "Not responding"


def test_database_folder_is_gitignored(tmp_path):
    s = AlertStore(tmp_path / "private" / "a.db")
    assert (tmp_path / "private" / ".gitignore").read_text().strip().endswith("*")
    s.close()


def test_update_chain_referencing_only_previous_message(store):
    add(store)
    prov = MockProvider()
    store.set_settings(cooldown_minutes=0)
    run(store, prov, nws_fetch=nws_fixed(nws_feature(aid="X1")))
    run(store, prov, nws_fetch=nws_fixed(nws_feature(aid="X2", refs=["X1"])))
    run(store, prov, nws_fetch=nws_fixed(nws_feature(aid="X3", refs=["X2"])))    # references only X2
    assert len(prov.outbox) == 1


# Shapes copied from real api.weather.gov alerts active on 2026-11-19 (public domain), descriptions omitted.
REAL_2026_11_19 = [
    {"id": "urn:oid:...7b6b38.001.1", "event": "Flood Warning", "status": "Actual", "messageType": "Alert",
     "severity": "Severe", "urgency": "Expected", "certainty": "Likely", "senderName": "NWS Melbourne FL",
     "areaDesc": "Brevard, FL", "headline": "Flood Warning issued November 19 at 2:38PM EST until November 19 at 7:00PM EST by NWS Melbourne FL",
     "description": "", "expires": "2026-11-19T19:00:00-05:00", "ends": "2026-11-19T19:00:00-05:00", "references": [],
     "parameters": {}},
    {"id": "urn:oid:...721595.001.1", "event": "Flood Watch", "status": "Actual", "messageType": "Update",
     "severity": "Severe", "urgency": "Future", "certainty": "Possible", "senderName": "NWS Miami FL",
     "areaDesc": "Inland Palm Beach County; Metro Palm Beach County; Inland Broward County; Metro Broward County; "
                 "Inland Miami-Dade County; Metropolitan Miami Dade; Coastal Miami Dade County",
     "headline": "Flood Watch issued November 19 at 2:51AM EST until November 19 at 7:00PM EST by NWS Miami FL",
     "description": "", "expires": "2026-11-19T19:00:00-05:00", "ends": "2026-11-19T19:00:00-05:00",
     "references": [{"identifier": "urn:oid:...16f695.001.1"}], "parameters": {}},
    {"id": "urn:oid:...f2884f.001.1", "event": "Flood Warning", "status": "Actual", "messageType": "Update",
     "severity": "Severe", "urgency": "Immediate", "certainty": "Observed", "senderName": "NWS Melbourne FL",
     "areaDesc": "Lake, FL; Volusia, FL", "headline": "Flood Warning issued November 19 at 2:11PM EST by NWS Melbourne FL",
     "description": "", "expires": "2026-11-20T10:00:00-05:00", "ends": None,
     "references": [{"identifier": "urn:oid:...014e80.001.1"}], "parameters": {}},
]


@pytest.mark.parametrize("props,level", list(zip(REAL_2026_11_19, [3, 2, 3])))
def test_real_nws_alert_shapes(store, props, level):
    assert alerts.nws_level(props) == level
    add(store, phone="+13055550123", city="miami", lat=25.774, lon=-80.193, area="Downtown")
    res = alerts.evaluate(store, nws_fetch=nws_fixed({"properties": props}), forecast_fetch=forecast(0), now=NOW)
    a = store.one("SELECT * FROM alerts")
    m = alerts.build_message(a)
    assert len(m) <= alerts.MAX_SMS_CHARS and m.isascii() and "National Weather Service" in m and "STOP" in m
    assert props["event"] in m


# ---------------------------------------------------------------- secrets & redaction
def test_redact_removes_phone_numbers_sids_and_configured_secrets(monkeypatch):
    from phoenix_core.sms_service import redact
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok_abcdef123456")
    sid = "AC" + "0123456789abcdef" * 2
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", sid)
    raw = (f"The 'To' number +1 (504) 555-0123 is not valid. URL /Accounts/{sid}/Messages; token tok_abcdef123456; "
           "+23276123456")
    out = redact(raw)
    for secret in ("555-0123", "5550123", sid, "tok_abcdef123456", "76123456"):
        assert secret not in out
    assert "•••23" in out and "•••56" in out


def test_provider_errors_are_redacted_before_storage(store, monkeypatch):
    import requests
    add(store)
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(400, {
        "code": 21211, "message": "The 'To' number +15045550123 is not a valid phone number."}))
    p = twilio(monkeypatch)
    res = alerts.evaluate(store, nws_fetch=nws_fixed(nws_feature()), forecast_fetch=forecast(0), now=NOW)
    alerts.dispatch(store, p, alert_ids=res["alert_ids"], now=NOW)
    d = store.one("SELECT * FROM deliveries WHERE status='failed'")
    assert "5550123" not in (d["error_message"] or "") and d["error_code"] == "21211"
    from phoenix_core.sms_service import explain_error
    assert explain_error("21211") == "Invalid phone number"


def test_verify_network_error_hides_account_sid(monkeypatch):
    import requests

    def get(url, **k):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
    monkeypatch.setattr(requests, "get", get)
    p = twilio(monkeypatch)
    ok, msg = p.verify()
    assert not ok and p.sid not in msg


def test_sender_number_is_masked(monkeypatch):
    p = twilio(monkeypatch)
    assert p.sender_masked.startswith("+1") and p.sender_masked.endswith("00") and "5045550000" not in p.sender_masked


def test_auto_send_messages_name_no_settings(store, monkeypatch):
    store.set_settings(auto_send=True)
    for prov in (TwilioProvider(), twilio(monkeypatch)):
        ok, why = alert_monitor.may_auto_send(store, prov)
        assert not ok and "PHOENIX_" not in why and "TWILIO_" not in why


def test_auto_send_still_off_by_default(store):
    assert store.settings()["auto_send"] is False


def test_gitignore_protects_secrets_and_personal_data():
    gi = (ROOT / ".gitignore").read_text()
    for entry in (".env", ".streamlit/secrets.toml", "data/private/", "*.db"):
        assert entry in gi


# ---------------------------------------------------------------- settings from env / Streamlit secrets
class FakeSecrets(dict):
    pass


def test_settings_read_from_streamlit_secrets_sections(monkeypatch):
    from phoenix_core import sms_service
    sec = FakeSecrets(twilio=FakeSecrets(account_sid="AC" + "2" * 32, auth_token="sec-token", phone_number="+15045550000"),
                      phoenix=FakeSecrets(admin_passcode="pw-from-secrets"))
    monkeypatch.setattr(sms_service, "_secrets", lambda: sec)
    p = TwilioProvider()
    assert p.configured()[0] and p.token == "sec-token" and p.from_number == "+15045550000"
    assert sms_service.get_setting("PHOENIX_ADMIN_PASSCODE") == "pw-from-secrets"
    assert sms_service.setting_source("TWILIO_AUTH_TOKEN") == "Streamlit secrets"
    assert "sec-token" not in sms_service.redact("error sec-token")          # secrets values are redacted too


def test_settings_top_level_secrets_and_env_priority(monkeypatch):
    from phoenix_core import sms_service
    monkeypatch.setattr(sms_service, "_secrets", lambda: FakeSecrets(TWILIO_AUTH_TOKEN="top-level"))
    assert sms_service.get_setting("TWILIO_AUTH_TOKEN") == "top-level"
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "from-env")
    assert sms_service.get_setting("TWILIO_AUTH_TOKEN") == "from-env"
    assert sms_service.setting_source("TWILIO_AUTH_TOKEN") == "environment variable"


def test_missing_secrets_file_is_harmless(monkeypatch):
    from phoenix_core import sms_service
    assert sms_service.get_setting("TWILIO_AUTH_TOKEN") == ""      # no env, no secrets file in tests
    assert sms_service.setting_source("TWILIO_AUTH_TOKEN") == "not set"


def test_automatic_sending_still_needs_explicit_approval(store, monkeypatch):
    from phoenix_core import sms_service
    monkeypatch.setattr(sms_service, "_secrets", lambda: FakeSecrets())
    assert not sms_service.automated_real_sms_allowed()
    assert store.settings()["auto_send"] is False


# ---------------------------------------------------------------- demonstration test messages
def test_demonstration_message_is_clearly_not_an_alert():
    m = alerts.build_message(alerts.demo_alert("freetown", "Kroo Town"), test=True)
    assert m.startswith("PHOENIX DEMONSTRATION - not an official emergency alert.")
    assert "No action is needed" in m and m.endswith("Reply STOP to opt out, HELP for help.")
    assert "flood alert (" not in m and "Warning" not in m and len(m) <= alerts.MAX_SMS_CHARS and m.isascii()


def test_repeat_test_send_blocked_but_failed_attempt_does_not_block(store):
    rid = add(store)
    failing = MockProvider(fail_numbers={"+15045550123"})
    a1 = alerts.make_test_alert(store, "new_orleans", 1, "Mid-City", False, demonstration=True)
    assert alerts.send_test(store, failing, a1, rid) == "failed"
    ok = MockProvider()
    a2 = alerts.make_test_alert(store, "new_orleans", 1, "Mid-City", False, demonstration=True)
    assert alerts.send_test(store, ok, a2, rid) == "sent"            # failure did not block a retry
    a3 = alerts.make_test_alert(store, "new_orleans", 1, "Mid-City", False, demonstration=True)
    with pytest.raises(alerts.RepeatSendBlocked):
        alerts.send_test(store, ok, a3, rid)
    assert len(ok.outbox) == 1


def test_test_send_refuses_sms_disabled(store):
    rid = add(store, sms_enabled=False)
    aid = alerts.make_test_alert(store, "new_orleans", 1, "Mid-City", False, demonstration=True)
    with pytest.raises(ValueError):
        alerts.send_test(store, MockProvider(), aid, rid)


def test_demo_sends_never_counted_as_alert_deliveries(store):
    rid = add(store)
    aid = alerts.make_test_alert(store, "new_orleans", 1, "Mid-City", False, demonstration=True)
    alerts.send_test(store, MockProvider(), aid, rid)
    s = alerts.dashboard_stats(store)
    assert s["attempted"] == 0 and s["alerts"] == 0 and s["tests"] == 1
