"""Streamlit UI for "Flood Alerts & SMS Notifications". Called from app.py; holds no business logic.

Safety in the UI:
  * Nothing is sent when the page opens or a control changes: every send is a button press, and group
    sends and test sends need an explicit confirmation step plus typing SEND / TEST.
  * Phone numbers are only ever shown masked.
  * Administrator sign-in is mandatory; without a configured passcode the admin tools are disabled.
  * No secrets, setting names or recipient numbers are shown in the normal interface; technical setup lives in
    the administrator-only "Setup & help" tab, and provider error text is redacted before it is stored or shown.
    The public part shows only current alerts (no recipient data) and the system status.
  * There is no simulation mode: sending uses Twilio and is disabled until Twilio is configured.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from . import alert_monitor, alerts, data, maps, theme
from . import recipient_manager as rm
from .alert_store import AlertStore, db_path, parse_ts, ROOT
from .cities import CITIES
from .sms_service import automated_real_sms_allowed, explain_error, get_provider, get_setting, redact, setting_source

LEVEL_OPTS = list(rm.LEVELS)
STATUS_LABEL = {"queued": "Accepted by Twilio (queued)", "accepted": "Accepted by Twilio", "sent": "Sent to carrier",
                "delivered": "Delivered (confirmed)", "failed": "Failed", "undelivered": "Undelivered",
                "skipped": "Not sent (rule)"}
SKIP_LABEL = {"no_consent": "no consent", "opted_out": "opted out", "sms_disabled": "SMS disabled",
              "below_preference": "below person's chosen level", "duplicate": "already sent for this event",
              "cooldown": "cooldown period", "daily_cap": "daily message cap", "recipient_deleted": "recipient deleted"}


@st.cache_resource
def get_store() -> AlertStore:
    return AlertStore()


@st.cache_resource
def get_monitor() -> alert_monitor.MonitorThread:
    return alert_monitor.MonitorThread(get_store(), get_provider)


@st.cache_data(show_spinner=False)
def ward_points() -> dict:
    """Freetown ward name -> representative point (lat, lon)."""
    from shapely.geometry import shape
    out = {}
    for f in data.read_geojson(data.FREETOWN_PROCESSED / "wards.geojson")["features"]:
        p = shape(f["geometry"]).representative_point()
        name = f["properties"]["ward"]
        out[name if name not in out else f"{name} ({f['properties'].get('area')})"] = (p.y, p.x)
    return dict(sorted(out.items()))


def fmt_time(iso: str | None) -> str:
    d = parse_ts(iso)
    if not d:
        return "never"
    mins = (datetime.now(timezone.utc) - d).total_seconds() / 60
    ago = f"{mins:.0f} min ago" if mins < 120 else f"{mins / 60:.1f} h ago"
    return f"{d.astimezone().strftime('%Y-%m-%d %H:%M')} ({ago})"


def fmt_ago(iso: str | None) -> str:
    return fmt_time(iso).split("(")[-1].rstrip(")") if parse_ts(iso) else "never"


MAX_ATTEMPTS, LOCK_MINUTES, SESSION_MINUTES = 5, 15, 60


@st.cache_resource
def _login_guard() -> dict:
    """Server-wide failed-attempt counter (survives page reloads / new browser sessions)."""
    return {"fails": 0, "locked_until": None}


def _gate() -> bool:
    """Administrator login. Mandatory: without PHOENIX_ADMIN_PASSCODE the admin tools stay disabled."""
    code = get_setting("PHOENIX_ADMIN_PASSCODE")
    if not code:
        st.info("Administrator access has not been set up on this server, so recipient management and SMS tools "
                "are unavailable. See *Administrator setup* in the project README.")
        return False
    now = datetime.now(timezone.utc)
    t = st.session_state.get("alerts_admin_at")
    if t and (now - t).total_seconds() < SESSION_MINUTES * 60:
        st.session_state["alerts_admin_at"] = now          # sliding expiry
        c1, c2 = st.columns([5, 1])
        c1.caption(f"Signed in as administrator (session ends after {SESSION_MINUTES} min of inactivity).")
        if c2.button("Sign out"):
            st.session_state.pop("alerts_admin_at", None)
            st.rerun()
        return True
    st.session_state.pop("alerts_admin_at", None)
    g = _login_guard()
    if g["locked_until"] and now < g["locked_until"]:
        st.error(f"Too many failed sign-in attempts. Try again after {g['locked_until'].astimezone().strftime('%H:%M')}.")
        return False
    st.markdown("### Administrator sign-in")
    st.caption("Recipient details and SMS operations are restricted to administrators.")
    with st.form("admin_gate"):
        entered = st.text_input("Administrator passcode", type="password")
        if st.form_submit_button("Sign in"):
            if hmac.compare_digest(entered.encode(), code.encode()):
                g["fails"], g["locked_until"] = 0, None
                st.session_state["alerts_admin_at"] = now
                st.rerun()
            g["fails"] += 1
            if g["fails"] >= MAX_ATTEMPTS:
                from datetime import timedelta
                g["locked_until"], g["fails"] = now + timedelta(minutes=LOCK_MINUTES), 0
                st.error(f"Too many failed sign-in attempts. Sign-in is locked for {LOCK_MINUTES} minutes.")
            else:
                st.error("Incorrect passcode.")
    return False


@st.cache_data(ttl=600, show_spinner=False)
def _cached_connection_check() -> tuple[bool, str]:
    """Account / sender check with Twilio (sends no message). Cached 10 min; no secrets in the cache key."""
    return get_provider().verify()


def connection_status(store, provider) -> tuple[str, str, str]:
    """(label, chip tone, short detail) - one of Connected / Not configured / Connection error."""
    ok, _ = provider.configured()
    if not ok:
        return "Not configured", "bad", "Twilio credentials are not configured on this server."
    vok, vmsg = _cached_connection_check()
    if vok:
        if not store.settings().get("twilio_verified_at"):
            store.set_settings(twilio_verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               twilio_verified_note=vmsg)
        return "Connected", "ok", vmsg
    return "Connection error", "bad", vmsg


def status_strip(store, provider):
    """One compact line of operational status, shown at the top of the admin area."""
    label, tone, detail = connection_status(store, provider)
    sender = provider.sender_masked if provider.configured()[0] else "—"
    mons = alert_monitor.monitor_status(store)
    alive = [m for m in mons if m["alive"]]
    auto_ok, auto_why = alert_monitor.may_auto_send(store, provider)
    last_ok = store.last_run(successful_only=True)
    st.markdown(
        theme.chip(f"SMS: {label}", tone) + theme.chip(f"Sender: {sender}") +
        theme.chip("Monitoring: running" if alive else "Monitoring: stopped", "ok" if alive else "") +
        theme.chip("Auto-send: on" if auto_ok else "Auto-send: off", "warn" if auto_ok else "") +
        theme.chip(f"Last complete check: {fmt_ago(last_ok['finished_at']) if last_ok else 'never'}"),
        unsafe_allow_html=True)
    if label != "Connected":
        st.caption(("SMS sending is unavailable until Twilio is connected; alerts can still be generated and reviewed. "
                    if label == "Not configured" else f"{detail} ") + "Details in *Setup & help*.")


def _recipient_label(r: dict) -> str:
    return f"{r['name']} · {rm.mask_phone(r['phone'])} · {CITIES[r['city']].name} - {r['area_label']}"


# ---------------------------------------------------------------------------------------------- tabs
def tab_dashboard(store, provider, slug):
    s = alerts.dashboard_stats(store)
    mons = alert_monitor.monitor_status(store)
    alive = [m for m in mons if m["alive"]]
    last_ok = store.last_run(successful_only=True)
    last_any = store.last_run()
    st.markdown(theme.cards([
        ("Registered recipients", s["recipients"], f"{s['opted_out']} opted out", "cyan"),
        ("Active subscribers", s["active"], "consented, SMS on, not opted out", "accent"),
        ("Alerts generated", s["alerts"], "excluding test alerts", "accent"),
        ("SMS attempted", s["attempted"], "submitted to Twilio", "accent"),
        ("Delivered (confirmed)", s["delivered"], f"{s['awaiting']} awaiting provider confirmation", "accent"),
        ("Failed deliveries", s["failed"], f"{s['skipped']} withheld by safety rules", "alert"),
        ("Last successful check", fmt_ago(last_ok["finished_at"]) if last_ok else "never",
         (fmt_time(last_ok["finished_at"]).split(" (")[0] + " · all sources answered" if last_ok else
          "no complete check yet") +
         (f" · last attempt {fmt_ago(last_any['finished_at'])} failed" if last_any and not last_any["ok"] else ""), "cyan"),
        ("Monitoring", "Active" if alive else "Not running",
         ", ".join(f"{m['runner']} every {m['interval_minutes']:.0f} min" for m in alive) or "start it below", "cyan"),
    ]), unsafe_allow_html=True)

    st.markdown("### Monitoring")
    mon = get_monitor()
    c1, c2, c3 = st.columns([1, 1, 2])
    if c1.button("Check sources now", help="Fetches official alerts and forecasts for registered locations. "
                                           "Generates alerts for review. Never sends SMS."):
        with st.spinner("Checking NWS alerts and rainfall forecasts…"):
            res = alert_monitor.run_check(store, provider, runner="manual", send=False)
        if res["ok"]:
            ev = res["evaluation"]
            failed = res["failed_sources"]
            if not ev["sources"]:
                st.info("No active recipients registered, so there was nothing to check.")
            msg = (f"Check finished: {len(ev['new_alert_ids'])} new alert(s), {len(set(ev['alert_ids']))} current. "
                   "Nothing was sent.")
            if failed:
                st.warning(f"Check incomplete: {failed} data source request(s) could not be reached. "
                           "No alerts found does not mean no risk. Nothing was sent.")
            else:
                st.success(msg)
            with st.expander("What was checked"):
                rows = []
                for c, v in ev["sources"].items():
                    nws = v.get("nws")
                    fc = v.get("forecast", [])
                    rows.append({"City": CITIES[c].name if c in CITIES else c,
                                 "Registered locations": v.get("recipients", 0),
                                 "Official warnings (NWS)": (f"{nws.get('ok_queries', 0)} checked, "
                                                             f"{nws.get('failed_queries', 0)} unreachable, "
                                                             f"{nws.get('flood_alerts', 0)} flood alert(s)")
                                 if isinstance(nws, dict) else "No official feed for this country",
                                 "Rainfall forecast": (f"{sum(1 for f in fc if f.get('ok'))} of {len(fc)} areas checked"
                                                       if fc else "-")})
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.error("The check could not be completed. Technical details are in *Setup & help*.")
    if mon.running:
        if c2.button("Stop app monitor"):
            mon.stop()
            st.rerun()
    else:
        if c2.button("Start app monitor", help="Checks every N minutes while this app is running."):
            mon.start()
            st.rerun()
    ok_auto, why = alert_monitor.may_auto_send(store, provider)
    c3.markdown(theme.chip("Auto-send on" if ok_auto else "Auto-send off", "warn" if ok_auto else "")
                + f"<span class='px-muted'> {theme.esc(why)}</span>", unsafe_allow_html=True)
    if mons:
        st.dataframe(pd.DataFrame([{"Monitor": m["runner"], "State": m["state"],
                                    "Interval (min)": m["interval_minutes"], "Last heartbeat": fmt_time(m["heartbeat_at"]),
                                    "Auto-send": "on" if m["auto_send"] else "off"} for m in mons]),
                     width="stretch", hide_index=True)
    st.caption("The in-app monitor runs only while PHOENIX is running. Continuous monitoring uses the separate "
               "monitor service (see *Setup & help*).")

    st.markdown("### Current alerts awaiting a decision")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = store.q("SELECT * FROM alerts WHERE is_test=0 AND (expires_at IS NULL OR expires_at > ?) ORDER BY level DESC, id DESC",
                  (now,))
    if not cur:
        st.info("No current alerts. Use *Check sources now* or start a monitor.")
    else:
        st.dataframe(pd.DataFrame([{"ID": a["id"], "City": CITIES[a["city"]].name, "Area": a["area_label"],
                                    "Category": a["category"], "Type": a["source_type"].replace("_", " "),
                                    "Source": a["source"], "Generated": fmt_time(a["created_at"]),
                                    "Expires": fmt_time(a["expires_at"]) if a["expires_at"] else "-"} for a in cur]),
                     width="stretch", hide_index=True)
        pv = alerts.preview_recipients(store, [a["id"] for a in cur])
        n_send = int(pv["would_send"].sum()) if not pv.empty else 0
        st.markdown(f"**{n_send}** message(s) would be sent now. Others are withheld by consent, preference, duplicate, "
                    "cooldown or daily-cap rules.")
        if not pv.empty:
            pv["reason_if_not"] = pv["reason_if_not"].map(lambda r: SKIP_LABEL.get(r, r))
            st.dataframe(pv.rename(columns={"alert_id": "Alert", "category": "Category", "recipient": "Recipient",
                                            "phone": "Phone", "would_send": "Will send", "reason_if_not": "Withheld because"}),
                         width="stretch", hide_index=True)
        prov_ok = provider.configured()[0]
        if n_send and not prov_ok:
            st.caption(f"{n_send} message(s) ready - sending is unavailable until Twilio is connected.")
        if n_send and prov_ok:
            with st.form("send_real"):
                confirm = st.checkbox(f"I have reviewed these alerts and want to send {n_send} real SMS message(s) "
                                      "through Twilio.")
                typed = st.text_input("Type SEND to confirm")
                if st.form_submit_button("Send alerts", type="primary"):
                    if not confirm or typed.strip() != "SEND":
                        st.error("Not sent: tick the confirmation and type SEND.")
                    else:
                        out = alerts.dispatch(store, provider, alert_ids=[a["id"] for a in cur])
                        st.success(f"Done: {out['sent']} accepted by provider, {out['failed']} failed, "
                                   f"{out['skipped']} withheld by rules.")
    c1, c2 = st.columns(2)
    prov_ok = provider.configured()[0]
    if c1.button("Retry failed messages", disabled=not prov_ok):
        r = alerts.retry_failed(store, provider)
        st.info(f"Retried {r['retried']}: {r['sent']} accepted by Twilio, {r['failed']} failed again, "
                f"{r['gave_up']} not retried (permanent error or attempt limit).")
    if c2.button("Refresh delivery status", disabled=not prov_ok,
                 help="Asks Twilio for the latest status of messages it accepted."):
        st.info(f"{alerts.refresh_statuses(store, provider)} delivery status update(s) received from Twilio.")


def tab_recipients(store, slug):
    st.markdown("### Register a person")
    st.caption("Collect only what is needed: a name or nickname, an international phone number, and an approximate "
               "location (rounded to about 100 m). Explicit consent is required.")
    city = st.selectbox("City", list(CITIES), index=list(CITIES).index(slug),
                        format_func=lambda s: CITIES[s].name, key="reg_city")
    c = CITIES[city]
    if city == "freetown":
        mode = st.radio("Location", ["Ward", "Coordinates"], horizontal=True, key="reg_mode_ft")
    else:
        mode = st.radio("Location", ["Neighbourhood + coordinates", "Whole city (city centre)"], horizontal=True,
                        key="reg_mode_us")
    with st.form("register", clear_on_submit=True):
        name = st.text_input("Name or nickname")
        phone = st.text_input("Mobile number (international format)",
                              placeholder="+232 76 123456" if city == "freetown" else "+1 504 555 0123")
        if mode == "Ward":
            wp = ward_points()
            area = st.selectbox("Ward", list(wp))
            lat, lon = wp[area]
            zone = "ward"
        elif mode == "Whole city (city centre)":
            area, (lat, lon), zone = f"{c.name} (whole city)", c.center, "city"
        else:
            area = st.text_input("Neighbourhood or area label", placeholder="e.g. Mid-City" if city != "freetown" else "e.g. Kroo Bay")
            cc1, cc2 = st.columns(2)
            lat = cc1.number_input("Latitude (approx.)", value=float(c.center[0]), format="%.3f")
            lon = cc2.number_input("Longitude (approx.)", value=float(c.center[1]), format="%.3f")
            zone = "point"
        lvl = st.selectbox("Send alerts at or above", LEVEL_OPTS, index=1, format_func=lambda l: rm.LEVELS[l])
        sms_on = st.checkbox("SMS notifications enabled", value=True)
        st.markdown("<span class='px-muted'>Consent statement read to / accepted by the person: <i>\"I agree to receive "
                    "flood information text messages from PHOENIX (not a government service) about my area. Message "
                    "frequency varies. Reply STOP to opt out at any time, HELP for help. Standard message rates may "
                    "apply.\"</i></span>", unsafe_allow_html=True)
        consent = st.checkbox("The person has explicitly agreed to the statement above")
        method = st.selectbox("How consent was given", rm.CONSENT_METHODS)
        if st.form_submit_button("Register", type="primary"):
            try:
                sa = None
                try:
                    from shapely.geometry import Point
                    from .geo import union_of_features
                    sa = union_of_features(data.read_geojson(c.boundary_file)["features"])
                    if sa is not None and sa.buffer(0.05).contains(Point(lon, lat)) is False:
                        raise rm.ValidationError(f"That location is outside {c.name}'s study area.")
                except FileNotFoundError:
                    pass
                rid, warn = rm.register(store, rm.RecipientInput(name, phone, city, area or "", zone, lat, lon, lvl,
                                                                 sms_on, consent, method, "administrator"))
                st.success(f"Registered recipient #{rid}. No message has been sent.")
                if warn:
                    st.warning(warn)
            except rm.ValidationError as e:
                st.error(str(e))

    st.markdown("### Registered recipients")
    rows = rm.list_recipients(store)
    if not rows:
        st.info("No one is registered yet.")
        return
    st.dataframe(pd.DataFrame([{"ID": r["id"], "Name": r["name"], "Phone": rm.mask_phone(r["phone"]),
                                "City": CITIES[r["city"]].name, "Area": r["area_label"],
                                "Alert level ≥": rm.LEVELS[r["min_level"]], "SMS": "on" if r["sms_enabled"] else "off",
                                "Consent": f"{r['consent_method']} ({r['consent_at'][:10]})" if r["consent"] else "none",
                                "Status": "OPTED OUT" if r["opted_out"] else ("active" if rm.is_active(r) else "inactive")}
                               for r in rows]), width="stretch", hide_index=True)

    st.markdown("### Manage a recipient")
    pick = st.selectbox("Recipient", rows, format_func=_recipient_label, key="manage_pick")
    r = rm.get(store, pick["id"])
    m1, m2 = st.columns(2)
    with m1.form("upd"):
        nl = st.selectbox("Send alerts at or above", LEVEL_OPTS, index=LEVEL_OPTS.index(r["min_level"]),
                          format_func=lambda l: rm.LEVELS[l])
        on = st.checkbox("SMS notifications enabled", value=bool(r["sms_enabled"]))
        if st.form_submit_button("Save preferences"):
            rm.update(store, r["id"], min_level=nl, sms_enabled=on)
            st.success("Saved.")
    with m2:
        if r["opted_out"]:
            st.warning(f"Opted out {r['opted_out_at'][:16]} ({r['opt_out_source']}).")
            meth = st.selectbox("New consent method", rm.CONSENT_METHODS, key="reoptin")
            if st.checkbox("The person has asked to receive alerts again", key="reopt_ok") and st.button("Re-subscribe"):
                rm.opt_back_in(store, r["id"], meth)
                st.rerun()
        else:
            if st.button("Record opt-out (takes effect immediately)"):
                rm.opt_out(store, r["id"], "administrator")
                st.rerun()
        if st.checkbox("Delete this person's record", key="del_ok") and st.button("Delete permanently"):
            rm.delete(store, r["id"])
            st.rerun()
    with st.expander("Record an inbound text reply (STOP / START / HELP)"):
        st.caption("Record a reply the person sent (for example, seen in the Twilio console).")
        body = st.text_input("Reply text", key="inbound_body")
        if st.button("Process reply") and body:
            reply = rm.handle_inbound(store, r["phone"], body, store.settings()["help_contact"])
            st.info(f"Recorded. Reply the person should receive: {reply}" if reply else "No keyword recognised.")


def tab_zones(store, slug):
    c = CITIES[slug]
    st.markdown(f"### Alert zones · {c.name}")
    st.caption("Registered locations are approximate (about 100 m). Alerts are matched per location: NWS alerts are "
               "queried at each location; forecasts per ~5 km cell. Hazard maps give context only.")
    rs = rm.list_recipients(store, slug)
    m = maps.base_map(c.center, c.zoom)
    if c.boundary_file.exists():
        maps.add_boundary(m, data.read_geojson(c.boundary_file))
    if c.available_scenarios:
        s = c.available_scenarios[-1]
        disp = s.file.with_name(s.file.stem + "_display.geojson")
        maps.add_hazard(m, data.read_geojson(disp if disp.exists() else s.file), f"Hazard context: {s.label}",
                        "SFHA" if s.kind == "regulatory" else "modelled")
    if slug == "freetown":
        folium.GeoJson(data.read_geojson(data.FREETOWN_PROCESSED / "wards.geojson"), name="Wards",
                       style_function=lambda f: {"color": "#64748b", "weight": 0.8, "fillOpacity": 0},
                       tooltip=folium.GeoJsonTooltip(fields=["ward"], aliases=["Ward"])).add_to(m)
    zones: dict = {}
    for r in rs:
        z = zones.setdefault((r["area_label"], round(r["lat"], 3), round(r["lon"], 3), r["zone_type"]),
                             {"n": 0, "active": 0})
        z["n"] += 1
        z["active"] += rm.is_active(r)
    for (area, lat, lon, zt), z in zones.items():
        folium.CircleMarker([lat, lon], radius=6 + min(10, z["n"] * 2), color="#22d3ee", weight=2, fill=True,
                            fill_opacity=0.6, tooltip=f"{area}: {z['n']} registered ({z['active']} active)").add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, use_container_width=True, height=460, returned_objects=[], key=f"zones_{slug}")
    if zones:
        st.dataframe(pd.DataFrame([{"Zone": a, "Type": zt, "Registered": z["n"], "Active": z["active"],
                                    "Hazard context (scenario maps, not a forecast)":
                                        alerts.hazard_context(slug, lat, lon, zt, a)["text"]}
                                   for (a, lat, lon, zt), z in zones.items()]), width="stretch", hide_index=True)
    else:
        st.info(f"No recipients registered in {c.name} yet.")


def tab_history(store, slug):
    st.markdown("### Alert history")
    only_city = st.checkbox("Only the selected city", value=False, key="hist_city")
    show_tests = st.checkbox("Include test alerts", value=False, key="hist_tests")
    q = "SELECT * FROM alerts WHERE 1=1" + ("" if show_tests else " AND is_test=0") + (" AND city=?" if only_city else "")
    rows = store.q(q + " ORDER BY id DESC LIMIT 500", (slug,) if only_city else ())
    if not rows:
        st.info("No alerts yet.")
    else:
        st.dataframe(pd.DataFrame([{"ID": a["id"], "Generated (UTC)": a["created_at"], "City": CITIES[a["city"]].name,
                                    "Area": a["area_label"], "Category": a["category"],
                                    "Type": "TEST" if a["is_test"] else a["source_type"].replace("_", " "),
                                    "Source": a["source"], "Escalated from": a["escalated_from"] or "",
                                    "Expires": a["expires_at"] or ""} for a in rows]), width="stretch", hide_index=True)
        a = st.selectbox("Details for alert", rows, format_func=lambda a: f"#{a['id']} {a['category']} {a['area_label'][:60]}")
        st.markdown(f"**Why:** {a['explanation']}\n\n**Precautions:** {a['precautions']}\n\n**SMS text:**")
        st.code(alerts.build_message(a, test=bool(a["is_test"])), language=None)
    st.markdown("### SMS delivery status")
    d = store.q("SELECT d.*, a.category, a.city FROM deliveries d LEFT JOIN alerts a ON a.id=d.alert_id "
                "ORDER BY d.id DESC LIMIT 500")
    if not d:
        st.info("No delivery attempts yet.")
        return
    st.dataframe(pd.DataFrame([{"Time (UTC)": x["created_at"], "Alert": x["alert_id"], "Category": x["category"],
                                "To": x["phone_masked"] or "", "Status": STATUS_LABEL.get(x["status"], x["status"]),
                                "Why withheld": SKIP_LABEL.get(x["skip_reason"], x["skip_reason"] or ""),
                                "Provider": x["provider"] or "", "Problem": explain_error(x["error_code"]),
                                "Attempts": x["attempts"], "Test": "yes" if x["is_test"] else ""} for x in d]),
                 width="stretch", hide_index=True)


DEMO_STYLE = "PHOENIX demonstration message (recommended for live demos)"
STYLES = [DEMO_STYLE, "Sample official warning (as relayed from NWS)", "Sample PHOENIX forecast notice"]


def tab_test(store, provider, slug):
    st.markdown(theme.note(
        "<b>Send Test SMS.</b> One message to <b>one</b> person who has agreed to receive it, only after you approve "
        f"it. Test messages are never sent to groups, and the same person cannot receive another test within "
        f"{alerts.TEST_RESEND_MINUTES} minutes."), unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    city = c1.selectbox("City", list(CITIES), index=list(CITIES).index(slug), format_func=lambda s: CITIES[s].name,
                        key="t_city")
    style = c2.selectbox("Message", STYLES, key="t_style")
    lvl = 2
    if style != DEMO_STYLE:
        lvl = st.selectbox("Sample alert category", LEVEL_OPTS, index=2, format_func=lambda l: rm.LEVELS[l], key="t_lvl")
    rs = [r for r in rm.list_recipients(store, city) if rm.is_active(r)]
    area = CITIES[city].name
    pick = None
    if rs:
        pick = st.selectbox("Recipient", rs, format_func=_recipient_label, key="t_rec")
        area = pick["area_label"]
    else:
        st.info("No consenting recipient with SMS enabled in this city yet. Register one under *Registered recipients*; "
                "you can still preview the message.")
    sample = alerts.demo_alert(city, area) if style == DEMO_STYLE else \
        alerts.sample_alert(city, lvl, area, style.startswith("Sample official"))
    msg = alerts.build_message(sample, test=True)
    st.markdown("**Exact message the person will receive**")
    st.code(msg, language=None)
    st.caption(f"{len(msg)} characters. Sender: {provider.sender_masked or 'not configured'}.")
    if not pick:
        return
    if not provider.configured()[0]:
        st.caption("Sending is unavailable until Twilio is connected. The preview above is the exact text.")
        return
    last = alerts.last_test_send(store, pick["id"])
    if last:
        st.caption(f"Last test to this person: {fmt_time(last['created_at'])} - "
                   f"{STATUS_LABEL.get(last['status'], last['status'])}.")
    with st.form("test_send", clear_on_submit=True):   # confirmation resets after every attempt
        ok = st.checkbox(f"I approve sending ONE real SMS to {rm.mask_phone(pick['phone'])} ({pick['name']}), who has "
                         "agreed to receive a test message")
        typed = st.text_input("Type TEST to confirm")
        if st.form_submit_button("Send Test SMS", type="primary"):
            if not ok or typed.strip() != "TEST":
                st.error("Not sent: tick the approval box and type TEST.")
            else:
                try:
                    last = alerts.last_test_send(store, pick["id"])   # re-check right before sending
                    aid = alerts.make_test_alert(store, city, lvl, area, style.startswith("Sample official"),
                                                 demonstration=(style == DEMO_STYLE))
                    res = alerts.send_test(store, provider, aid, pick["id"])
                    d = store.one("SELECT status, error_code FROM deliveries WHERE is_test=1 AND recipient_id=? "
                                  "ORDER BY id DESC LIMIT 1", (pick["id"],))
                    if res == "sent":
                        st.success(f"Accepted by Twilio (status: {STATUS_LABEL.get(d['status'], d['status'])}). "
                                   "Delivery is confirmed only after Twilio reports it: use *Refresh delivery "
                                   "status* on the Alert dashboard.")
                    else:
                        st.error("Failed: Twilio did not accept the message - " +
                                 (explain_error(d["error_code"]) or "unknown error") + ".")
                except alerts.RepeatSendBlocked as e:
                    st.warning(str(e))
                except (ValueError, alerts.ProviderNotConfigured) as e:
                    st.error(redact(e))


def tab_settings(store, provider):
    st_ = store.settings()
    with st.form("settings"):
        st.markdown("**Notification rules**")
        c1, c2, c3 = st.columns(3)
        cooldown = c1.number_input("Cooldown per person (minutes)", 0, 1440, int(st_["cooldown_minutes"]),
                                   help="No further message to the same person within this time unless the risk "
                                        "category goes up (escalation).")
        cap = c2.number_input("Max messages per person per 24 h", 1, 20, int(st_["max_messages_per_day"]),
                              help="Critical official warnings are exempt.")
        att = c3.number_input("Max send attempts", 1, 5, int(st_["max_attempts"]))
        st.markdown("**PHOENIX rainfall-forecast notices**")
        fe = st.checkbox("Generate forecast notices (never above ELEVATED, always marked 'not an official warning')",
                         value=bool(st_["forecast_alerts_enabled"]))
        thr = st.number_input("Screening threshold: forecast rain in next 24 h (mm)", 5.0, 500.0,
                              float(st_["forecast_threshold_mm_24h"]), step=5.0,
                              help="A local administrative choice to decide when to inform people. It is NOT a "
                                   "scientifically validated flood threshold; set it with local experts.")
        st.markdown("**Monitoring**")
        c1, c2 = st.columns(2)
        interval = c1.number_input("Monitoring interval (minutes)", 5, 1440, int(st_["monitor_interval_minutes"]))
        auto = c2.checkbox("Send automatically when the monitor finds a new alert", value=bool(st_["auto_send"]),
                           help="Off by default. Also needs server approval for unattended sending (see Setup & help).")
        helpc = st.text_input("HELP contact (agency phone or email shown in HELP replies)", st_["help_contact"])
        if st.form_submit_button("Save settings", type="primary"):
            store.set_settings(cooldown_minutes=int(cooldown), max_messages_per_day=int(cap), max_attempts=int(att),
                               forecast_alerts_enabled=bool(fe), forecast_threshold_mm_24h=float(thr),
                               monitor_interval_minutes=int(interval), auto_send=bool(auto), help_contact=helpc.strip())
            st.success("Saved. The app monitor picks up the new interval after its current wait.")
    st.markdown("**SMS connection**")
    label, tone, detail = connection_status(store, provider)
    st.markdown(theme.chip(label, tone) + theme.chip(f"Sender: {provider.sender_masked or '—'}")
                + f"<span class='px-muted'> {theme.esc(detail)}</span>", unsafe_allow_html=True)
    if st.button("Check connection (sends no message)", disabled=not provider.configured()[0]):
        _cached_connection_check.clear()
        vok, vmsg = _cached_connection_check()
        if vok:
            store.set_settings(twilio_verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               twilio_verified_note=vmsg)
            st.success(vmsg)
        else:
            st.error(vmsg)
    st.markdown("**System status**")
    st.dataframe(pd.DataFrame(alerts.capability_status(store, provider, alert_monitor.monitor_status(store))),
                 width="stretch", hide_index=True)


def tab_setup(store, provider):
    """Administrator-only technical documentation: setup, credentials, backup, troubleshooting."""
    st.caption("Technical reference for administrators. Credentials are read from the server environment only and "
               "are never shown, stored in the database or written to project files.")
    missing = provider.missing_settings()
    with st.expander("SMS delivery (Twilio) setup", expanded=bool(missing)):
        st.markdown("Set these on the server **before** starting PHOENIX, in the same terminal:")
        st.code('$env:TWILIO_ACCOUNT_SID="AC..."\n$env:TWILIO_AUTH_TOKEN="..."\n$env:TWILIO_PHONE_NUMBER="+1..."\n'
                '# optional, e.g. for US A2P 10DLC:\n$env:TWILIO_MESSAGING_SERVICE_SID="MG..."\n'
                'streamlit run app.py', language="powershell")
        st.markdown("**On Streamlit Community Cloud** add them under *App settings > Secrets* instead "
                    "(never in a file committed to Git):")
        st.code('[twilio]\naccount_sid = "AC..."\nauth_token = "..."\nphone_number = "+1..."\n\n'
                '[phoenix]\nadmin_passcode = "choose-a-strong-passcode"', language="toml")
        st.markdown(("**Missing on this server:** " + ", ".join(f"`{m}`" for m in missing)) if missing else
                    "All required Twilio settings are present.")
        st.dataframe(pd.DataFrame([{"Setting": n, "Read from": setting_source(n)} for n in (
            "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TWILIO_MESSAGING_SERVICE_SID",
            "PHOENIX_ADMIN_PASSCODE", "PHOENIX_ALLOW_AUTOMATED_SMS")]), width="stretch", hide_index=True)
        st.caption("Values are never displayed. Environment variables take priority over Streamlit secrets.")
        st.markdown("- US numbers need A2P 10DLC registration (or a verified toll-free number).\n"
                    "- Check Twilio availability and sender-ID rules for Sierra Leone (+232).\n"
                    "- Trial accounts deliver only to verified numbers and add a trial prefix.\n"
                    "- Never put credentials in code, in project files or in Git.")
    with st.expander("Administrator access"):
        st.code('$env:PHOENIX_ADMIN_PASSCODE="choose-a-strong-passcode"', language="powershell")
        st.markdown(f"Sessions end after {SESSION_MINUTES} minutes of inactivity; {MAX_ATTEMPTS} wrong passcodes lock "
                    f"sign-in for {LOCK_MINUTES} minutes.")
    with st.expander("Monitoring service and automatic sending"):
        st.markdown("The in-app monitor stops when PHOENIX stops. For continuous monitoring run the monitor service "
                    "(for example from Windows Task Scheduler at log-on):")
        st.code("python scripts/run_alert_monitor.py          # loop; Ctrl+C to stop\n"
                "python scripts/run_alert_monitor.py --once   # one check, never sends", language="powershell")
        st.markdown("Unattended automatic sending requires **all** of: the *auto send* setting, a Twilio connection, "
                    "and server approval:")
        st.code('$env:PHOENIX_ALLOW_AUTOMATED_SMS="1"', language="powershell")
        st.markdown(f"Server approval for unattended sending is currently **{'on' if automated_real_sms_allowed() else 'off'}**.")
    with st.expander("Data storage and backup"):
        try:
            rel = db_path().resolve().relative_to(ROOT)
        except ValueError:
            rel = "custom location (PHOENIX_ALERTS_DB)"
        st.markdown(f"Recipients, alerts and delivery records are stored locally in `{rel}` inside the project "
                    "folder. That folder is excluded from Git. To back up, stop PHOENIX and the monitor service, then "
                    "copy the file; keep backups as securely as the original because they contain phone numbers.")
    with st.expander("Troubleshooting"):
        runs = store.q("SELECT finished_at, runner, error FROM monitor_runs WHERE ok=0 ORDER BY id DESC LIMIT 10")
        fails = store.q("SELECT updated_at, error_code, error_message FROM deliveries WHERE status IN "
                        "('failed','undelivered') ORDER BY id DESC LIMIT 10")
        st.markdown("**Recent incomplete checks**")
        st.dataframe(pd.DataFrame([{"Time (UTC)": r["finished_at"], "Monitor": r["runner"],
                                    "Detail": redact(r["error"])} for r in runs]) if runs else
                     pd.DataFrame({"Detail": ["none"]}), width="stretch", hide_index=True)
        st.markdown("**Recent delivery problems** (provider text redacted)")
        st.dataframe(pd.DataFrame([{"Time (UTC)": f["updated_at"], "Problem": explain_error(f["error_code"]),
                                    "Code": f["error_code"], "Provider detail": redact(f["error_message"])} for f in fails])
                     if fails else pd.DataFrame({"Problem": ["none"]}), width="stretch", hide_index=True)
        st.markdown("- *Incomplete check*: the weather or forecast service could not be reached; try again later.\n"
                    "- *Connection error*: check the credentials, the account status and the sender number in Twilio.\n"
                    "- *Filtered by carrier / sender not registered*: complete Twilio's registration for that country.")


# ---------------------------------------------------------------------------------------------- public view
def public_panel(store, provider, slug):
    """Non-sensitive information anyone may see: current alerts (no recipient data) and system status."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = store.q("SELECT * FROM alerts WHERE is_test=0 AND (expires_at IS NULL OR expires_at > ?) ORDER BY level DESC, id DESC",
                  (now,))
    st.markdown("### Current flood alerts")
    if not cur:
        last = store.last_run(successful_only=True)
        st.info("No current flood alerts for registered PHOENIX areas" +
                (f" (last complete check {fmt_time(last['finished_at'])})." if last else
                 ". No complete check has been run yet on this installation, so this is not an all-clear."))
    else:
        st.dataframe(pd.DataFrame([{
            "City": CITIES[a["city"]].name, "Category": a["category"],
            "Type": "Official warning" if a["source_type"] == "official_warning" else "PHOENIX forecast notice (not official)",
            # forecast-notice areas are labels of where subscribers live, so only official areas are shown publicly
            "Area": a["area_label"] if a["source_type"] == "official_warning" else f"Parts of {CITIES[a['city']].name}",
            "Source": a["source"], "Issued (UTC)": a["created_at"], "Until": a["expires_at"] or "",
            "Why": a["explanation"]} for a in cur]), width="stretch", hide_index=True)
    st.caption("For official warnings always follow your national weather service and emergency management agency.")
    with st.expander("System status (what is operational on this installation)"):
        rows = alerts.capability_status(store, provider, alert_monitor.monitor_status(store), public=True)
        st.dataframe(pd.DataFrame([{"Capability": r["Capability"], "Status": r["Status"]} for r in rows]),
                     width="stretch", hide_index=True)


