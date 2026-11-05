"""Alert history, status snapshot and config-change restart (the pieces the web UI relies on)."""
import asyncio
import json
import sqlite3

import yaml

from sherpa.app import App
from sherpa.config import AlertsCfg, AppCfg, DeviceCfg, FeedCfg, load_config
from sherpa.mock import MockFeed, MockShelly


def _cfg(tmp_path, feed, sh, **feed_kw):
    return AppCfg(feed=FeedCfg(url=feed.url, poll_interval_s=0.2, timeout_s=1, **feed_kw),
                  devices=[DeviceCfg(name="d", ip=sh.hostport, generation=1)],
                  alerts=AlertsCfg(device_check_interval_s=0), db_path=str(tmp_path / "x.db"))


def test_alerts_are_logged_and_status_snapshot_written(tmp_path):
    async def go():
        feed, sh = MockFeed().start(), MockShelly(1).start()
        app = App(_cfg(tmp_path, feed, sh), telegram=None)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.6)
        app.alerts.raise_("k", "something broke")
        app.alerts.clear("k", "fixed")
        await asyncio.sleep(0.3)
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop(); sh.stop()
        return sh.hostport
    hostport = asyncio.run(go())
    rows = sqlite3.connect(tmp_path / "x.db").execute("SELECT kind,key,text FROM alert_log").fetchall()
    kinds = [r[0] for r in rows]
    assert "raise" in kinds and "clear" in kinds and "info" in kinds  # info = "started ..."
    snap = json.loads((tmp_path / "status.json").read_text())
    assert snap["polls_ok"] >= 1 and snap["devices"]["d"]["ip"] == hostport


def test_alert_history_failure_does_not_break_alerting():
    from sherpa.alerts import AlertManager, LogNotifier

    def boom(*a):
        raise RuntimeError("db gone")
    am = AlertManager(LogNotifier(), host="h", on_event=boom)
    am.raise_("k", "down")
    assert len(am.sent) == 1


def test_valid_config_change_requests_restart_invalid_does_not(tmp_path):
    async def go():
        feed, sh = MockFeed().start(), MockShelly(1).start()
        cfg = _cfg(tmp_path, feed, sh)
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(cfg.model_dump()))
        app = App(cfg, telegram=None, config_path=str(path))
        app.config_poll_s = 0.1
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.4)
        path.write_text("feed: {nonsense")  # invalid: keep running, raise an alert
        await asyncio.sleep(0.5)
        assert not app.restart_requested and app.alerts.is_active("config-invalid")
        raw = cfg.model_dump(); raw["feed"]["max_age_min"] = 42
        path.write_text(yaml.safe_dump(raw))
        await asyncio.wait_for(t, 15)  # the app stops itself
        assert app.restart_requested and not app.alerts.is_active("config-invalid")
        feed.stop(); sh.stop()
    asyncio.run(go())
