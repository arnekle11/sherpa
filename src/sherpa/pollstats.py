"""Per-minute poll statistics (availability + latency), flushed to SQLite once a minute.

Pure bookkeeping: recording never touches the database, so it cannot slow or break a poll.
"""
from __future__ import annotations

import statistics
from collections import deque
from datetime import UTC, datetime


def minute_key(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


class PollStats:
    def __init__(self, keep_latencies: int = 300):
        self._cur: dict[str, dict] = {}
        self.latencies: deque[float] = deque(maxlen=keep_latencies)  # seconds, successful responses only

    def _bucket(self, now: datetime | None) -> dict:
        return self._cur.setdefault(minute_key(now), {"ok": 0, "failed": 0, "skipped": 0,
                                                      "lat_sum": 0.0, "lat_n": 0, "lat_max": 0.0})

    def record(self, ok: bool, latency_s: float | None, now: datetime | None = None) -> None:
        b = self._bucket(now)
        b["ok" if ok else "failed"] += 1
        if latency_s is not None:
            b["lat_sum"] += latency_s
            b["lat_n"] += 1
            b["lat_max"] = max(b["lat_max"], latency_s)
            self.latencies.append(latency_s)

    def skipped(self, n: int, now: datetime | None = None) -> None:
        self._bucket(now)["skipped"] += n

    def drain(self) -> dict[str, dict]:
        """Take everything recorded so far (the DB upsert is additive, so partial minutes are fine)."""
        out, self._cur = self._cur, {}
        return out

    def requeue(self, rows: dict[str, dict]) -> None:
        """Put rows back after a failed flush so nothing is lost."""
        for k, r in rows.items():
            b = self._cur.setdefault(k, {"ok": 0, "failed": 0, "skipped": 0, "lat_sum": 0.0, "lat_n": 0, "lat_max": 0.0})
            for f in ("ok", "failed", "skipped", "lat_sum", "lat_n"):
                b[f] += r[f]
            b["lat_max"] = max(b["lat_max"], r["lat_max"])

    def latency_summary(self) -> dict | None:
        if not self.latencies:
            return None
        v = sorted(self.latencies)
        return {"n": len(v), "last_ms": self.latencies[-1] * 1000, "median_ms": statistics.median(v) * 1000,
                "p95_ms": v[min(len(v) - 1, int(0.95 * len(v)))] * 1000, "max_ms": v[-1] * 1000}
