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



IDRIJA = (46.0025, 14.0303)
RULE = dict(title_contains=["Tehnična pomoč", "Dimniški požar"], near_lat=IDRIJA[0], near_lon=IDRIJA[1], radius_km=2.5)
TEXT_RULE = dict(title_contains=["Tehnična pomoč", "Dimniški požar"], require_any=["Idriji", "mestu Idrija"],
                 ignore_phrases=["PGD Idrija", "NMP Idrija", "občini Idrija"])


@pytest.mark.parametrize("title,loc,desc,sent", [
    ("Tehnična pomoč 17. 11. 2026 10:00", (46.004, 14.027), "", True),          # in town, no description yet
    ("Dimniški požar 17. 11. 2026 10:00", (45.995, 14.041), "", True),
    ("TEHNICNA POMOC 1", (46.001, 14.031), "", True),                           # case + diacritics in the title
    ("Tehnična pomoč 17. 11. 2026 10:00", (45.908, 14.023), "", False),         # Črni vrh, ~10 km: not the town
    ("Dimniški požar 17. 11. 2026 10:00", (46.130, 13.960), "", False),         # Cerkno
    ("Dimniški požar 17. 11. 2026 10:00", None, "", False),                     # no location: fail closed
    ("Požar v naravi 17. 11. 2026 10:00", (46.130, 13.960), "", True),          # other types: not restricted
    ("Prometna nesreča 17. 11. 2026 10:00", None, "", True),
])
def test_rule_verdict_by_location(title, loc, desc, sent):
    from sherpa.config import PushRule
    from sherpa.pushfeeds import Location, matching_rule, rule_verdict
    rule = matching_rule([PushRule(**RULE)], title)
    assert (rule_verdict(rule, title, desc, Location(*loc) if loc else None) is None) is sent


@pytest.mark.parametrize("desc,sent", [
    ("Gasilci PGD Idrija so črpali vodo iz kleti v Idriji.", True),
    ("Na cesti Črni vrh–Col, občina Idrija. Gasilci PGD Idrija, NMP Idrija.", False),
    ("", False),
])
def test_rule_verdict_by_text(desc, sent):
    from sherpa.config import PushRule
    from sherpa.pushfeeds import matching_rule, rule_verdict
    t = "Tehnična pomoč 1"
    assert (rule_verdict(matching_rule([PushRule(**TEXT_RULE)], t), t, desc, None) is None) is sent


def test_rule_needs_a_place():
    from sherpa.config import PushRule
    with pytest.raises(ValueError):
        PushRule(title_contains=["x"])
    with pytest.raises(ValueError):
        PushRule(title_contains=["x"], near_lat=46.0)


def test_rules_use_the_map_location_and_wait_briefly_for_it(tmp_path, monkeypatch):
    from sherpa import pushfeeds
    monkeypatch.setattr(pushfeeds, "LOCATION_WAIT_S", 4)

    async def go():
        main, push, sh, po = MockFeed().start(), MockFeed().start(), MockShelly(2).start(), MockPushover().start()
        app = _app(tmp_path, main, push, sh, po, poll_interval_s=2, rules=[RULE], location_url=push.location_url)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.8)
        g1 = push.add_item("Dimniški požar 17. 11. 2026 10:00", "")          # Cerkno: filtered
        push.set_location(g1, 46.130, 13.960, "Cerkno")
        g2 = push.add_item("Tehnična pomoč 17. 11. 2026 10:05", "")          # in town: sent, with the občina
        push.set_location(g2, 46.004, 14.027, "Idrija")
        push.add_item("Požar 17. 11. 2026 10:10", "Grmada.")                 # not restricted: sent, no location needed
        g4 = push.add_item("Tehnična pomoč 17. 11. 2026 10:15", "")          # location appears a bit later: sent
        push.add_item("Dimniški požar 17. 11. 2026 10:20", "")               # never a location: dropped (fail closed)
        await asyncio.sleep(2.5)
        early = [m["title"] for m in po.messages]
        push.set_location(g4, 46.000, 14.033, "Idrija")
        await asyncio.sleep(7)
        msgs, st = {m["title"]: m for m in po.messages}, app.push_feeds[0].snapshot()
        app.stop(); await asyncio.wait_for(t, 15)
        for x in (main, push, sh, po): x.stop()
        return early, msgs, st

    early, msgs, st = asyncio.run(go())
    assert "Tehnična pomoč 17. 11. 2026 10:15" not in early
    assert sorted(msgs) == ["Požar 17. 11. 2026 10:10", "Tehnična pomoč 17. 11. 2026 10:05",
                            "Tehnična pomoč 17. 11. 2026 10:15"], sorted(msgs)
    assert "občina Idrija" in msgs["Tehnična pomoč 17. 11. 2026 10:05"]["message"]
    assert st["filtered"] == 2 and st["notified"] == 3
