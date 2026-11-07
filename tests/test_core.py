import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from sherpa.alerts import AlertManager, LogNotifier
from sherpa.app import App
from sherpa.config import AppCfg, DeviceCfg, FeedCfg, AlertsCfg, load_config
from sherpa.devices import make_device
from sherpa.mock import MockFeed, MockShelly
from sherpa.selftest import run_selftest
from sherpa.store import Store


def test_end_to_end_selftest():
    assert asyncio.run(run_selftest(verbose=False)) == []


def test_store_dedupe_persists_across_reopen(tmp_path):
    p = str(tmp_path / "s.db")
    now = datetime.now(UTC)
    s = Store(p)
    assert s.record("g1", "t", "l", now - timedelta(seconds=7), now, "triggered") is True
    assert s.record("g1", "t2", "l", now, now, "triggered") is False
    s.close()
    s2 = Store(p)
    assert s2.seen("g1")
    assert s2.lag_stats()["median"] == pytest.approx(7)


def test_alert_manager_raise_once_then_reminder_then_clear():
    t = [0.0]
    am = AlertManager(LogNotifier(), repeat_s=100, host="h", clock=lambda: t[0])
    am.raise_("k", "down"); am.raise_("k", "down")
    assert len(am.sent) == 1
    t[0] = 150; am.raise_("k", "down")
    assert len(am.sent) == 2 and "still" in am.sent[1]
    am.clear("k", "up"); am.clear("k", "up")
    assert len(am.sent) == 3 and not am.is_active("k")


@pytest.mark.parametrize("gen", [1, 2])
def test_device_rejects_on_without_timer(gen):
    """If a relay is ON but no auto-off timer is armed, that is a FAILURE, not success."""
    async def go():
        s = MockShelly(gen).start()
        orig = s._on
        s._on = lambda timer: orig(None)  # device ignores the timer
        dev = make_device(DeviceCfg(name="d", ip=s.hostport, generation=gen, attempts=2, retry_delay_s=0.01),
                          httpx.AsyncClient(trust_env=False))
        try:
            r = await dev.activate()
            assert not r.ok and "timer" in r.detail
        finally:
            s.stop()
    asyncio.run(go())


def test_device_timeout_is_bounded():
    async def go():
        s = MockShelly(1).start()
        s.delay_s = 3
        dev = make_device(DeviceCfg(name="d", ip=s.hostport, generation=1, attempts=1, timeout_s=0.3),
                          httpx.AsyncClient(trust_env=False))
        try:
            r = await dev.activate()
            assert not r.ok and r.elapsed_s < 1.5
        finally:
            s.stop()
    asyncio.run(go())


def test_slow_feed_skips_ticks_never_overlaps():
    async def go():
        feed = MockFeed().start(); feed.mode = "slow"
        sh = MockShelly(1).start()
        with tempfile.TemporaryDirectory() as d:
            cfg = AppCfg(feed=FeedCfg(url=feed.url, poll_interval_s=0.2, timeout_s=0.5),
                         devices=[DeviceCfg(name="d", ip=sh.hostport, generation=1)],
                         alerts=AlertsCfg(device_check_interval_s=0), db_path=f"{d}/x.db")
            app = App(cfg, telegram=None)
            active = max_active = 0
            orig = app._poll

            async def counted():
                nonlocal active, max_active
                active += 1; max_active = max(max_active, active)
                try:
                    await orig()
                finally:
                    active -= 1
            app._poll = counted
            t = asyncio.create_task(app.run())
            await asyncio.sleep(2.0)
            app.stop(); await asyncio.wait_for(t, 15)
            assert max_active == 1 and app.polls_skipped >= 1 and app.polls_failed >= 2
        feed.stop(); sh.stop()
    asyncio.run(go())


def test_crash_between_record_and_dispatch_is_recovered():
    async def go():
        feed, sh = MockFeed().start(), MockShelly(1).start()
        with tempfile.TemporaryDirectory() as d:
            cfg = AppCfg(feed=FeedCfg(url=feed.url, poll_interval_s=0.2, timeout_s=1),
                         devices=[DeviceCfg(name="d", ip=sh.hostport, generation=1)],
                         alerts=AlertsCfg(device_check_interval_s=0), db_path=f"{d}/x.db")
            st = Store(cfg.db_path)
            now = datetime.now(UTC)
            st.record("crashed", "t", "", now, now - timedelta(seconds=30), "pending")
            st.close()
            app = App(cfg, telegram=None)
            t = asyncio.create_task(app.run())
            for _ in range(60):
                if sh.is_on: break
                await asyncio.sleep(0.05)
            app.stop(); await asyncio.wait_for(t, 15)
            assert sh.is_on
        feed.stop(); sh.stop()
    asyncio.run(go())


def test_example_config_loads():
    cfg = load_config(Path(__file__).parents[1] / "config.example.yaml")
    assert cfg.feed.poll_interval_s == 2 and cfg.devices[0].on_duration_s == 300


def test_unknown_config_key_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("feed: {url: http://x, pol_interval_s: 1}\ndevices: [{name: a, ip: 1.2.3.4, generation: 1}]\n")
    with pytest.raises(Exception):
        load_config(p)


@pytest.mark.parametrize("component", ["rgb", "rgbw", "light"])
def test_gen2_light_components_switch_on_with_device_timer(component):
    """An rgb-profile Shelly (e.g. Plus RGBW PM) has no Switch.*; the adapter must use its own component."""
    async def go():
        s = MockShelly(2, component=component).start()
        try:
            async with httpx.AsyncClient(trust_env=False) as c:
                r = await make_device(DeviceCfg(name="d", ip=s.hostport, generation=2, component=component,
                                                attempts=1, retry_delay_s=0.01, on_duration_s=60), c).activate()
                assert r.ok and s.is_on and s.timer_duration == 60
                # default component against this device fails visibly instead of pretending
                bad = await make_device(DeviceCfg(name="d", ip=s.hostport, generation=2,
                                                  attempts=1, retry_delay_s=0.01), c).activate()
                assert not bad.ok
        finally:
            s.stop()
    asyncio.run(go())
