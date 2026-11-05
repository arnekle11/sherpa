"""SQLite persistence: dedupe by guid + first-seen/pubDate lag record.

Local-file SQLite calls take well under a millisecond, so they run directly on the
event loop. synchronous=FULL so a power cut cannot lose a recorded guid.
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  guid       TEXT PRIMARY KEY,
  title      TEXT,
  link       TEXT,
  pub_date   TEXT,          -- ISO UTC, may be NULL
  first_seen TEXT NOT NULL, -- ISO UTC
  lag_s      REAL,          -- first_seen - pub_date
  state      TEXT NOT NULL, -- pending|triggered|partial|failed|ignored_old|abandoned
  detail     TEXT,
  is_test    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_first_seen ON events(first_seen);
CREATE TABLE IF NOT EXISTS alert_log (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  ts   TEXT NOT NULL,       -- ISO UTC
  kind TEXT NOT NULL,       -- raise|repeat|clear|info
  key  TEXT,                -- alert key for raise/repeat/clear
  text TEXT NOT NULL
);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def lag_summary(lags: list[float]) -> dict | None:
    if not lags:
        return None
    lags = sorted(lags)
    p95 = lags[min(len(lags) - 1, int(0.95 * len(lags)))]
    return {"n": len(lags), "min": lags[0], "median": statistics.median(lags),
            "p95": p95, "max": lags[-1]}


LAG_SQL = "SELECT lag_s FROM events WHERE is_test=0 AND state NOT IN ('ignored_old') AND lag_s IS NOT NULL"


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self._seen: set[str] = {r["guid"] for r in self.db.execute("SELECT guid FROM events")}

    def seen(self, guid: str) -> bool:
        return guid in self._seen

    def record(self, guid: str, title: str, link: str, pub: datetime | None,
               first_seen: datetime, state: str, detail: str = "", is_test: bool = False) -> bool:
        """Insert; return True only if this guid was new."""
        lag = (first_seen - pub).total_seconds() if pub else None
        cur = self.db.execute(
            "INSERT OR IGNORE INTO events(guid,title,link,pub_date,first_seen,lag_s,state,detail,is_test)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (guid, title, link, _iso(pub), _iso(first_seen), lag, state, detail, int(is_test)),
        )
        self._seen.add(guid)
        return cur.rowcount == 1

    def set_state(self, guid: str, state: str, detail: str = "") -> None:
        self.db.execute("UPDATE events SET state=?, detail=? WHERE guid=?", (state, detail, guid))

    def pending(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM events WHERE state='pending'"))

    def prune(self, days: int = 60) -> None:
        cutoff = _iso(datetime.now(UTC) - timedelta(days=days))
        self.db.execute("DELETE FROM events WHERE first_seen < ?", (cutoff,))
        self.db.execute("DELETE FROM alert_log WHERE ts < ?", (cutoff,))

    def log_alert(self, kind: str, key: str | None, text: str) -> None:
        self.db.execute("INSERT INTO alert_log(ts,kind,key,text) VALUES (?,?,?,?)",
                        (_iso(datetime.now(UTC)), kind, key, text))

    def recent(self, n: int = 10) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM events ORDER BY first_seen DESC LIMIT ?", (n,)))

    def lag_stats(self) -> dict | None:
        return lag_summary([r["lag_s"] for r in self.db.execute(LAG_SQL)])

    def close(self) -> None:
        self.db.close()
