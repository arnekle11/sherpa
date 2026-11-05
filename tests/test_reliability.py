"""Poll statistics, dead-man's switch, startup device probe, change descriptions."""
import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime

import httpx

from sherpa.app import App
from sherpa.config import AlertsCfg, AppCfg, DeviceCfg, FeedCfg, describe_changes
from sherpa.deadman import Deadman
from sherpa.mock import MockFeed, MockShelly
from sherpa.pollstats import PollStats
from sherpa.store import Store


def test_pollstats_buckets_drain_and_requeue():
    ps = PollStats()
    t = datetime(2026, 11, 5, 20, 1, 30, tzinfo=UTC)
    ps.record(True, 0.2, t); ps.record(True, 0.4, t); ps.record(False, None, t); ps.skipped(2, t)
    rows = ps.drain()
    r = rows["2026-11-05T20:01"]
    assert (r["ok"], r["failed"], r["skipped"], r["lat_n"]) == (2, 1, 2, 2) and r["lat_max"] == 0.4
    assert ps.drain() == {}
    ps.requeue(rows); ps.record(True, 0.1, t)
    assert ps.drain()["2026-11-05T20:01"]["ok"] == 3
    assert ps.latency_summary()["max_ms"] == 400


def test_poll_minutes_upsert_is_additive(tmp_path):
    s = Store(str(tmp_path / "x.db"))
    row = {"ok": 2, "failed": 1, "skipped": 0, "lat_sum": 0.5, "lat_n": 2, "lat_max": 0.3}
    s.add_poll_minutes({"2026-11-05T20:01": row})
    s.add_poll_minutes({"2026-11-05T20:01": dict(row, lat_max=0.9)})
    r = s.db.execute("SELECT * FROM poll_log").fetchone()
    assert (r["ok"], r["failed"], r["lat_n"], r["lat_max"]) == (4, 2, 4, 0.9)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_deadman_pings_only_when_healthy_and_never_logs_url(caplog):
    secret = "https://hc.example/ping/SECRET-UUID"
    hits = []
    healthy = [True]

    def handler(req):
        hits.append(str(req.url))
        return httpx.Response(200 if len(hits) < 2 else 500)

    async def go():
        d = Deadman(secret, 60, lambda: healthy[0], client=_client(handler))
        assert await d.ping_once() is True
        assert await d.ping_once() is False and d.last_error == "HTTP 500"
        healthy[0] = False
        assert await d.ping_once() is False and len(hits) == 2  # withheld: a hung loop must look dead
        return d.snapshot()
    from sherpa.cli import setup_logging
    setup_logging("DEBUG")  # the service's own logging setup, at its most verbose
    with caplog.at_level(logging.DEBUG):
        snap = asyncio.run(go())
    assert snap["configured"] and snap["ok"] == 1 and snap["failed"] == 1
    assert "SECRET" not in caplog.text


def test_deadman_connection_error_reports_type_only():
    def handler(req):
        raise httpx.ConnectError("cannot reach https://hc.example/ping/SECRET-UUID")

    async def go():
        d = Deadman("https://hc.example/ping/SECRET-UUID", 60, lambda: True, client=_client(handler))
        await d.ping_once()
        return d.last_error
    assert asyncio.run(go()) == "ConnectError"


def test_describe_changes():
    base = AppCfg(feed=FeedCfg(url="http://x"), devices=[DeviceCfg(name="a", ip="1.1.1.1", generation=1)])
    new = base.model_copy(deep=True)
    new.feed.max_age_min = 30
    new.devices[0].ip = "2.2.2.2"
    new.devices.append(DeviceCfg(name="b", ip="3.3.3.3", generation=2))
    ch = describe_changes(base, new)
    assert "feed.max_age_min: 10.0 -> 30" in ch
    assert "device a: ip 1.1.1.1 -> 2.2.2.2" in ch and "device b added (3.3.3.3)" in ch
    assert describe_changes(base, base) == []


