"""Stand-alone PHOENIX alert monitor (separate process; keeps checking while it runs).

Usage (from the project folder, with the virtual environment active):
    python scripts/run_alert_monitor.py --once            # one check, never sends
    python scripts/run_alert_monitor.py                   # loop at the interval set in the app
    python scripts/run_alert_monitor.py --interval 10     # override the interval (minutes)

Sending follows the same rules as the app: only if "auto send" is enabled in Notification settings, the Twilio
environment variables are set, and PHOENIX_ALLOW_AUTOMATED_SMS=1 is set as well.
Stop with Ctrl+C. When this process is not running, nothing is being monitored.

To keep it running on Windows, create a Task Scheduler task that starts
    <project>\\.venv\\Scripts\\python.exe scripts\\run_alert_monitor.py
at log-on (Start in: the project folder). On Linux use a systemd service or a cloud worker.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phoenix_core.alert_monitor import run_check  # noqa: E402
from phoenix_core.alert_store import AlertStore  # noqa: E402
from phoenix_core.sms_service import get_provider, redact  # noqa: E402

RUNNER = "worker"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one check without sending, then exit")
    ap.add_argument("--interval", type=float, help="minutes between checks (default: app setting)")
    a = ap.parse_args()
    store = AlertStore()
    print("PHOENIX alert monitor started (local alerts database).")
    if a.once:
        res = run_check(store, get_provider(), runner=RUNNER, send=False)
        print("ok" if res["ok"] else redact(res["error"]), res.get("evaluation", {}).get("new_alert_ids"))
        return
    try:
        first = True
        while True:
            st = store.settings()
            interval = a.interval or float(st["monitor_interval_minutes"])
            store.heartbeat(RUNNER, True, interval, bool(st["auto_send"]), started=first)
            first = False
            res = run_check(store, get_provider(), runner=RUNNER)
            print(time.strftime("%Y-%m-%d %H:%M:%S"), "check", "ok" if res["ok"] else "FAILED",
                  "| new alerts:", len(res.get("evaluation", {}).get("new_alert_ids", [])),
                  "| messages:", res.get("dispatch", {}), "|", redact(res.get("auto_send", res.get("error", "")))[:120])
            end = time.time() + interval * 60
            while time.time() < end:
                time.sleep(min(30, max(0, end - time.time())))
                store.heartbeat(RUNNER, True, interval, bool(st["auto_send"]))
    except KeyboardInterrupt:
        print("stopped")
    finally:
        store.heartbeat(RUNNER, False)


if __name__ == "__main__":
    main()
