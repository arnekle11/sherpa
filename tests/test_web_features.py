import csv
import io
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import yaml

from sherpa import commands, web
from sherpa.store import Store
from sherpa.webstats import event_type


def _status(**over):
    st = {"written": time.time(), "version": "t", "uptime_s": 100, "poll_interval_s": 2, "polls_ok": 50,
          "polls_failed": 1, "polls_skipped": 0, "events_triggered": 2, "last_event_at": None, "feed_ok_age_s": 1,
          "active_alerts": [], "telegram_configured": False, "deadman": {"configured": False},
          "latency": {"n": 10, "last_ms": 210, "median_ms": 200, "p95_ms": 260, "max_ms": 300},
          "feed_items": [{"title": "Požar eksplozija 5. 11. 2026 20:44", "pub": datetime.now(UTC).isoformat()}],
          "devices": {"led-1": {"ip": "192.168.1.50", "ok": True, "detail": "reachable, relay off", "checked": None}}}
    st.update(over)
    return st


@pytest.fixture
def site(tmp_path):
    db = tmp_path / "data" / "sherpa.db"
    cfg = {"feed": {"url": "http://x/feed"}, "devices": [{"name": "led-1", "ip": "192.168.1.50", "generation": 1}],
           "db_path": str(db)}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    s = Store(str(db))
    now = datetime.now(UTC)
    s.record("g1", "Prometna nesreča 5. 11. 2026 20:41", "https://spin3.sos112.si/javno/zemljevid/1",
             now - timedelta(seconds=4), now - timedelta(hours=3), "triggered", "led-1=ok")
    s.record("g2", "=HYPERLINK(\"http://evil\") 5. 11. 2026 20:42", "", now - timedelta(seconds=9),
             now - timedelta(hours=1), "failed", "led-1=timeout")
    s.record("old", "Požar 4. 11. 2026 10:00", "", now - timedelta(days=1), now, "ignored_old")
    minute = now.replace(second=0, microsecond=0)
    s.add_poll_minutes({(minute - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M"):
                        {"ok": 30, "failed": 1 if i == 3 else 0, "skipped": 0, "lat_sum": 6.0, "lat_n": 30, "lat_max": 0.4}
                        for i in range(1, 40)})
    s.close()
    sp = db.parent / "status.json"
    sp.write_text(json.dumps(_status()))
    srv = web.serve(str(path), "pw", "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path, sp
    srv.shutdown(); srv.server_close()


def _login(c, base):
    assert c.post(base + "/login", data={"password": "pw"}).status_code == 303
    return re.search(r'name="csrf" value="([^"]+)"', c.get(base + "/settings").text)[1]


def test_event_type():
    assert event_type("Prometna nesreča 5. 11. 2026 20:41") == "Prometna nesreča"
    assert event_type("Požar, eksplozija 2. 11. 2026 18:05") == "Požar, eksplozija"
    assert event_type("Something else") == "Something else" and event_type("") == "(no title)"


def test_dashboard_new_cards(site):
    base, _, sp = site
    d = httpx.get(base + "/").text
    assert "Telegram is not configured" in d and "Feed health, last 24 h" in d
    assert "Currently in the feed" in d and "Požar eksplozija" in d and "Alert paths" in d
    assert "<svg" in d and " style=" not in d
    sp.write_text(json.dumps(_status(telegram_configured=True, deadman={"configured": True, "last_error": "HTTP 503",
                                                                        "last_ok_age_s": None})))
    d = httpx.get(base + "/").text
    assert "Telegram is not configured" not in d and "Dead-man" in d and "HTTP 503" in d


def test_stats_page(site):
    base, _, _ = site
    for days in ("7", "30", "60", "bogus"):
        r = httpx.get(base + f"/stats?days={days}")
        assert r.status_code == 200 and "Statistics" in r.text
    t = httpx.get(base + "/stats?days=7").text
    assert "Prometna nesreča" in t and "Events per day" in t and "Feed availability" in t
    assert "HYPERLINK" in t  # shown as text, escaped
    assert "50%" in t  # 1 of 2 counted events delivered (ignored_old excluded)


def test_csv_export_neutralises_formulas(site):
    base, _, _ = site
    r = httpx.get(base + "/events.csv")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 3
    evil = next(x for x in rows if "HYPERLINK" in x["title"])
    assert evil["title"].startswith("'=")


def test_api_status_and_metrics(site):
    base, _, sp = site
    j = httpx.get(base + "/api/status").json()
    assert j["level"] == "ok" and j["status"]["polls_ok"] == 50
    m = httpx.get(base + "/metrics").text
    assert "sherpa_up 1" in m and 'sherpa_polls_total{result="failed"} 1' in m
    assert 'sherpa_device_up{device="led-1",address="192.168.1.50"} 1' in m
    assert 'sherpa_events_recorded{state="triggered",test="false"} 1' in m
    sp.write_text(json.dumps(_status(written=time.time() - 600)))
    assert "sherpa_up 0" in httpx.get(base + "/metrics").text
    assert httpx.get(base + "/api/status").json()["level"] == "bad"


def test_tools_need_login_csrf_and_live_service(site):
    base, tmp, sp = site
    cdir = commands.command_dir(tmp / "data" / "sherpa.db")
    with httpx.Client() as c:
        r = c.post(base + "/tools/telegram-test", data={})
        assert r.status_code == 303 and r.headers["location"] == "/login"
        csrf = _login(c, base)
        assert c.post(base + "/tools/telegram-test", data={"csrf": "nope"}).status_code == 403
        r = c.post(base + "/tools/test-event", data={"csrf": csrf, "duration_s": "10"})
        assert "err=confirm" in r.headers["location"]
        r = c.post(base + "/tools/test-event", data={"csrf": csrf, "duration_s": "600", "confirm": "yes"})
        assert "err=duration" in r.headers["location"]
        r = c.post(base + "/tools/test-event", data={"csrf": csrf, "duration_s": "x", "confirm": "yes"})
        assert "err=duration" in r.headers["location"]
        assert not list(cdir.glob("*.json")) if cdir.exists() else True
        r = c.post(base + "/tools/test-event", data={"csrf": csrf, "duration_s": "10", "confirm": "yes"})
        assert "done=test_event" in r.headers["location"]
        r = c.post(base + "/tools/telegram-test", data={"csrf": csrf})
        assert "done=telegram_test" in r.headers["location"]
        got = commands.take(cdir)
        assert [(x["kind"], x.get("duration_s")) for x in got] == [("test_event", 10), ("telegram_test", None)]
        assert got[0]["source"] == "127.0.0.1"
        page = c.get(base + "/settings?done=test_event").text
        assert "Test event requested" in page and "Maintenance tools" in page
        assert "<form" in page and page.count("<form") == page.count("</form>")
        sp.write_text(json.dumps(_status(written=time.time() - 600)))
        r = c.post(base + "/tools/telegram-test", data={"csrf": csrf})
        assert "err=service_down" in r.headers["location"]
        assert commands.take(cdir) == []


def test_forms_are_not_nested(site):
    base, _, _ = site
    with httpx.Client() as c:
        _login(c, base)
        page = c.get(base + "/settings").text
    depth = 0
    for tag in re.findall(r"</?form\b", page):
        depth += 1 if tag == "<form" else -1
        assert depth in (0, 1)
