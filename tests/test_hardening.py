"""Second review round: the LED must light despite malformed feeds and storage failures; alerts must stay
useful under stress; the web UI must stay up and honest."""
import asyncio
import json
import re
import socket
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import yaml

from sherpa import cli, settings, web
from sherpa.alerts import AlertManager
from sherpa.app import App
from sherpa.config import AlertsCfg, AppCfg, DeviceCfg, FeedCfg, load_config
from sherpa.feed import parse_feed
from sherpa.mock import MockFeed, MockShelly, MockTelegram
from sherpa.store import Store


def _rss(items_xml: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>t</title>'
            f'<link>https://spin3.sos112.si/javno/OD/52034</link>{items_xml}</channel></rss>').encode()


def _item(n: int, pub: datetime, desc: str = "Ljubljana") -> str:
    g = f"https://spin3.sos112.si/javno/zemljevid/{n}"
    return (f"<item><title>Požar {n} 5. 11. 2026 20:00</title><description>{desc}</description><link>{g}</link>"
            f"<guid>{g}</guid><pubDate>{format_datetime(pub, usegmt=True)}</pubDate></item>")


def _app(tmp_path, sh, **kw):
    cfg = AppCfg(feed=FeedCfg(url="http://127.0.0.1:9/feed", poll_interval_s=0.2),
                 devices=[DeviceCfg(name="d", ip=sh.hostport, generation=1, attempts=1)],
                 alerts=AlertsCfg(device_check_interval_s=0), db_path=str(tmp_path / "x.db"))
    return App(cfg, telegram=None, **kw)


async def _serve(app, bodies):
    """Feed the app the given response bodies, one per poll."""
    it = iter(bodies)

    async def fetch():
        return next(it)
    app.fetcher.fetch = fetch
    for _ in bodies:
        await app.poll_once()
    await asyncio.sleep(0.3)  # let dispatch tasks run


# ---- the LED must still light -------------------------------------------------------------------
def test_malformed_item_does_not_hide_a_new_emergency(tmp_path):
    sh = MockShelly(1).start()
    app = _app(tmp_path, sh)
    now = datetime.now(UTC)
    asyncio.run(_serve(app, [_rss(""), _rss(_item(1, now) + _item(2, now, "Gasilci & reševalci&nbsp;\x0b"))]))
    sh.stop()
    assert sh.is_on and len(sh.on_calls) == 1
    assert "read leniently" in " ".join(parse_feed(_rss(_item(2, now, "a & b"))).problems)


def test_storage_failure_still_lights_the_led_exactly_once(tmp_path):
    sh = MockShelly(1).start()
    app = _app(tmp_path, sh)

    def broken(*a, **k):
        raise sqlite3.OperationalError("attempt to write a readonly database")
    now = datetime.now(UTC)

    async def go():
        await _serve(app, [_rss("")])          # baseline
        app.store.record = broken
        await _serve(app, [_rss(_item(7, now))] * 3)  # the same item on three polls
    asyncio.run(go())
    sh.stop()
    assert len(sh.on_calls) == 1 and app.alerts.is_active("storage")


def test_flapping_feed_does_not_flood_telegram_but_a_real_change_alerts(tmp_path):
    sh = MockShelly(1).start()
    app = _app(tmp_path, sh)
    ok, html = _rss(""), b"<html><body>Bad gateway</body></html>"
    asyncio.run(_serve(app, [ok, html] * 10))
    assert not any("structure" in m for m in app.alerts.sent)
    asyncio.run(_serve(app, [html] * 3))
    assert app.alerts.is_active("feed-structure")
    asyncio.run(_serve(app, [ok] * 3))
    sh.stop()
    assert not app.alerts.is_active("feed-structure")


