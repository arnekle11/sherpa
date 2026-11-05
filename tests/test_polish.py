"""Polish round: liveness signals, UI bounds, field errors, sessions, paging, help, status CLI."""
import asyncio
import json
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import yaml

from sherpa import settings, web
from sherpa.app import App
from sherpa.cli import main as cli
from sherpa.config import AlertsCfg, AppCfg, DeviceCfg, FeedCfg
from sherpa.mock import MockFeed
from sherpa.store import Store


def _status(**over):
    st = {"written": time.time(), "version": "t", "uptime_s": 100, "poll_interval_s": 2, "last_poll_age_s": 1,
          "polls_ok": 5, "polls_failed": 0, "polls_skipped": 0, "events_triggered": 0, "last_event_at": None,
          "feed_ok_age_s": 1, "active_alerts": [], "telegram_configured": True,
          "telegram_delivery": {"ok": True, "error": None, "undelivered": 0, "queued": 0},
          "deadman": {"configured": False}, "latency": None, "feed_items": [], "feed_items_at": time.time(),
          "devices": {"led-1": {"ip": "192.168.1.50", "ok": True, "detail": "reachable, relay off", "checked": None}}}
    st.update(over)
    return st


@pytest.fixture
def site(tmp_path):
    db = tmp_path / "data" / "sherpa.db"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"feed": {"url": "http://x/feed"}, "db_path": str(db),
                                    "devices": [{"name": "led-1", "ip": "192.168.1.50", "generation": 1}]}))
    Store(str(db)).close()
    sp = db.parent / "status.json"
    sp.write_text(json.dumps(_status()))
    srv = web.serve(str(path), "pw", "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path, sp, path
    srv.shutdown(); srv.server_close()


# ---- liveness --------------------------------------------------------------------------------
def test_status_is_written_on_its_own_clock_even_with_a_slow_poll_interval(tmp_path):
    async def go():
        feed = MockFeed().start()
        cfg = AppCfg(feed=FeedCfg(url=feed.url, poll_interval_s=50, timeout_s=1),
                     devices=[DeviceCfg(name="d", ip="127.0.0.1:9", generation=1)],
                     alerts=AlertsCfg(device_check_interval_s=0), db_path=str(tmp_path / "x.db"))
        app = App(cfg, telegram=None)
        app.status_every_s = 0.2
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.3)
        first = json.loads((tmp_path / "status.json").read_text())["written"]
        await asyncio.sleep(0.6)  # no poll happens in this time (interval 50 s), status must still refresh
        second = json.loads((tmp_path / "status.json").read_text())
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop()
        return first, second
    first, second = asyncio.run(go())
    assert second["written"] > first and second["last_poll_age_s"] >= 0.5


def test_dashboard_says_polling_stopped(site):
    base, _, sp, _ = site
    sp.write_text(json.dumps(_status(last_poll_age_s=400)))
    d = httpx.get(base + "/").text
    assert "Polling has stopped" in d and "Polling stopped" in d and "docker compose restart sherpa" in d
    sp.write_text(json.dumps(_status(poll_interval_s=50, last_poll_age_s=60)))  # slow interval: not stalled
    assert "Polling has stopped" not in httpx.get(base + "/").text


def test_healthcheck_allows_slow_poll_intervals(tmp_path):
    health = tmp_path / "health"
    health.touch()
    old = time.time() - 100
    os.utime(health, (old, old))
    assert cli(["healthcheck", "--file", str(health), "--max-age", "30"]) == 1
    (tmp_path / "status.json").write_text(json.dumps({"poll_interval_s": 60}))
    assert cli(["healthcheck", "--file", str(health), "--max-age", "30"]) == 0   # limit 190 s
    old = time.time() - 300
    os.utime(health, (old, old))
    assert cli(["healthcheck", "--file", str(health), "--max-age", "30"]) == 1


# ---- settings bounds and field errors -------------------------------------------------------------
BASE = {"feed": {"url": "http://x/feed"}, "devices": [{"name": "led-1", "ip": "192.168.1.50", "generation": 1}]}


def _form(**over):
    f = {"feed.poll_interval_s": "2", "feed.max_age_min": "10", "feed.down_after_s": "60", "alerts.repeat_min": "30",
         "alerts.heartbeat_min": "0", "alerts.device_check_interval_s": "300", "alerts.deadman_interval_s": "60",
         "dev.0.name": "led-1", "dev.0.ip": "192.168.1.50", "dev.0.generation": "1", "dev.0.model": "",
         "dev.0.channel": "0", "dev.0.on_duration_s": "300", "dev.0.timeout_s": "3", "dev.0.attempts": "4",
         "dev.0.retry_delay_s": "0.5", "dev.1.new": "1", "dev.1.name": ""}
    f.update(over)
    return f


@pytest.mark.parametrize("field,val,expect", [
    ("feed.max_age_min", "2000", "old emergencies"),   # would fire the LED for everything in the feed on restart
    ("feed.max_age_min", "0", "1-120"),                # would ignore an emergency arriving during a restart
    ("feed.poll_interval_s", "61", "2-60"),
    ("feed.poll_interval_s", "1", "minimum"),
    ("alerts.device_check_interval_s", "5", "or 0"),
    ("dev.0.on_duration_s", "3", "10-3600"),
    ("dev.0.timeout_s", "30", "0.5-10"),
    ("dev.0.channel", "9", "0-3"),
])
def test_ui_bounds(tmp_path, field, val, expect):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(BASE))
    errs = settings.save(p, _form(**{field: val}), settings.config_hash(p))
    assert errs and expect in errs[0] and errs[0].field == field
    assert yaml.safe_load(p.read_text()) == BASE


