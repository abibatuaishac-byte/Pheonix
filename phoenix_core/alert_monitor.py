"""Monitoring: periodically check alert sources and (optionally) send.

Three ways to run a check - all use run_check() and the same database:
  1. Manual   - "Check sources now" button in the app (never sends by itself).
  2. App      - MonitorThread, started from the app. Runs ONLY while the Streamlit server process runs.
  3. Worker   - `python scripts/run_alert_monitor.py`, a separate process for persistent monitoring
                (e.g. under Windows Task Scheduler, systemd or a cloud worker). Runs only while that process runs.

Nothing keeps checking once these processes stop. The dashboard decides "active" from heartbeats, so a
crashed or stopped monitor shows as inactive instead of pretending to run.

Automatic sending: only if the administrator enabled auto_send, Twilio is configured, AND
PHOENIX_ALLOW_AUTOMATED_SMS=1 is set (a deliberate second switch for unattended real SMS).
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta, timezone

from . import alerts
from .alert_store import AlertStore, parse_ts
from .sms_service import automated_real_sms_allowed, redact


def may_auto_send(store: AlertStore, provider) -> tuple[bool, str]:
    st = store.settings()
    if not st["auto_send"]:
        return False, "Automatic sending is off (alerts are generated for review only)."
    ok, msg = provider.configured()
    if not ok:
        return False, "Automatic sending needs a Twilio connection."
    if getattr(provider, "real", False) and not automated_real_sms_allowed():
        return False, "Automatic sending is not approved on this server (see Setup & help)."
    return True, "Automatic sending enabled."


def failed_source_requests(sources: dict) -> int:
    n = 0
    for v in sources.values():
        if isinstance(v.get("nws"), dict):
            n += v["nws"].get("failed_queries", 0)
        n += sum(1 for f in v.get("forecast", []) if not f.get("ok"))
    return n


def run_check(store: AlertStore, provider, runner: str = "manual", send: bool | None = None, **fetchers) -> dict:
    """One monitoring cycle. send=None -> follow the auto-send rules; send=False -> never send."""
    run_id = store.start_run(runner)
    try:
        res = alerts.evaluate(store, **fetchers)
        sent = {"sent": 0, "failed": 0, "skipped": 0}
        allowed, why = may_auto_send(store, provider)
        if send is not False and allowed and res["alert_ids"]:
            sent = alerts.dispatch(store, provider, alert_ids=sorted(set(res["alert_ids"])))
        if getattr(provider, "real", False) and provider.configured()[0]:
            alerts.refresh_statuses(store, provider)
        failed = failed_source_requests(res["sources"])
        # a check only counts as successful if every source request worked
        store.finish_run(run_id, failed == 0, f"{failed} source request(s) failed" if failed else None,
                         res["sources"].keys(), len(res["new_alert_ids"]), sent["sent"] + sent["failed"], res["sources"])
        return {"ok": True, "complete": failed == 0, "failed_sources": failed, "run_id": run_id, "evaluation": res,
                "dispatch": sent, "auto_send": why}
    except Exception as exc:
        store.finish_run(run_id, False, redact(f"{type(exc).__name__}: {exc}")[:500])
        return {"ok": False, "run_id": run_id, "error": redact(traceback.format_exc()[-1500:])}


class MonitorThread:
    """Background monitor inside the app process (one per server, held by st.cache_resource)."""

    RUNNER = "app-thread"

    def __init__(self, store: AlertStore, provider_factory):
        self.store = store
        self.provider_factory = provider_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: dict | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="phoenix-alert-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.store.heartbeat(self.RUNNER, False)

    def _loop(self):
        first = True
        while not self._stop.is_set():
            st = self.store.settings()
            interval = max(1.0, float(st["monitor_interval_minutes"]))
            self.store.heartbeat(self.RUNNER, True, interval, bool(st["auto_send"]), started=first)
            first = False
            self.last_result = run_check(self.store, self.provider_factory(), runner=self.RUNNER)
            self.store.heartbeat(self.RUNNER, True, interval, bool(st["auto_send"]))
            # wait in short steps so Stop and heartbeats stay responsive
            end = datetime.now(timezone.utc) + timedelta(minutes=interval)
            while not self._stop.is_set() and datetime.now(timezone.utc) < end:
                self._stop.wait(30)
                if not self._stop.is_set():
                    self.store.heartbeat(self.RUNNER, True, interval, bool(st["auto_send"]))
        self.store.heartbeat(self.RUNNER, False)


def monitor_status(store: AlertStore, now: datetime | None = None) -> list[dict]:
    """Active = said it is active AND sent a heartbeat recently (otherwise it has stopped or crashed)."""
    now = now or datetime.now(timezone.utc)
    out = []
    for s in store.monitor_states():
        hb = parse_ts(s["heartbeat_at"])
        fresh = bool(hb) and (now - hb) < timedelta(minutes=3)
        out.append({**s, "alive": bool(s["active"]) and fresh,
                    "state": "Running" if (s["active"] and fresh) else ("Not responding" if s["active"] else "Stopped")})
    return out