def test_startup_probe_and_status_extras(tmp_path):
    async def go():
        feed, sh = MockFeed().start(), MockShelly(1).start()
        cfg = AppCfg(feed=FeedCfg(url=feed.url, poll_interval_s=0.2, timeout_s=1),
                     devices=[DeviceCfg(name="good", ip=sh.hostport, generation=1),
                              DeviceCfg(name="dead", ip="127.0.0.1:9", generation=1, timeout_s=0.3, attempts=1)],
                     alerts=AlertsCfg(device_check_interval_s=600), db_path=str(tmp_path / "x.db"))
        feed.add_item("Prometna nesreča 5. 11. 2026 20:41", "Ljubljana", age_s=7200)  # old: ignored at start
        app = App(cfg, telegram=None, deadman=None)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(4.5)  # startup probe runs after ~3 s, long before the 600 s interval
        assert app.alerts.is_active("offline:dead") and not app.alerts.is_active("offline:good")
        app._status_written = 0; app._write_status()
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop(); sh.stop()
    asyncio.run(go())
    snap = json.loads((tmp_path / "status.json").read_text())
    assert snap["telegram_configured"] is False and snap["deadman"] == {"configured": False}
    assert snap["latency"]["n"] >= 1 and snap["feed_items"][0]["title"].startswith("Prometna")
    assert snap["devices"]["dead"]["ok"] is False and snap["devices"]["good"]["ok"] is True
    rows = sqlite3.connect(tmp_path / "x.db").execute("SELECT SUM(ok) FROM poll_log").fetchone()
    assert rows[0] >= 1  # flushed on shutdown


def test_command_validation_and_take(tmp_path):
    from sherpa import commands
    import time as _t
    d = tmp_path / "commands"
    commands.submit(d, "telegram_test", "192.168.1.165")
    commands.submit(d, "test_event", "192.168.1.165", duration_s=10)
    (d / "evil.json").write_text(json.dumps({"kind": "rm -rf", "ts": _t.time()}))
    (d / "stale.json").write_text(json.dumps({"kind": "telegram_test", "ts": _t.time() - 3600}))
    (d / "long.json").write_text(json.dumps({"kind": "test_event", "ts": _t.time(), "duration_s": 300}))
    (d / "garbage.json").write_text("{not json")
    got = commands.take(d)
    assert [c["kind"] for c in got] == ["telegram_test", "test_event"] and got[1]["duration_s"] == 10
    assert list(d.glob("*.json")) == []  # everything claimed or discarded: nothing can run twice
    assert commands.take(d) == []
    import pytest
    with pytest.raises(ValueError):
        commands.submit(d, "test_event", "x", duration_s=0)


def test_web_command_runs_test_event_once_with_cooldown(tmp_path):
    from sherpa import commands

    async def go():
        feed, sh = MockFeed().start(), MockShelly(2).start()
        cfg = AppCfg(feed=FeedCfg(url=feed.url, poll_interval_s=0.5, timeout_s=1),
                     devices=[DeviceCfg(name="d", ip=sh.hostport, generation=2)],
                     alerts=AlertsCfg(device_check_interval_s=0), db_path=str(tmp_path / "x.db"))
        app = App(cfg, telegram=None, deadman=None)
        app.command_poll_s = 0.1
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.3)
        d = commands.command_dir(cfg.db_path)
        commands.submit(d, "test_event", "10.0.0.1", duration_s=5)
        commands.submit(d, "test_event", "10.0.0.1", duration_s=5)  # second one: refused by cooldown
        commands.submit(d, "telegram_test", "10.0.0.1")
        for _ in range(50):
            if sh.is_on: break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        on = sh.is_on
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop(); sh.stop()
        return on, app.alerts.sent
    on, sent = asyncio.run(go())
    assert on
    assert sum("switching every device ON for 5 s" in m for m in sent) == 1
    assert any("refused" in m for m in sent) and any("NOT configured" in m for m in sent)
    rows = sqlite3.connect(tmp_path / "x.db").execute("SELECT state, is_test FROM events").fetchall()
    assert rows == [("triggered", 1)]
