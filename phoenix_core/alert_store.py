"""Local SQLite persistence for the Flood Alerts & SMS feature.

Location: data/private/phoenix_alerts.db (override with the PHOENIX_ALERTS_DB environment variable).
The folder contains a .gitignore so recipient phone numbers are never committed to source control.
Back it up by copying the .db file while the app and monitor are stopped (see README).

Tables
  recipients       who may receive messages, with consent and opt-out status
  alerts           every alert generated (real or test), with source and explanation
  alert_matches    which recipients each alert applies to (location match)
  deliveries       every send attempt or deliberate skip, with provider status
  monitor_runs     every monitoring check (manual, in-app thread or worker)
  monitor_state    heartbeat of each running monitor
  settings         administrator settings (cooldown, thresholds, interval...)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "private" / "phoenix_alerts.db"

DEFAULT_SETTINGS = {
    "cooldown_minutes": 180,          # minimum gap between messages to one person (escalations excepted)
    "max_messages_per_day": 4,        # hard cap per person per 24 h (critical official warnings excepted)
    "max_attempts": 3,                # retries for failed sends
    "forecast_alerts_enabled": True,  # PHOENIX rainfall-forecast notices (never above ELEVATED)
    "forecast_threshold_mm_24h": 50.0,  # administrator screening threshold - NOT a validated flood trigger
    "monitor_interval_minutes": 15,
    "auto_send": False,               # monitor sends automatically (real provider also needs env approval)
    "help_contact": "",               # shown in HELP text, e.g. an agency phone or email
    # evidence recorded by the system (not administrator settings)
    "twilio_verified_at": "",         # last successful credential check with Twilio (no SMS sent)
    "twilio_verified_note": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    city TEXT NOT NULL,
    area_label TEXT NOT NULL,
    zone_type TEXT NOT NULL,          -- ward | point | city
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    min_level INTEGER NOT NULL DEFAULT 2,
    sms_enabled INTEGER NOT NULL DEFAULT 1,
    consent INTEGER NOT NULL DEFAULT 0,
    consent_at TEXT,
    consent_method TEXT,
    consent_recorded_by TEXT,
    opted_out INTEGER NOT NULL DEFAULT 0,
    opted_out_at TEXT,
    opt_out_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (phone, city)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    city TEXT NOT NULL,
    area_label TEXT NOT NULL,
    level INTEGER NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,       -- official_warning | model_forecast | test
    event_key TEXT NOT NULL,
    headline TEXT NOT NULL,
    explanation TEXT NOT NULL,
    precautions TEXT NOT NULL,
    expires_at TEXT,
    is_test INTEGER NOT NULL DEFAULT 0,
    escalated_from INTEGER,
    raw TEXT,
    UNIQUE (event_key, level)
);
CREATE TABLE IF NOT EXISTS alert_matches (
    alert_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    reason TEXT,
    PRIMARY KEY (alert_id, recipient_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    level INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,            -- simulated | queued | sending | sent | delivered | undelivered | failed | skipped
    skip_reason TEXT,
    provider TEXT,
    provider_sid TEXT,
    error_code TEXT,
    error_message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    is_test INTEGER NOT NULL DEFAULT 0,
    phone_masked TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS monitor_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    runner TEXT NOT NULL,            -- manual | app-thread | worker
    ok INTEGER,
    error TEXT,
    cities TEXT,
    alerts_new INTEGER DEFAULT 0,
    messages_attempted INTEGER DEFAULT 0,
    sources TEXT
);
CREATE TABLE IF NOT EXISTS monitor_state (
    runner TEXT PRIMARY KEY,
    active INTEGER NOT NULL,
    interval_minutes REAL,
    heartbeat_at TEXT,
    started_at TEXT,
    pid INTEGER,
    auto_send INTEGER
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_ids (source_id TEXT PRIMARY KEY, event_key TEXT NOT NULL);  -- NWS id -> event
CREATE INDEX IF NOT EXISTS ix_deliv_recipient ON deliveries (recipient_id, created_at);
CREATE INDEX IF NOT EXISTS ix_deliv_event ON deliveries (recipient_id, event_key);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def db_path() -> Path:
    return Path(os.environ.get("PHOENIX_ALERTS_DB") or DEFAULT_DB)


class AlertStore:
    """Thread-safe thin wrapper around one SQLite file (the app thread and UI share it)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            gi = self.path.parent / ".gitignore"
            if not gi.exists():
                gi.write_text("# Recipient phone numbers live here. Never commit.\n*\n", encoding="utf-8")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL") if str(self.path) != ":memory:" else None
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        r = self.q(sql, args)
        return r[0] if r else None

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ settings
    def settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        for r in self.q("SELECT key, value FROM settings"):
            out[r["key"]] = json.loads(r["value"])
        return out

    def set_settings(self, **kw):
        with self.tx() as c:
            for k, v in kw.items():
                if k not in DEFAULT_SETTINGS:
                    raise KeyError(k)
                c.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (k, json.dumps(v)))

    # ------------------------------------------------------------ monitor bookkeeping
    def start_run(self, runner: str) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO monitor_runs(started_at, runner) VALUES(?, ?)", (utcnow(), runner)).lastrowid

    def finish_run(self, run_id: int, ok: bool, error: str | None = None, cities=(), alerts_new=0,
                   messages_attempted=0, sources=None):
        with self.tx() as c:
            c.execute("UPDATE monitor_runs SET finished_at=?, ok=?, error=?, cities=?, alerts_new=?, "
                      "messages_attempted=?, sources=? WHERE id=?",
                      (utcnow(), int(ok), error, ",".join(cities), alerts_new, messages_attempted,
                       json.dumps(sources or {}), run_id))

    def heartbeat(self, runner: str, active: bool, interval_minutes: float | None = None, auto_send: bool = False,
                  started: bool = False):
        with self.tx() as c:
            c.execute("""INSERT INTO monitor_state(runner, active, interval_minutes, heartbeat_at, started_at, pid, auto_send)
                         VALUES(?,?,?,?,?,?,?) ON CONFLICT(runner) DO UPDATE SET active=excluded.active,
                         interval_minutes=COALESCE(excluded.interval_minutes, interval_minutes),
                         heartbeat_at=excluded.heartbeat_at,
                         started_at=CASE WHEN ? THEN excluded.started_at ELSE started_at END,
                         pid=excluded.pid, auto_send=excluded.auto_send""",
                      (runner, int(active), interval_minutes, utcnow(), utcnow(), os.getpid(), int(auto_send),
                       int(started)))

    def monitor_states(self) -> list[dict]:
        return self.q("SELECT * FROM monitor_state")

    def last_run(self, successful_only=False) -> dict | None:
        cond = "WHERE ok=1" if successful_only else "WHERE finished_at IS NOT NULL"
        return self.one(f"SELECT * FROM monitor_runs {cond} ORDER BY id DESC LIMIT 1")
