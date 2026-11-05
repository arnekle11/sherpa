"""Regression tests for the review findings on the web-tools / dead-man features."""
import asyncio
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sherpa import commands
from sherpa.alerts import AlertManager
from sherpa.app import App, TestRefused
from sherpa.config import AlertsCfg, AppCfg, DeviceCfg, FeedCfg
from sherpa.devices import DeviceResult, DeviceState
from sherpa.mock import MockFeed, MockShelly
from sherpa.store import Store


def _cfg(tmp_path, feed, ip="127.0.0.1:9", gen=2, **dev):
    return AppCfg(feed=FeedCfg(url=feed.url, poll_interval_s=0.2, timeout_s=1),
                  devices=[DeviceCfg(name="d", ip=ip, generation=gen, **dev)],
                  alerts=AlertsCfg(device_check_interval_s=0), db_path=str(tmp_path / "x.db"))


async def _wait(cond, timeout=5.0):
    t = time.monotonic()
    while not cond():
        if time.monotonic() - t > timeout:
            return False
        await asyncio.sleep(0.05)
    return True


# --- 1. a test event must never cut a real alert short ------------------------------------------
def test_test_event_refused_while_real_alert_lights_the_led(tmp_path):
    async def go():
        feed, sh = MockFeed().start(), MockShelly(2).start()
        app = App(_cfg(tmp_path, feed, sh.hostport), telegram=None)
        app.command_poll_s = 0.1
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.5)
        feed.add_item("Požar 5. 11. 2026 21:00")           # a real emergency
        assert await _wait(lambda: sh.is_on)
        commands.submit(commands.command_dir(app.cfg.db_path), "test_event", "10.0.0.9", duration_s=3)
        assert await _wait(lambda: any("refused" in m for m in app.alerts.sent))
        await asyncio.sleep(0.5)
        remaining = sh._remaining()
        with pytest.raises(TestRefused):  # CLI path without --force, same rule
            await app.send_test_event(3)
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop(); sh.stop()
        return remaining, app.alerts.sent
    remaining, sent = asyncio.run(go())
    assert remaining > 250, remaining  # still the real 300 s timer, not 3 s
    assert any("real event was seen" in m for m in sent)


def test_test_event_refused_when_a_relay_is_already_on(tmp_path):
    async def go():
        feed, sh = MockFeed().start(), MockShelly(2).start()
        sh._on(200)  # switched on by something else (e.g. by hand at the device)
        app = App(_cfg(tmp_path, feed, sh.hostport), telegram=None)
        reason = await app.test_event_blocker()
        await app._dev_client.aclose(); await app.fetcher.aclose()
        feed.stop(); sh.stop()
        return reason
    assert "already ON" in asyncio.run(go())


class _ScriptedDevice:
    """Short-timer (test) requests hang 'in flight' and are applied LATE, after whatever came meanwhile:
    the worst ordering a real network can produce."""
    def __init__(self, cfg):
        self.cfg, self.name = cfg, cfg.name
        self.applied: list[int] = []
        self.on = False

    async def read_state(self):
        return DeviceState(self.on, self.on, None)

    async def activate(self, duration_s=None):
        d = duration_s or self.cfg.on_duration_s
        if d < 60:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:  # the client gave up, but the request already left: it lands late
                asyncio.get_running_loop().call_later(0.2, self.applied.append, d)
                raise
        self.applied.append(d); self.on = True
        return DeviceResult(self.name, True, 1, "ok", 0.0)


def test_real_event_during_test_cancels_it_and_real_timer_wins(tmp_path):
    async def go():
        feed = MockFeed().start()
        app = App(_cfg(tmp_path, feed, timeout_s=0.2), telegram=None)
        dev = _ScriptedDevice(app.cfg.devices[0])
        app.devices[:] = [dev]
        app.command_poll_s = 0.1
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.4)
        commands.submit(commands.command_dir(app.cfg.db_path), "test_event", "10.0.0.9", duration_s=5)
        assert await _wait(lambda: app._test_task is not None)
        await asyncio.sleep(0.3)                              # test request now "in flight"
        feed.add_item("Prometna nesreča 5. 11. 2026 21:05")  # real emergency arrives
        assert await _wait(lambda: 300 in dev.applied)
        await asyncio.sleep(2.0)                              # late test request + delayed re-arm
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop()
        return dev.applied, app.alerts.sent
    applied, sent = asyncio.run(go())
    assert 5 in applied            # the late test request did land after the real one ...
    assert applied[-1] == 300      # ... but the re-arm made the real timer the last word
    assert any("test event cancelled" in m for m in sent)
    rows = dict(sqlite3.connect(tmp_path / "x.db").execute("SELECT is_test, state FROM events").fetchall())
    assert rows[1] == "abandoned" and rows[0] == "triggered"