# ---------------------------------------------------------------------------------------------- entry point
# ---------------------------------------------------------------------------------------------- entry point
def render(slug: str):
    st.markdown(theme.note(
        "<b>Flood Alerts & SMS Notifications.</b> PHOENIX relays <b>official warnings</b> (US National Weather Service) "
        "and issues clearly-labelled <b>forecast notices</b> by SMS to people who have explicitly opted in. Messages "
        "identify PHOENIX as the sender, never claim to be from a government agency, and always include STOP / HELP."),
        unsafe_allow_html=True)
    store, provider = get_store(), get_provider()
    public_panel(store, provider, slug)
    st.divider()
    if not _gate():
        return
    status_strip(store, provider)
    tabs = st.tabs(["Alert dashboard", "Registered recipients", "Geographic alert zones", "Alert history & delivery",
                    "Test notification", "Notification settings", "Setup & help"])
    with tabs[0]:
        tab_dashboard(store, provider, slug)
    with tabs[1]:
        tab_recipients(store, slug)
    with tabs[2]:
        tab_zones(store, slug)
    with tabs[3]:
        tab_history(store, slug)
    with tabs[4]:
        tab_test(store, provider, slug)
    with tabs[5]:
        tab_settings(store, provider)
    with tabs[6]:
        tab_setup(store, provider)
