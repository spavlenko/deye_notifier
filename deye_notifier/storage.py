"""SQLite storage for grid state and outage history."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS outages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER
);
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id       INTEGER PRIMARY KEY,
    name          TEXT,
    subscribed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id        TEXT PRIMARY KEY,
    notified_start  INTEGER NOT NULL DEFAULT 0,
    notified_end    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS readings (
    ts            INTEGER PRIMARY KEY,
    grid_state    TEXT,
    soc           REAL,
    grid_v1       REAL,
    grid_v2       REAL,
    grid_v3       REAL,
    load_w        REAL,
    pv_w          REAL,
    grid_w        REAL,
    battery_w     REAL,
    battery_temp  REAL,
    inverter_temp REAL
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Outage:
    id: int
    started_at: int
    ended_at: int | None

    @property
    def duration(self) -> int:
        return (self.ended_at or int(time.time())) - self.started_at


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Each thread (monitor, Telegram bot) opens its own Storage on the same file.
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(alerts)")}
        with self.db:
            if "name" not in cols:
                self.db.execute("ALTER TABLE alerts ADD COLUMN name TEXT")
            if "started_at" not in cols:
                self.db.execute("ALTER TABLE alerts ADD COLUMN started_at INTEGER")

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def open_outage(self) -> Outage | None:
        row = self.db.execute(
            "SELECT id, started_at, ended_at FROM outages WHERE ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return Outage(*row) if row else None

    def start_outage(self, ts: int) -> Outage:
        with self.db:
            cur = self.db.execute("INSERT INTO outages(started_at) VALUES(?)", (ts,))
        return Outage(cur.lastrowid, ts, None)

    def end_outage(self, outage: Outage, ts: int) -> Outage:
        with self.db:
            self.db.execute("UPDATE outages SET ended_at=? WHERE id=?", (ts, outage.id))
        return Outage(outage.id, outage.started_at, ts)

    def outages(self, since: int = 0) -> list[Outage]:
        rows = self.db.execute(
            "SELECT id, started_at, ended_at FROM outages "
            "WHERE ended_at IS NULL OR ended_at >= ? ORDER BY started_at",
            (since,),
        ).fetchall()
        return [Outage(*r) for r in rows]

    # ---- Telegram subscribers ----------------------------------------------

    def add_subscriber(self, chat_id: int, name: str) -> bool:
        """Returns False if already subscribed."""
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO subscribers(chat_id, name, subscribed_at) VALUES(?, ?, ?)",
                (chat_id, name, int(time.time())),
            )
        return cur.rowcount > 0

    def remove_subscriber(self, chat_id: int) -> bool:
        """Returns False if was not subscribed."""
        with self.db:
            cur = self.db.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
        return cur.rowcount > 0

    def migrate_subscriber(self, old_id: int, new_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE OR IGNORE subscribers SET chat_id=? WHERE chat_id=?",
                            (new_id, old_id))
            self.db.execute("DELETE FROM subscribers WHERE chat_id=?", (old_id,))

    def is_subscribed(self, chat_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM subscribers WHERE chat_id=?", (chat_id,)
        ).fetchone() is not None

    def subscribers(self) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT chat_id FROM subscribers")]

    # ---- Deye alerts -------------------------------------------------------

    def get_alert(self, alert_id: str) -> dict[str, bool] | None:
        row = self.db.execute(
            "SELECT notified_start, notified_end FROM alerts WHERE alert_id=?", (alert_id,)
        ).fetchone()
        return {"notified_start": bool(row[0]), "notified_end": bool(row[1])} if row else None

    def save_alert(self, alert_id: str, notified_start: bool, notified_end: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO alerts(alert_id, notified_start, notified_end) VALUES(?, ?, ?) "
                "ON CONFLICT(alert_id) DO UPDATE SET notified_start=excluded.notified_start, "
                "notified_end=excluded.notified_end",
                (alert_id, int(notified_start), int(notified_end)),
            )

    def save_alert_meta(self, alert_id: str, name: str, started_at: int | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO alerts(alert_id, name, started_at) VALUES(?, ?, ?) "
                "ON CONFLICT(alert_id) DO UPDATE SET name=excluded.name, started_at=excluded.started_at",
                (alert_id, name, started_at),
            )

    def count_alerts(self, name_part: str, since: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM alerts WHERE name LIKE ? AND started_at >= ?",
            (f"%{name_part}%", since),
        ).fetchone()[0]

    # ---- reading history ---------------------------------------------------

    READING_COLS = ("grid_state", "soc", "grid_v1", "grid_v2", "grid_v3", "load_w", "pv_w",
                    "grid_w", "battery_w", "battery_temp", "inverter_temp")
    KEEP_READINGS = 120 * 86400

    def add_reading(self, ts: int, values: dict) -> None:
        cols = ", ".join(self.READING_COLS)
        marks = ", ".join("?" for _ in self.READING_COLS)
        with self.db:
            self.db.execute(
                f"INSERT OR REPLACE INTO readings(ts, {cols}) VALUES(?, {marks})",
                (ts, *(values.get(c) for c in self.READING_COLS)),
            )
            self.db.execute("DELETE FROM readings WHERE ts < ?", (ts - self.KEEP_READINGS,))

    def readings(self, since: int, until: int | None = None) -> list[dict]:
        cur = self.db.execute(
            f"SELECT ts, {', '.join(self.READING_COLS)} FROM readings "
            "WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (since, until if until is not None else 2**62),
        )
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur]

    def reading_at_or_before(self, ts: int) -> dict | None:
        cur = self.db.execute(
            f"SELECT ts, {', '.join(self.READING_COLS)} FROM readings "
            "WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts,),
        )
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else None

    def latest_reading(self) -> dict | None:
        return self.reading_at_or_before(2**62)
