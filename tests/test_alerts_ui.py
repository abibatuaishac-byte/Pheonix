"""UI tests for the alerts section (Streamlit AppTest; no network, no SMS).  Run: python -m pytest -q"""
import sys
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
APP = str(ROOT / "app.py")
SECTION = "Flood Alerts & SMS Notifications"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOENIX_ALERTS_DB", str(tmp_path / "ui.db"))
    for k in ("PHOENIX_ADMIN_PASSCODE", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
              "PHOENIX_ALLOW_AUTOMATED_SMS"):
        monkeypatch.delenv(k, raising=False)
    import requests

    def boom(*a, **k):
        raise AssertionError("network call during UI test")
    monkeypatch.setattr(requests, "post", boom)
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


def app(city="new_orleans"):
    at = AppTest.from_file(APP, default_timeout=180)
    at.session_state["city"] = city
    at.session_state["section"] = SECTION
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def text(at):
    return " ".join(m.value for m in at.markdown) + " " + " ".join(x.value for x in at.warning) + " " + \
           " ".join(x.value for x in at.error) + " " + " ".join(x.value for x in at.info)


def test_public_view_without_passcode_has_no_admin_tools():
    at = app()
    t = text(at)
    assert "Current flood alerts" in t
    assert "Administrator access has not been set up" in t and "PHOENIX_" not in t
    assert len(at.tabs) == 0                                   # no recipient / SMS tabs
    assert not any(b.label == "Register" for b in at.button)


def test_login_required_and_wrong_passcode_rejected(monkeypatch):
    monkeypatch.setenv("PHOENIX_ADMIN_PASSCODE", "correct horse")
    at = app()
    assert len(at.tabs) == 0 and any(ti.label == "Administrator passcode" for ti in at.text_input)
    at.text_input[0].set_value("wrong")
    next(b for b in at.button if b.label == "Sign in").click()
    at.run()
    assert "Incorrect passcode." in text(at) and len(at.tabs) == 0


def test_correct_passcode_opens_admin_tools_and_sms_disabled_without_twilio(monkeypatch):
    monkeypatch.setenv("PHOENIX_ADMIN_PASSCODE", "correct horse")
    at = app()
    at.text_input[0].set_value("correct horse")
    next(b for b in at.button if b.label == "Sign in").click()
    at.run()
    labels = [t.label for t in at.tabs]
    assert labels[:2] == ["Alert dashboard", "Registered recipients"] and labels[-1] == "Setup & help"
    t = text(at)
    assert "SMS: Not configured" in t
    assert not any(b.label == "Send alerts" for b in at.button)
    for lbl in ("Retry failed messages", "Refresh delivery status", "Check connection (sends no message)"):
        assert next(b for b in at.button if b.label == lbl).disabled


def _visible_outside_setup(at) -> str:
    """All text in the admin area except the Setup & help tab, plus everything above the tabs."""
    parts = [text(at)]
    setup = at.tabs[-1]
    setup_txt = " ".join(m.value for m in setup.markdown) + " ".join(c.value for c in setup.code)
    parts = [p.replace(setup_txt, "") for p in parts]
    for tab in at.tabs[:-1]:
        parts += [m.value for m in tab.markdown] + [c.value for c in tab.code] + [x.value for x in tab.caption]
    whole = " ".join(parts)
    for m in setup.markdown:
        whole = whole.replace(m.value, "")
    return whole


def test_no_technical_details_outside_setup_tab(monkeypatch, tmp_path):
    monkeypatch.setenv("PHOENIX_ADMIN_PASSCODE", "pw")
    at = app()
    at.text_input[0].set_value("pw")
    next(b for b in at.button if b.label == "Sign in").click()
    at.run()
    visible = _visible_outside_setup(at)
    for bad in ("TWILIO_", "PHOENIX_ADMIN", "PHOENIX_ALLOW", "$env:", "phoenix_alerts.db", str(tmp_path)):
        assert bad not in visible, bad
    setup = at.tabs[-1]
    setup_txt = " ".join(m.value for m in setup.markdown) + " ".join(c.value for c in setup.code)
    assert "TWILIO_ACCOUNT_SID" in setup_txt                    # documentation lives here
    assert str(tmp_path) not in setup_txt                        # never the full local path