def test_renumbered_old_items_do_not_fire_but_outage_events_do(tmp_path):
    sh = MockShelly(1).start()
    app = _app(tmp_path, sh)
    now = datetime.now(UTC)
    old = "".join(_item(100 + i, now - timedelta(hours=20)) for i in range(5))
    asyncio.run(_serve(app, [_rss(""), _rss(old)]))
    assert not sh.on_calls and any("OLD items appeared" in m for m in app.alerts.sent)
    # a feed outage of 2 h: three events published during it must still trigger when it comes back
    app._last_parse_ok_at = now - timedelta(hours=2)
    during = "".join(_item(200 + i, now - timedelta(minutes=50 + i)) for i in range(3))
    asyncio.run(_serve(app, [_rss(old + during)]))
    sh.stop()
    assert len(sh.on_calls) == 1


def test_relay_on_without_timer_is_rearmed(tmp_path):
    sh = MockShelly(1).start()
    sh._on(None)  # e.g. power restore with "last state"
    app = _app(tmp_path, sh)
    asyncio.run(app.probe_devices())
    sh.stop()
    assert sh.timer_expires is not None and sh.on_calls[-1]["timer"] == "300"
    assert any("re-armed" in m for m in app.alerts.sent)


def test_retry_backoff_is_capped(monkeypatch):
    from sherpa.devices import make_device
    slept = []
    real = asyncio.sleep

    async def fake(s):
        slept.append(s)
        await real(0)
    monkeypatch.setattr(asyncio, "sleep", fake)
    dev = make_device(DeviceCfg(name="d", ip="127.0.0.1:9", generation=1, attempts=6, retry_delay_s=10, timeout_s=0.2),
                      httpx.AsyncClient(trust_env=False))
    asyncio.run(dev.activate())
    assert slept and max(slept) <= 5.0


def test_device_client_has_no_hidden_5s_cap():
    from sherpa.devices import make_device_client
    c = make_device_client()
    assert c.timeout.read is None
    asyncio.run(c.aclose())


