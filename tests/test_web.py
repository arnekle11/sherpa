import json
import re
import threading
import time

import httpx
import pytest
import yaml

from sherpa import web
from sherpa.config import load_config
from sherpa.store import Store
from datetime import UTC, datetime, timedelta

CFG = {"feed": {"url": "http://x/feed", "poll_interval_s": 2}, "alerts": {"repeat_min": 30},
       "devices": [{"name": "led-1", "ip": "192.168.1.50", "generation": 1}]}


@pytest.fixture
def site(tmp_path):
    cfg = dict(CFG, db_path=str(tmp_path / "data" / "sherpa.db"))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    store = Store(cfg["db_path"])
    now = datetime.now(UTC)
    store.record("g1", "<script>alert(1)</script> Požar", "l", now - timedelta(seconds=5), now, "triggered", "led-1=ok")
    store.log_alert("raise", "feed-down", "feed <b>UNREACHABLE</b>")
    store.close()
    (tmp_path / "data" / "status.json").write_text(json.dumps({
        "written": time.time(), "version": "t", "uptime_s": 100, "poll_interval_s": 2, "polls_ok": 5,
        "polls_failed": 0, "polls_skipped": 0, "events_triggered": 1, "last_event_at": None, "feed_ok_age_s": 1,
        "active_alerts": ["feed-down"], "devices": {"led-1": {"ip": "192.168.1.50", "ok": False,
        "detail": "UNREACHABLE: timeout", "checked": None}}}))
    srv = web.serve(str(path), "s3cret", "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, path, srv
    srv.shutdown(); srv.server_close()


def _login(c, base):
    r = c.post(f"{base}/login", data={"password": "s3cret"})
    assert r.status_code == 303
    return r


def _csrf(c, base):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(f"{base}/settings").text)[1]


def test_public_pages_render_and_escape_feed_text(site):
    base, _, _ = site
    with httpx.Client() as c:
        for p in ("/", "/events", "/alerts"):
            r = c.get(base + p)
            assert r.status_code == 200
            assert "<script>alert" not in r.text
            assert "default-src 'none'" in r.headers["content-security-policy"]
        assert "&lt;script&gt;alert(1)&lt;/script&gt; Požar" in c.get(base + "/events").text
        assert "feed &lt;b&gt;UNREACHABLE" in c.get(base + "/alerts").text
        d = c.get(base + "/").text
        assert "Feed unreachable" in d and "feed &lt;b&gt;UNREACHABLE&lt;/b&gt;" in d and "<b>UNREACHABLE" not in d
        assert "UNREACHABLE: timeout" in d  # device state from status.json
        assert c.get(base + "/static.css").status_code == 200


def test_stale_status_shows_service_down(site):
    base, path, _ = site
    sp = path.parent / "data" / "status.json"
    st = json.loads(sp.read_text()); st["written"] -= 600; sp.write_text(json.dumps(st))
    assert "Service not responding" in httpx.get(base + "/").text


def test_filters_favicon_and_security_headers(site):
    base, _, _ = site
    with httpx.Client() as c:
        assert "Požar" in c.get(base + "/events?f=delivered").text
        assert "Požar" not in c.get(base + "/events?f=problems").text
        assert "Požar" in c.get(base + "/events?f=nonsense").text  # unknown filter falls back to all
        info = c.get(base + "/alerts?f=info").text
        assert "Active now" in info and "What to do" in info  # active alerts are always summarised on top
        assert 'badge bad">Raised' not in info                 # ... but the filtered history holds no raise rows
        assert 'badge bad">Raised' in c.get(base + "/alerts?f=raised").text
        assert c.get(base + "/alerts?f=raised").status_code == 200
        r = c.get(base + "/favicon.svg")
        assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml"
        assert c.get(base + "/favicon.ico").status_code == 204
        css = c.get(base + "/static.css")
        assert "text/css" in css.headers["content-type"]
        for page in ("/", "/events", "/alerts", "/login"):
            html_ = c.get(base + page).text
            assert " style=" not in html_ and "<script" not in html_  # CSP forbids inline style/script


def test_settings_requires_login(site):
    base, path, _ = site
    with httpx.Client() as c:
        assert "Log in to view" in c.get(base + "/settings").text
        r = c.post(base + "/settings", data={"csrf": "x", "base": "y"})
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_wrong_password_and_lockout(site, monkeypatch):
    base, _, _ = site
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    with httpx.Client() as c:
        for _ in range(web.LOGIN_MAX_FAILS):
            assert c.post(base + "/login", data={"password": "nope"}).status_code == 401
        assert c.post(base + "/login", data={"password": "s3cret"}).status_code == 429


def test_edit_flow_with_csrf(site):
    base, path, _ = site
    with httpx.Client() as c:
        _login(c, base)
        page = c.get(base + "/settings").text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
        base_hash = re.search(r'name="base" value="([^"]+)"', page)[1]
        form = {"csrf": csrf, "base": base_hash, "feed.poll_interval_s": "3", "feed.max_age_min": "10",
                "feed.down_after_s": "60", "alerts.repeat_min": "30", "alerts.heartbeat_min": "0",
                "alerts.device_check_interval_s": "300", "dev.0.name": "led-1", "dev.0.ip": "192.168.1.50",
                "dev.0.generation": "1", "dev.0.model": "", "dev.0.channel": "0", "dev.0.on_duration_s": "300",
                "dev.0.timeout_s": "3", "dev.0.attempts": "4", "dev.0.retry_delay_s": "0.5"}
        bad = c.post(base + "/settings", data=dict(form, csrf="wrong"))
        assert bad.status_code == 403 and load_config(path).feed.poll_interval_s == 2
        r = c.post(base + "/settings", data=dict(form, **{"feed.poll_interval_s": "1"}))
        assert r.status_code == 400 and "minimum" in r.text and load_config(path).feed.poll_interval_s == 2
        r = c.post(base + "/settings", data=form)
        assert r.status_code == 200 and "Saved" in r.text
        assert load_config(path).feed.poll_interval_s == 3


def test_settings_form_is_prefilled_with_defaults_for_omitted_keys(site):
    base, _, _ = site
    with httpx.Client() as c:
        _login(c, base)
        page = c.get(base + "/settings").text
        for field, v in (("feed.max_age_min", "10.0"), ("feed.down_after_s", "60.0"), ("alerts.heartbeat_min", "0")):
            assert re.search(rf'id="{re.escape(field)}"[^>]*value="{re.escape(v)}"', page), field


def test_logout_ends_session(site):
    base, _, _ = site
    with httpx.Client() as c:
        _login(c, base)
        assert "Save settings" in c.get(base + "/settings").text
        c.post(base + "/logout")
        assert "Log in to view" in c.get(base + "/settings").text


def test_without_password_editing_is_disabled(tmp_path):
    cfg = dict(CFG, db_path=str(tmp_path / "d" / "s.db"))
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(cfg))
    srv = web.serve(str(tmp_path / "c.yaml"), "", "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        assert "Editing is disabled" in httpx.get(base + "/login").text
        assert "Editing is disabled" in httpx.post(base + "/login", data={"password": ""}).text
        assert httpx.get(base + "/").status_code == 200  # empty install still renders
    finally:
        srv.shutdown(); srv.server_close()