def test_registered_numbers_never_rendered(monkeypatch):
    monkeypatch.setenv("PHOENIX_ADMIN_PASSCODE", "pw")
    from phoenix_core import recipient_manager as rm
    from phoenix_core.alerts_ui import get_store
    st.cache_resource.clear()
    store = get_store()
    rm.register(store, rm.RecipientInput("Ama", "+23276123456", "freetown", "Kroo Town", "ward", 8.49, -13.24, 2,
                                         True, True, rm.CONSENT_METHODS[0]))
    at = app("freetown")
    at.text_input[0].set_value("pw")
    next(b for b in at.button if b.label == "Sign in").click()
    at.run()
    everything = text(at) + " ".join(df.value.to_string() for df in at.dataframe) + \
        " ".join(str(sb.options) for sb in at.selectbox)
    assert "76123456" not in everything and "Ama" in everything


def test_lockout_after_repeated_failures(monkeypatch):
    monkeypatch.setenv("PHOENIX_ADMIN_PASSCODE", "pw")
    at = app()
    for _ in range(5):
        at.text_input[0].set_value("nope")
        next(b for b in at.button if b.label == "Sign in").click()
        at.run()
    assert "Too many failed sign-in attempts" in text(at)
    at.run()                                                  # still locked on the next visit
    assert "Too many failed sign-in attempts" in text(at) and not any(b.label == "Sign in" for b in at.button)


def test_other_sections_remain_public():
    for sec in ("Overview", "Infrastructure Exposure"):
        at = AppTest.from_file(APP, default_timeout=180)
        at.session_state["city"] = "miami"
        at.session_state["section"] = sec
        at.run()
        assert not at.exception and "Administrator" not in text(at)


def test_send_test_sms_flow_with_faked_twilio(monkeypatch):
    """Full on-screen flow; Twilio HTTP is faked, so nothing is sent."""
    import requests

    class R:
        def __init__(self, code, p):
            self.status_code, self._p, self.content, self.text = code, p, b"x", ""

        def json(self):
            return self._p
    posts = []
    monkeypatch.setattr(requests, "post", lambda url, auth, timeout, data: posts.append(data) or
                        R(201, {"sid": "SM1", "status": "queued"}))
    monkeypatch.setattr(requests, "get", lambda url, **k: R(200, {"status": "active", "type": "Full",
                                                                  "incoming_phone_numbers": [{"capabilities": {"sms": True}}]}))
    monkeypatch.setenv("PHOENIX_ADMIN_PASSCODE", "pw")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "3" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok-xyz-123")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15045550000")
    from phoenix_core import recipient_manager as rm
    from phoenix_core.alerts_ui import get_store
    st.cache_resource.clear()
    rm.register(get_store(), rm.RecipientInput("Demo Volunteer", "+15045550177", "new_orleans", "Mid-City", "point",
                                               29.95, -90.07, 1, True, True, rm.CONSENT_METHODS[0]))
    at = app()
    at.text_input[0].set_value("pw")
    next(b for b in at.button if b.label == "Sign in").click()
    at.run()
    send = lambda: next(b for b in at.button if b.label == "Send Test SMS")  # noqa: E731
    send().click(); at.run()                                   # no approval -> refused
    assert posts == [] and "Not sent" in text(at)
    next(c for c in at.checkbox if c.label.startswith("I approve sending ONE")).check()
    next(t for t in at.text_input if t.label == "Type TEST to confirm").set_value("TEST")
    send().click(); at.run()
    assert len(posts) == 1 and posts[0]["To"] == "+15045550177"
    assert posts[0]["Body"].startswith("PHOENIX DEMONSTRATION - not an official emergency alert.")
    assert "Accepted by Twilio" in " ".join(x.value for x in at.success)
    # second approved attempt within the block window is refused
    next(c for c in at.checkbox if c.label.startswith("I approve sending ONE")).check()
    next(t for t in at.text_input if t.label == "Type TEST to confirm").set_value("TEST")
    send().click(); at.run()
    assert len(posts) == 1 and "already sent to this person" in text(at)
    page = text(at) + " ".join(df.value.to_string() for df in at.dataframe)
    for secret in ("tok-xyz-123", "3333333333", "5045550177", "5045550000"):
        assert secret not in page