def test_invalid_log_level_rejected_and_iso_dates_accepted(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("feed: {url: http://x}\ndevices: [{name: a, ip: 1.2.3.4, generation: 1}]\nlog_level: TRACE\n")
    with pytest.raises(Exception, match="log_level"):
        load_config(p)
    from sherpa.feed import _parse_date
    assert _parse_date("2026-11-05T19:00:00Z")[0] == datetime(2026, 11, 5, 19, tzinfo=UTC)


# ---- alerting under stress --------------------------------------------------------------------------
def test_alert_worker_survives_a_crashing_notifier_and_honours_retry_after(monkeypatch):
    calls, slept = [], []
    real = asyncio.sleep

    class Flaky:
        last_error, retry_after = None, 0.0

        async def send(self, text):
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("bug")
            if len(calls) == 2:
                self.retry_after = 7.0
                return False
            self.retry_after = 0.0
            return True

    async def fake(s):
        slept.append(s)
        await real(0)

    async def go():
        monkeypatch.setattr(asyncio, "sleep", fake)
        am = AlertManager(Flaky(), host="h")
        w = asyncio.create_task(am.run())
        am.info("one")
        am.info("two")
        await am.drain(5)
        w.cancel()
        return am
    am = asyncio.run(go())
    assert am.delivery_ok is True and 7.0 in slept and len(calls) == 4


def test_device_alerts_jump_the_queue_and_queue_is_bounded():
    am = AlertManager(type("N", (), {"send": None})(), host="h")
    for i in range(400):
        am.info(f"info {i}")
    am.raise_("activate:led-1", "led-1 FAILED to switch on")
    first = am._q.get_nowait()[2]
    assert "led-1 FAILED" in first and am._q.qsize() <= 301 and am.dropped >= 99


def test_fatal_start_is_reported_once_per_half_hour(tmp_path, monkeypatch):
    tg = MockTelegram().start()
    monkeypatch.setenv("SHERPA_TELEGRAM_TOKEN", "T0KEN"); monkeypatch.setenv("SHERPA_TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(cli, "FATAL_MARKER", tmp_path / "marker")
    cli._report_fatal("OperationalError: attempt to write a readonly database", tg.api_base)
    cli._report_fatal("again", tg.api_base)
    tg.stop()
    assert len(tg.messages) == 1 and "cannot run" in tg.messages[0] and "T0KEN" not in tg.messages[0]


def test_cli_test_event_goes_through_the_running_service(tmp_path, capsys):
    sh = MockShelly(2).start()
    feed = MockFeed().start()
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(yaml.safe_dump({"feed": {"url": feed.url, "poll_interval_s": 0.5},
                                       "devices": [{"name": "d", "ip": sh.hostport, "generation": 2}],
                                       "alerts": {"device_check_interval_s": 0}, "db_path": str(tmp_path / "x.db")}))
    loop = asyncio.new_event_loop()
    holder = {}

    def service():  # SQLite objects belong to the thread that made them: build the App in the service thread
        asyncio.set_event_loop(loop)
        holder["app"] = app = App(load_config(cfgfile), telegram=None)
        app.command_poll_s = 0.1
        loop.run_until_complete(app.run())
    t = threading.Thread(target=service, daemon=True)
    t.start()
    try:
        for _ in range(50):
            if (tmp_path / "status.json").exists():
                break
            time.sleep(0.1)
        rc = cli.main(["test-event", "-c", str(cfgfile), "--duration", "3", "--yes"])
    finally:
        loop.call_soon_threadsafe(holder["app"].stop)
        t.join(15)
        sh.stop(); feed.stop()
    out = capsys.readouterr().out
    assert rc == 0 and "Queued for the running service" in out and "OK" in out
    assert any("from the command line" in m for m in holder["app"].alerts.sent)


# ---- web UI -------------------------------------------------------------------------------------------
@pytest.fixture
def site(tmp_path):
    db = tmp_path / "data" / "sherpa.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"feed": {"url": "http://x/feed", "poll_interval_s": 1},  # hand-set 1 s
                                   "devices": [{"name": "led-1", "ip": "192.168.1.50", "generation": 1}],
                                   "db_path": str(db)}))
    Store(str(db)).close()
    (db.parent / "status.json").write_text(json.dumps({
        "written": time.time(), "version": "t", "uptime_s": 1, "poll_interval_s": 1, "last_poll_age_s": 0,
        "polls_ok": 1, "polls_failed": 0, "polls_skipped": 0, "events_triggered": 0, "last_event_at": None,
        "feed_ok_age_s": 0, "active_alerts": [], "telegram_configured": True,
        "telegram_delivery": {"ok": True}, "config_hash": settings.config_hash(cfg), "feed_items_at": time.time(),
        "feed_items": [], "devices": {}}))
    srv = web.serve(str(cfg), "pw", "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path, cfg, srv
    srv.shutdown(); srv.server_close()


def _login(c, base):
    c.post(base + "/login", data={"password": "pw"})
    page = c.get(base + "/settings").text
    return page, re.search(r'name="csrf" value="([^"]+)"', page)[1], re.search(r'name="base" value="([^"]+)"', page)[1]


def _form_from(page: str) -> dict:
    f = {}
    for t in re.findall(r"<input[^>]*>", page):
        n = re.search(r'name="([^"]+)"', t)
        if n and "checkbox" not in t:
            v = re.search(r'value="([^"]*)"', t)
            f[n[1]] = v[1] if v else ""
    for name, body in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        sel = re.search(r'value="(\d)" selected', body)
        f[name] = sel[1] if sel else "1"
    return f


def test_hand_set_values_outside_ui_bounds_do_not_block_other_edits(site):
    base, _, cfg, _ = site
    with httpx.Client() as c:
        page, csrf, h = _login(c, base)
        assert "The service runs exactly these settings" in page
        assert 'id="dev.0.name"' in page and not re.search(r'id="dev\.0\.name"[^>]*inputmode', page)
        f = _form_from(page)
        f["dev.0.model"] = "Shelly 1 (garage)"
        r = c.post(base + "/settings", data=f)
    assert r.status_code == 303, r.text[:300]
    saved = load_config(cfg)
    assert saved.feed.poll_interval_s == 1 and saved.devices[0].model == "Shelly 1 (garage)"


def test_concurrent_saves_never_lose_an_edit_silently(site):
    base, _, cfg, _ = site
    with httpx.Client() as a, httpx.Client() as b:
        pa, _, _ = _login(a, base)
        pb, _, _ = _login(b, base)
        fa, fb = _form_from(pa), _form_from(pb)
        fa["feed.max_age_min"], fb["feed.down_after_s"] = "20", "120"
        res = {}
        ta = threading.Thread(target=lambda: res.update(a=a.post(base + "/settings", data=fa)))
        tb = threading.Thread(target=lambda: res.update(b=b.post(base + "/settings", data=fb)))
        ta.start(); tb.start(); ta.join(); tb.join()
    codes = sorted(r.status_code for r in res.values())
    assert codes == [303, 409]
    loser = next(r for r in res.values() if r.status_code == 409)
    assert "NOT applied" in loser.text
    c = load_config(cfg)
    assert (c.feed.max_age_min, c.feed.down_after_s) in ((20, 60), (10, 120))


def test_unreadable_database_is_shown_not_a_500(site):
    base, tmp, _, _ = site
    (tmp / "data" / "sherpa.db").write_bytes(b"this is not a database at all" * 50)
    for p in ("/", "/events", "/alerts", "/stats", "/events.csv", "/metrics"):
        r = httpx.get(base + p)
        assert r.status_code == 200, p
    d = httpx.get(base + "/").text
    assert "database cannot be read" in d and 'http-equiv="refresh"' in d


def test_connection_cap_and_odd_cookies_and_chunked(site):
    base, *_ = site
    port = int(base.rsplit(":", 1)[1])
    idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(web.MAX_CONNECTIONS)]
    try:
        time.sleep(0.3)
        assert httpx.get(base + "/healthz").status_code == 503
    finally:
        for s in idle:
            s.close()
    for _ in range(50):
        try:
            if httpx.get(base + "/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    with httpx.Client() as c:
        r = c.post(base + "/login", data={"password": "pw"})
        tok = re.search(r"sherpa_sid=([^;]+)", r.headers["set-cookie"])[1]
    odd = {"Cookie": f'prefs={{"theme":"dark"}}; lang=sl SI; sherpa_sid={tok}'}
    assert "Save settings" in httpx.get(base + "/settings", headers=odd).text
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(b"POST /login HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nGET /\r\n0\r\n\r\n")
    data = s.recv(4096)
    s.close()
    assert data.startswith(b"HTTP/1.1 411")


def test_non_ascii_csrf_is_a_403_not_a_500(site):
    base, *_ = site
    with httpx.Client() as c:
        _login(c, base)
        r = c.post(base + "/settings", data={"csrf": "ünïcode", "base": "x"})
    assert r.status_code == 403


def test_total_outage_bucket_is_red_not_invisible(site):
    base, tmp, _, _ = site
    s = Store(str(tmp / "data" / "sherpa.db"))
    m = (datetime.now(UTC) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M")
    s.add_poll_minutes({m: {"ok": 0, "failed": 30, "skipped": 0, "lat_sum": 0, "lat_n": 0, "lat_max": 0}})
    s.close()
    d = httpx.get(base + "/").text
    assert "no answer from the feed" in d and 'class="bar bad"' in d


def test_stuck_pending_counts_as_a_problem(site):
    base, tmp, _, _ = site
    s = Store(str(tmp / "data" / "sherpa.db"))
    t = datetime.now(UTC) - timedelta(minutes=10)
    s.record("stuck", "Požar 5. 11. 2026", "", t, t, "pending")
    s.close()
    assert "1 with delivery problems" in httpx.get(base + "/").text