def test_zero_allowed_where_it_means_off_and_header_written(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(BASE))
    assert settings.save(p, _form(**{"alerts.device_check_interval_s": "0", "alerts.repeat_min": "0"}),
                         settings.config_hash(p)) == []
    assert p.read_text().startswith("# Managed by the SHERPA web UI")


def test_duplicate_relay_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(BASE))
    errs = settings.save(p, _form(**{"dev.1.name": "led-2", "dev.1.ip": "192.168.1.50", "dev.1.generation": "1",
                                     "dev.1.channel": "0", "dev.1.on_duration_s": "300", "dev.1.timeout_s": "3",
                                     "dev.1.attempts": "4", "dev.1.retry_delay_s": "0.5"}), settings.config_hash(p))
    assert any("same relay" in e and e.field == "dev.1.ip" for e in errs)


def test_pydantic_errors_map_to_fields_and_last_device_message(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(BASE))
    errs = settings.save(p, _form(**{"dev.0.delete": "on"}), settings.config_hash(p))
    assert any("at least one device" in e for e in errs)


def _login(c, base):
    assert c.post(base + "/login", data={"password": "pw"}).status_code == 303
    page = c.get(base + "/settings").text
    return (re.search(r'name="csrf" value="([^"]+)"', page)[1], re.search(r'name="base" value="([^"]+)"', page)[1])


def test_invalid_field_is_marked_in_the_form(site):
    base, *_ = site
    with httpx.Client() as c:
        csrf, h = _login(c, base)
        r = c.post(base + "/settings", data={"csrf": csrf, "base": h, **_form(**{"feed.max_age_min": "2000"})})
    assert r.status_code == 400
    assert re.search(r'id="feed\.max_age_min"[^>]*aria-invalid="true"', r.text)
    assert 'id="feed.max_age_min-err"' in r.text and "Not saved: 1 problem" in r.text
    assert 'value="2000"' in r.text  # what was typed is kept


# ---- sessions --------------------------------------------------------------------------------------
def test_session_cookie_is_a_browser_session_cookie(site):
    base, *_ = site
    r = httpx.post(base + "/login", data={"password": "pw"})
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Max-Age" not in cookie


def test_sessions_survive_concurrent_logins(site):
    base, *_ = site
    clients = [httpx.Client() for _ in range(12)]
    threads = [threading.Thread(target=lambda c=c: c.post(base + "/login", data={"password": "pw"})) for c in clients]
    for t in threads: t.start()
    for t in threads: t.join()
    ok = sum("Save settings" in c.get(base + "/settings").text for c in clients)
    for c in clients: c.close()
    assert ok == 12


# ---- tab title / favicon, help, paging -------------------------------------------------------------
def test_status_in_title_and_favicon(site):
    base, _, sp, _ = site
    d = httpx.get(base + "/").text
    assert "<title>Dashboard · SHERPA</title>" in d and "/favicon.svg?s=ok" in d
    sp.write_text(json.dumps(_status(active_alerts=["feed-down"])))
    d = httpx.get(base + "/").text
    assert "<title>● 1 alert · Dashboard · SHERPA</title>" in d and "/favicon.svg?s=bad" in d
    assert "What to do" in d and "internet connection" in d
    svg = httpx.get(base + "/favicon.svg?s=bad").text
    assert "#ef4444" in svg and httpx.get(base + "/favicon.svg?s=<script>").text.startswith("<svg")


def test_pause_auto_refresh(site):
    base, *_ = site
    assert 'http-equiv="refresh"' in httpx.get(base + "/").text
    paused = httpx.get(base + "/?refresh=0").text
    assert 'http-equiv="refresh"' not in paused and "Auto-refresh paused" in paused


def test_help_page(site):
    base, *_ = site
    t = httpx.get(base + "/help").text
    assert "Alerts and what to do" in t and "&lt;device&gt; unreachable" in t and "end to end" in t


def test_keyset_paging(site):
    base, tmp, _, _ = site
    s = Store(str(tmp / "data" / "sherpa.db"))
    now = datetime.now(UTC)
    for i in range(130):
        t = now - timedelta(minutes=i)
        s.record(f"g{i}", f"Event {i:03d}", "", t, t, "triggered")
        s.log_alert("info", None, f"note {i:03d}")
    s.close()
    with httpx.Client() as c:
        p1 = c.get(base + "/events").text
        assert p1.count("Event 0") == 100 and "Older →" in p1
        older = re.search(r'href="(/events\?[^"]+)">Older', p1)[1].replace("&amp;", "&")
        p2 = c.get(base + older).text
        assert "Event 129" in p2 and "Event 000" not in p2 and "Older →" not in p2 and "Newest" in p2
        a1 = c.get(base + "/alerts").text
        a2 = c.get(base + re.search(r'href="(/alerts\?[^"]+)">Older', a1)[1].replace("&amp;", "&")).text
        assert "note 129" in a1 and "note 000" in a2 and "note 129" not in a2  # ids grow: 129 is the newest
        assert c.get(base + "/events?before=garbage").status_code == 200


# ---- sherpa status ---------------------------------------------------------------------------------
def test_status_cli(site, capsys):
    _, _, sp, cfg = site
    assert cli(["status", "-c", str(cfg)]) == 0
    assert "SHERPA: OPERATIONAL" in capsys.readouterr().out
    sp.write_text(json.dumps(_status(active_alerts=["offline:led-1"], telegram_configured=False)))
    assert cli(["status", "-c", str(cfg)]) == 1
    out = capsys.readouterr().out
    assert "ALERT led-1 unreachable" in out and "what to do" in out and "NOT configured" in out
    sp.unlink()
    assert cli(["status", "-c", str(cfg)]) == 2
