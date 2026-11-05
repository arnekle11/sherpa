"""Ops review: invalid config fallback, clock skew, alert clearing, preflight database check."""
import asyncio
import re
import threading
import time
from email.utils import formatdate

import httpx
import pytest
import yaml

from sherpa import web
from sherpa.app import App
from sherpa.cli import main as cli
from sherpa.config import (AlertsCfg, AppCfg, DeviceCfg, FeedCfg, load_config, load_config_with_fallback,
                           save_last_good)
from sherpa.mock import MockFeed


def _write_cfg(path, db, **feed):
    path.write_text(yaml.safe_dump({"feed": {"url": "http://x/feed", **feed}, "db_path": str(db),
                                    "devices": [{"name": "led-1", "ip": "127.0.0.1:9", "generation": 1}]}))


def test_fallback_loading_and_last_good_copy(tmp_path):
    cfg, fb = tmp_path / "config.yaml", tmp_path / "data" / "last-good.yaml"
    _write_cfg(cfg, tmp_path / "x.db")
    save_last_good(cfg, fb)
    assert fb.read_bytes() == cfg.read_bytes()
    before = fb.stat().st_mtime_ns
    save_last_good(cfg, fb)                      # unchanged: not rewritten
    assert fb.stat().st_mtime_ns == before
    cfg.write_text("feed: {url: http://x/feed, pol_interval: 2}\n")   # hand-edit typo
    loaded, err = load_config_with_fallback(cfg, fb)
    assert loaded.devices[0].name == "led-1" and "pol_interval" in err
    with pytest.raises(Exception):
        load_config_with_fallback(cfg, tmp_path / "missing.yaml")


def test_service_on_fallback_alerts_until_fixed(tmp_path):
    async def go():
        feed = MockFeed().start()
        cfgfile = tmp_path / "config.yaml"
        cfg = AppCfg(feed=FeedCfg(url=feed.url, poll_interval_s=0.2, timeout_s=1),
                     devices=[DeviceCfg(name="d", ip="127.0.0.1:9", generation=1)],
                     alerts=AlertsCfg(device_check_interval_s=0), db_path=str(tmp_path / "x.db"))
        cfgfile.write_text("not: [valid")
        app = App(cfg, telegram=None, config_path=str(cfgfile), config_error="mapping values are not allowed here")
        app.config_poll_s = 0.1
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.4)
        raised = app.alerts.is_active("config-invalid")
        cfgfile.write_text(yaml.safe_dump(cfg.model_dump()))  # fixed, identical to what runs: clear, no restart
        await asyncio.sleep(0.5)
        cleared = not app.alerts.is_active("config-invalid") and not app.restart_requested
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop()
        return raised, cleared, app.alerts.sent
    raised, cleared, sent = asyncio.run(go())
    assert raised and cleared
    assert any("Restore last good settings" in m for m in sent)


def test_web_survives_invalid_config_and_restores(tmp_path):
    cfg, fb = tmp_path / "config.yaml", tmp_path / "data" / "last-good.yaml"
    _write_cfg(cfg, tmp_path / "data" / "sherpa.db")
    good = cfg.read_text()
    save_last_good(cfg, fb)
    cfg.write_text("feed: [broken")
    srv = web.serve(str(cfg), "pw", "127.0.0.1", 0, fallback=str(fb))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with httpx.Client() as c:
            assert c.get(base + "/").status_code == 200
            c.post(base + "/login", data={"password": "pw"})
            page = c.get(base + "/settings").text
            assert "The settings file is invalid" in page and "Restore last good settings" in page
            csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
            assert c.post(base + "/settings/restore", data={"csrf": "x"}).status_code == 403
            r = c.post(base + "/settings/restore", data={"csrf": csrf})
            assert r.headers["location"] == "/settings?done=restored"
            assert "last good settings were restored" in c.get(base + "/settings?done=restored").text
        assert cfg.read_text() == good and (tmp_path / "config.yaml.broken").read_text() == "feed: [broken"
        load_config(cfg)
    finally:
        srv.shutdown(); srv.server_close()


def _app(tmp_path):
    return App(AppCfg(feed=FeedCfg(url="http://127.0.0.1:9/feed"),
                      devices=[DeviceCfg(name="d", ip="127.0.0.1:9", generation=1)],
                      db_path=str(tmp_path / "x.db")), telegram=None)


def test_clock_skew_alert(tmp_path):
    app = _app(tmp_path)
    app.fetcher.last_server_date = formatdate(time.time() - 300, usegmt=True)  # VM 5 min ahead
    app._check_clock()
    assert app.alerts.is_active("clock-skew") and 295 < app.clock_skew_s < 305
    assert "ahead" in app.alerts.sent[-1]
    app.fetcher.last_server_date = formatdate(time.time(), usegmt=True)
    app._check_clock()
    assert not app.alerts.is_active("clock-skew")
    app.fetcher.last_server_date = "garbage"
    app._check_clock()  # never raises


def test_internal_alert_clears_after_a_good_poll(tmp_path):
    app = _app(tmp_path)
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")

    app._poll = flaky
    asyncio.run(app.poll_once())
    assert app.alerts.is_active("internal") and not (tmp_path / "health").exists()
    asyncio.run(app.poll_once())
    assert not app.alerts.is_active("internal") and (tmp_path / "health").exists()


def test_check_reports_unwritable_database(tmp_path, capsys):
    feed = MockFeed().start()
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"feed": {"url": feed.url, "expected_link_contains": None},
                                   "db_path": str(blocker / "sherpa.db"),
                                   "devices": [{"name": "d", "ip": "127.0.0.1:9", "generation": 1,
                                                "timeout_s": 0.3, "attempts": 1}]}))
    try:
        assert cli(["check", "-c", str(cfg)]) == 1
    finally:
        feed.stop()
    out = capsys.readouterr().out
    assert "feed OK" in out and "clock: VM" in out and "database FAILED" in out
