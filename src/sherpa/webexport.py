"""Machine-readable views: events CSV, status JSON, Prometheus metrics. Read-only, like the dashboard."""
from __future__ import annotations

import csv
import io
import json
import time

from .webpages import overview

CSV_FIELDS = ("first_seen", "pub_date", "lag_s", "state", "is_test", "title", "link", "detail", "guid")


def _cell(v) -> str:
    """Neutralise spreadsheet formula injection: feed text is untrusted."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def events_csv(w) -> str:
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(CSV_FIELDS)
    for r in w.query(f"SELECT {', '.join(CSV_FIELDS)} FROM events ORDER BY first_seen DESC"):
        out.writerow([_cell(r[k]) for k in CSV_FIELDS])
    return buf.getvalue()


def status_json(w) -> str:
    ov = overview(w)
    st = ov["st"] or {}
    return json.dumps({
        "level": ov["level"], "summary": ov["pill"],
        "status_age_s": (time.time() - st["written"]) if st else None,
        "active_alerts": ov["alerts"],
        "status": st or None,
    }, indent=2)


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metrics(w) -> str:
    st = w.status() or {}
    age = (time.time() - st["written"]) if st else float("inf")
    lines: list[str] = []

    def m(name: str, kind: str, help_: str, samples: list[tuple[dict, float]]) -> None:
        lines.append(f"# HELP sherpa_{name} {help_}")
        lines.append(f"# TYPE sherpa_{name} {kind}")
        for labels, v in samples:
            lab = ",".join(f'{k}="{_esc(str(x))}"' for k, x in labels.items())
            lines.append(f"sherpa_{name}{{{lab}}} {v}" if lab else f"sherpa_{name} {v}")

    m("status_age_seconds", "gauge", "Seconds since the service last wrote its status (stale = service down).",
      [({}, round(age, 1) if age != float("inf") else "+Inf")])
    m("up", "gauge", "1 if the service status is fresh (< 30 s old).", [({}, int(age < 30))])
    if st:
        m("polls_total", "counter", "Feed polls since the service started.",
          [({"result": "ok"}, st["polls_ok"]), ({"result": "failed"}, st["polls_failed"]),
           ({"result": "skipped"}, st["polls_skipped"])])
        m("feed_last_ok_age_seconds", "gauge", "Seconds since the last healthy feed response.", [({}, round(st["feed_ok_age_s"], 1))])
        m("events_triggered_total", "counter", "New feed events since the service started.", [({}, st["events_triggered"])])
        m("active_alerts", "gauge", "Number of active alert conditions.", [({}, len(st["active_alerts"]))])
        m("alert_active", "gauge", "1 for each active alert condition.", [({"key": k}, 1) for k in st["active_alerts"]])
        m("telegram_configured", "gauge", "1 if Telegram alerting is configured.", [({}, int(bool(st.get("telegram_configured"))))])
        devs = st.get("devices", {})
        m("device_up", "gauge", "1 reachable, 0 unreachable, -1 not checked yet.",
          [({"device": n, "address": d.get("ip", "")}, 1 if d.get("ok") else (-1 if d.get("ok") is None else 0)) for n, d in devs.items()])
        lat = st.get("latency")
        if lat:
            m("feed_latency_milliseconds", "gauge", "Feed response time over recent successful polls.",
              [({"stat": k[:-3]}, round(v, 1)) for k, v in lat.items() if k.endswith("_ms")])
        dm = st.get("deadman") or {}
        m("deadman_configured", "gauge", "1 if the dead-man's switch is configured.", [({}, int(bool(dm.get("configured"))))])
    counts = w.query("SELECT state, is_test, COUNT(*) AS n FROM events GROUP BY state, is_test")
    m("events_recorded", "gauge", "Events in the database by state (retention 60 days).",
      [({"state": r["state"], "test": "true" if r["is_test"] else "false"}, r["n"]) for r in counts])
    return "\n".join(lines) + "\n"
