import asyncio

import pytest

from sherpa.app import App
from sherpa.config import AlertsCfg, AppCfg, DeviceCfg, FeedCfg, PushFeedCfg
from sherpa.mock import MockFeed, MockPushover, MockShelly
from sherpa.pushfeeds import PushoverNotifier


def _app(tmp_path, main, push, sh, po, **pf):
    cfg = AppCfg(feed=FeedCfg(url=main.url, poll_interval_s=0.5, timeout_s=1),
                 push_feeds=[PushFeedCfg(name="Idrija", url=push.url, **pf)],
                 devices=[DeviceCfg(name="d", ip=sh.hostport, generation=2)],
                 alerts=AlertsCfg(device_check_interval_s=0), db_path=str(tmp_path / "x.db"))
    app = App(cfg, telegram=None, deadman=None, pushover=PushoverNotifier("TOKEN", "USERKEY", po.api_url) if po else None)
    return app


def test_push_feed_config_floor_and_unique_names():
    with pytest.raises(ValueError):
        PushFeedCfg(name="a", url="http://x", poll_interval_s=1)  # never faster than the main feed's 2 s floor
    with pytest.raises(ValueError):
        PushFeedCfg(name="a/b", url="http://x")
    base = dict(feed=FeedCfg(url="http://x"), devices=[DeviceCfg(name="d", ip="1.1.1.1", generation=2)])
    with pytest.raises(ValueError):
        AppCfg(**base, push_feeds=[PushFeedCfg(name="a", url="http://x"), PushFeedCfg(name="a", url="http://y")])


def test_new_item_is_pushed_once_old_ones_are_not_and_the_light_stays_untouched(tmp_path):
    async def go():
        main, push, sh, po = MockFeed().start(), MockFeed().start(), MockShelly(2).start(), MockPushover().start()
        push.add_item("Stara nesreča", "Idrija", age_s=3600)          # older than max_age at startup: ignored
        app = _app(tmp_path, main, push, sh, po, poll_interval_s=2)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(1.0)
        push.add_item("Prometna nesreča", "Idrija")
        await asyncio.sleep(5.5)                                       # several polls: still exactly one message
        msgs, frames, on = list(po.messages), list(sh.frames), sh.is_on
        status = app.push_feeds[0].snapshot()
        app.stop(); await asyncio.wait_for(t, 15)
        for x in (main, push, sh, po): x.stop()
        return msgs, frames, on, status, app.events_triggered

    msgs, frames, on, status, triggered = asyncio.run(go())
    assert len(msgs) == 1, msgs
    m = msgs[0]
    assert m["title"] == "Prometna nesreča" and "Idrija" in m["message"] and m["priority"] == "1"
    assert m["token"] == "TOKEN" and m["user"] == "USERKEY" and m["url"].startswith("https://spin3.sos112.si/")
    assert not on and not frames and triggered == 0, "a push feed never switches a device"
    assert status["ok"] and status["notified"] == 1


def test_failed_push_is_retried_then_alerts_and_recovers(tmp_path):
    async def go():
        main, push, sh, po = MockFeed().start(), MockFeed().start(), MockShelly(2).start(), MockPushover().start()
        po.fail = True
        app = _app(tmp_path, main, push, sh, po, poll_interval_s=2)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.8)
        push.add_item("Požar", "Idrija")
        await asyncio.sleep(8)                                         # >= 3 failed polls
        raised = app.alerts.is_active("pushover")
        none_yet = len(po.messages)
        po.fail = False
        await asyncio.sleep(3)
        res = (len(po.messages), app.alerts.is_active("pushover"))
        app.stop(); await asyncio.wait_for(t, 15)
        for x in (main, push, sh, po): x.stop()
        return raised, none_yet, res

    raised, none_yet, res = asyncio.run(go())
    assert raised and none_yet == 0
    assert res == (1, False), "delivered after the outage, exactly once, alert cleared"


def test_unreadable_push_feed_alerts_and_does_not_disturb_the_main_feed(tmp_path):
    async def go():
        main, push, sh, po = MockFeed().start(), MockFeed().start(), MockShelly(2).start(), MockPushover().start()
        push.mode = "500"
        app = _app(tmp_path, main, push, sh, po, poll_interval_s=2)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.5)
        main.add_item("Požar")
        await asyncio.sleep(8)
        res = (app.alerts.is_active("pushfeed:Idrija"), sh.is_on)
        app.stop(); await asyncio.wait_for(t, 15)
        for x in (main, push, sh, po): x.stop()
        return res

    assert asyncio.run(go()) == (True, True), "push feed down: alert, and the real alert still lights the LED"


def test_without_pushover_credentials_it_says_so(tmp_path):
    async def go():
        main, push, sh = MockFeed().start(), MockFeed().start(), MockShelly(2).start()
        app = _app(tmp_path, main, push, sh, None, poll_interval_s=2)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.8)
        push.add_item("Požar", "Idrija")
        await asyncio.sleep(3)
        res = app.alerts.is_active("pushover")
        app.stop(); await asyncio.wait_for(t, 15)
        for x in (main, push, sh): x.stop()
        return res

    assert asyncio.run(go()) is True