# --- 2. an interrupted test is never replayed as a real activation -----------------------------
def test_pending_test_event_is_not_replayed_at_restart(tmp_path):
    async def go():
        feed, sh = MockFeed().start(), MockShelly(1).start()
        cfg = _cfg(tmp_path, feed, sh.hostport, gen=1)
        st = Store(cfg.db_path)
        now = datetime.now(UTC)
        st.record("test:interrupted", "SHERPA test event", "", now, now - timedelta(seconds=20), "pending", is_test=True)
        st.close()
        app = App(cfg, telegram=None)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(1.0)
        on = sh.is_on
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop(); sh.stop()
        return on
    assert asyncio.run(go()) is False
    state = sqlite3.connect(tmp_path / "x.db").execute("SELECT state FROM events").fetchone()[0]
    assert state == "abandoned"


# --- 3. only `sherpa run` pings the dead-man URL ------------------------------------------------
class _Counter(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

    def log_message(self, *a):
        pass


def test_selftest_and_default_app_never_ping_deadman(monkeypatch, tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Counter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("SHERPA_DEADMAN_URL", f"http://127.0.0.1:{srv.server_address[1]}/ping")
    try:
        from sherpa.selftest import run_selftest
        assert asyncio.run(run_selftest(verbose=False)) == []
        feed = MockFeed().start()
        assert App(_cfg(tmp_path, feed), telegram=None).deadman is None
        feed.stop()
    finally:
        srv.shutdown()
    assert _Counter.hits == 0


# --- 4. pathological titles cannot pin the CPU ---------------------------------------------------
def test_event_type_is_linear_on_whitespace_runs():
    from sherpa.webstats import event_type
    t = time.monotonic()
    event_type("x" + " " * 200_000 + "y 5. 11. 2026 20:00")
    assert time.monotonic() - t < 0.2


# --- 5. one bad command file cannot block the queue ---------------------------------------------
def test_bad_command_file_is_discarded_and_others_still_run(tmp_path):
    d = tmp_path / "commands"
    d.mkdir()
    (d / "000-bad.json").write_bytes(b"\xff\xfe\x00garbage")
    commands.submit(d, "telegram_test", "10.0.0.1")
    got = commands.take(d)
    assert [c["kind"] for c in got] == ["telegram_test"] and not list(d.glob("*.json"))


# --- 7. the dead-man stops pinging when every poll crashes ---------------------------------------
def test_poll_health_false_when_polls_keep_crashing(tmp_path):
    async def go():
        feed = MockFeed().start()
        app = App(_cfg(tmp_path, feed), telegram=None)
        app.poll_health_grace_s = 0.6
        healthy_at_start = app._poll_healthy()

        async def boom():
            raise RuntimeError("store broken")
        app._poll = boom
        t = asyncio.create_task(app.run())
        await asyncio.sleep(1.2)
        crashed_healthy = app._poll_healthy()
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop()
        return healthy_at_start, crashed_healthy, app.alerts.is_active("internal")
    assert asyncio.run(go()) == (True, False, True)


# --- 8. Telegram delivery failures are tracked and recorded -------------------------------------
def test_undelivered_alerts_are_tracked_and_logged(monkeypatch):
    history = []

    class Down:
        last_error = "HTTP 401 Unauthorized"

        async def send(self, text):
            return False

    async def go():
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        am = AlertManager(Down(), host="h", on_event=lambda *a: history.append(a))
        worker = asyncio.create_task(am.run())
        am.raise_("feed-down", "feed down")
        await am.drain(5)
        worker.cancel()
        return am.delivery_snapshot()
    snap = asyncio.run(go())
    assert snap["ok"] is False and snap["undelivered"] == 1 and "401" in snap["error"]
    assert history[-1][0] == "undelivered" and "NOT delivered" in history[-1][2]


_real_sleep = asyncio.sleep


async def _fast_sleep(s):
    await _real_sleep(0)


def test_csv_keeps_numbers_numeric_and_has_bom(tmp_path):
    import types
    from sherpa.webexport import events_csv
    s = Store(str(tmp_path / "x.db"))
    now = datetime.now(UTC)
    s.record("g", "-negative title", "", now + timedelta(seconds=1), now, "triggered")  # lag -1.0 (clock skew)
    db = s.db
    out = events_csv(types.SimpleNamespace(query=lambda q, a=(): list(db.execute(q, a))))
    assert out.startswith("﻿")
    row = out.splitlines()[1]
    assert ",-1.0," in row and "'-negative title" in row
