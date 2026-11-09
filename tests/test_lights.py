import asyncio
import itertools
import json
import time

import httpx
import pytest

from sherpa import commands, lights
from sherpa.app import App
from sherpa.config import AlertsCfg, AppCfg, DeviceCfg, FeedCfg
from sherpa.devices import make_device
from sherpa.mock import MockFeed, MockShelly


@pytest.mark.parametrize("effect", list(lights.EFFECTS))
def test_effect_frames_are_safe(effect):
    fr = list(itertools.islice(lights.frames(effect, (255, 0, 0), 3), 200))
    assert len(fr) == 200
    for f in fr:
        assert f.step >= lights.MIN_STEP_S - 1e-9, "never faster than 2.5 frames/s (photosensitivity)"
        if f.on:
            assert 10 <= lights.timer_for(f) <= 30, "every ON frame has a short device-side timer"
        if f.rgb is not None:
            assert all(0 <= c <= 255 for c in f.rgb)
        if f.brightness is not None:
            assert 1 <= f.brightness <= 100
    assert any(f.on for f in fr)


def test_effects_really_differ():
    colours = {f.rgb for f in itertools.islice(lights.frames("rainbow", (0, 0, 0), 2), 40)}
    assert len(colours) >= 20
    police = {f.rgb for f in itertools.islice(lights.frames("police", (0, 0, 0), 2), 6)}
    assert police == {(255, 0, 0), (0, 0, 255)}
    assert lights.supports("color", "rainbow") and not lights.supports("bright", "rainbow")
    assert lights.supports("bright", "breathe") and not lights.supports("onoff", "breathe")
    assert lights.supports("onoff", "blink") and lights.supports("onoff", "solid")


def test_light_command_validation_scenes():
    ok = {"kind": "light", "ts": time.time(), "source": "cli", "device": "d", "action": "effect",
          "effect": "disco", "speed": 3, "hold": True, "color": "#FF00aa", "brightness": 50}
    v = commands.validate(dict(ok))
    assert v["hold"] and v["color"] == "#ff00aa" and "duration_s" not in v
    assert commands.validate({**ok, "hold": False, "duration_s": 120})["duration_s"] == 120
    for bad in ({"effect": "nuke"}, {"speed": 9}, {"color": "red"}, {"color": "#12345"}, {"brightness": 0},
                {"brightness": 101}, {"hold": True, "duration_s": 60}, {"hold": False}, {"hold": "yes", "duration_s": None}):
        assert commands.validate({**ok, **bad}) is None, bad


def _app(tmp_path, feed, sh, component="rgb", **dev):
    cfg = AppCfg(feed=FeedCfg(url=feed.url, poll_interval_s=0.5, timeout_s=1),
                 devices=[DeviceCfg(name="d", ip=sh.hostport, generation=2, component=component, **dev)],
                 alerts=AlertsCfg(device_check_interval_s=0), db_path=str(tmp_path / "x.db"))
    app = App(cfg, telegram=None, deadman=None)
    app.command_poll_s = 0.1
    return app, cfg


def _scene_frames(sh):
    return [f for f in sh.frames if "id" in f]


def test_scene_every_on_frame_has_a_timer_and_stop_restores_the_look(monkeypatch, tmp_path):
    monkeypatch.setattr(lights, "MIN_STEP_S", 0.05)

    async def go():
        sh = MockShelly(2, component="rgb").start()
        say = []
        async with httpx.AsyncClient(trust_env=False) as c:
            dev = make_device(DeviceCfg(name="d", ip=sh.hostport, generation=2, component="rgb", attempts=1), c)
            sc = lights.Scenes(say.append)
            sc.start(dev, "rainbow", (255, 255, 255), None, 3, None)
            await asyncio.sleep(2.5)
            running = sc.active()["d"]
            during = (sh.is_on, sh.rgb)
            assert await sc.stop(dev)
            after = (sh.is_on, sh.rgb, sh.brightness, dev.saved_look)
        frames = list(sh.frames)
        sh.stop()
        return running, during, after, frames

    running, during, after, frames = asyncio.run(go())
    assert running["hold"] and running["label"] == "Rainbow" and running["ends"] is None
    assert during[0] and during[1] != [255, 0, 0]
    ons = [f for f in frames if f["on"]]
    assert len(ons) >= 4 and all(0 < f["toggle_after"] <= 30 for f in ons)
    assert len({tuple(f["rgb"]) for f in ons}) >= 4
    assert after == (False, [255, 0, 0], 8, None)  # off, and the device's own colour and brightness are back


def test_hold_renews_a_short_timer_and_timed_colour_ends_by_itself(monkeypatch):
    monkeypatch.setattr(lights, "HOLD_RENEW_S", 0.15)

    async def go():
        sh = MockShelly(2, component="rgb").start()
        async with httpx.AsyncClient(trust_env=False) as c:
            dev = make_device(DeviceCfg(name="d", ip=sh.hostport, generation=2, component="rgb", attempts=1), c)
            sc = lights.Scenes(lambda m: None)
            sc.start(dev, "solid", (0, 0, 255), 40, 2, None)
            await asyncio.sleep(0.8)
            held = [f for f in sh.frames if f.get("on")]
            await sc.stop(dev)
            sh.frames.clear()
            sc.start(dev, "solid", (0, 255, 0), None, 2, 1)
            await asyncio.sleep(0.4)
            timed = [f for f in sh.frames if f.get("on")]
            await asyncio.sleep(1.5)
            end = (sh.is_on, sh.rgb, sc.active())
        sh.stop()
        return held, timed, end

    held, timed, end = asyncio.run(go())
    assert len(held) >= 3 and all(f["toggle_after"] == lights.HOLD_TIMER_S and f["rgb"] == [0, 0, 255] and f["brightness"] == 40 for f in held)
    assert len(timed) == 1 and timed[0]["toggle_after"] == 1, "a timed colour is ended by the device's own timer"
    assert end == (False, [255, 0, 0], {})


def test_real_alert_cancels_scene_and_lights_with_alert_look(monkeypatch, tmp_path):
    monkeypatch.setattr(lights, "MIN_STEP_S", 0.05)

    async def go():
        feed, sh = MockFeed().start(), MockShelly(2, component="rgb").start()
        app, cfg = _app(tmp_path, feed, sh, alert_color="#ff0000", alert_brightness=100)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.3)
        d = commands.command_dir(cfg.db_path)
        commands.submit(d, "light", "10.0.0.3", device="d", action="effect", effect="disco", speed=3, hold=True)
        await asyncio.sleep(1.5)
        scene_running = bool(app.scenes.active())
        manual_frames = len(sh.frames)
        feed.add_item("Požar")
        await asyncio.sleep(2.5)
        after = (sh.is_on, sh.rgb, sh.brightness, sh.timer_duration, app.scenes.active())
        n1 = len(sh.frames)
        await asyncio.sleep(1.0)
        n2 = len(sh.frames)
        # while the alert may still be active nothing manual may start
        commands.submit(d, "light", "10.0.0.3", device="d", action="effect", effect="police", hold=True)
        commands.submit(d, "light", "10.0.0.3", device="d", action="on", duration_s=30)
        await asyncio.sleep(0.8)
        still = (sh.rgb, sh.timer_duration, app.scenes.active())
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop(); sh.stop()
        return scene_running, manual_frames, after, n1, n2, still, app.alerts.sent

    scene_running, manual_frames, after, n1, n2, still, sent = asyncio.run(go())
    assert scene_running and manual_frames >= 3
    assert after == (True, [255, 0, 0], 100, 300, {}), after  # alert look, the normal 300 s timer, scene gone
    assert n1 == n2, "no scene frame after the alert took over"
    assert still == ([255, 0, 0], 300, {})
    assert sum("refused" in m and "real alert" in m for m in sent) == 2


def test_stop_without_alert_look_puts_original_colour_back_and_alert_uses_it(monkeypatch, tmp_path):
    """No alert_color configured: a real alert after a scene must still show the device's own colour."""
    monkeypatch.setattr(lights, "MIN_STEP_S", 0.05)

    async def go():
        feed, sh = MockFeed().start(), MockShelly(2, component="rgb").start()
        app, cfg = _app(tmp_path, feed, sh)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.3)
        d = commands.command_dir(cfg.db_path)
        commands.submit(d, "light", "10.0.0.3", device="d", action="effect", effect="rainbow", speed=3, hold=True)
        await asyncio.sleep(1.2)
        feed.add_item("Požar")
        await asyncio.sleep(2.5)
        res = (sh.is_on, sh.rgb, sh.brightness, sh.timer_duration)
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop(); sh.stop()
        return res

    assert asyncio.run(go()) == (True, [255, 0, 0], 8, 300)


def test_off_stops_scene_and_test_event_refused_during_scene(monkeypatch, tmp_path):
    monkeypatch.setattr(lights, "MIN_STEP_S", 0.05)

    async def go():
        feed, sh = MockFeed().start(), MockShelly(2, component="rgb").start()
        app, cfg = _app(tmp_path, feed, sh)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.3)
        d = commands.command_dir(cfg.db_path)
        commands.submit(d, "light", "10.0.0.3", device="d", action="effect", effect="police", hold=True)
        await asyncio.sleep(0.8)
        commands.submit(d, "test_event", "10.0.0.3", duration_s=5)
        await asyncio.sleep(0.6)
        commands.submit(d, "light", "10.0.0.3", device="d", action="off")
        await asyncio.sleep(0.8)
        n = len(sh.frames)
        await asyncio.sleep(0.6)
        res = (sh.is_on, sh.rgb, app.scenes.active(), n == len(sh.frames))
        # a colour device that cannot do what is asked, and a relay asked for a colour effect
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop(); sh.stop()
        return res, app.alerts.sent

    (is_on, rgb, active, quiet), sent = asyncio.run(go())
    assert not is_on and rgb == [255, 0, 0] and active == {} and quiet
    assert any("manual light scene is running" in m for m in sent)


def test_relay_cannot_do_colour_effects_but_can_blink(tmp_path):
    async def go():
        feed, sh = MockFeed().start(), MockShelly(2).start()
        app, cfg = _app(tmp_path, feed, sh, component="switch")
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.3)
        d = commands.command_dir(cfg.db_path)
        commands.submit(d, "light", "10.0.0.3", device="d", action="effect", effect="rainbow", hold=True)
        await asyncio.sleep(0.5)
        refused = list(app.alerts.sent)
        commands.submit(d, "light", "10.0.0.3", device="d", action="effect", effect="blink", hold=True, speed=3)
        await asyncio.sleep(1.5)
        frames = list(sh.frames)
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop(); sh.stop()
        return refused, frames

    refused, frames = asyncio.run(go())
    assert any("cannot do 'rainbow'" in m for m in refused)
    assert any(f["on"] for f in frames) and any(not f["on"] for f in frames)
    assert all("rgb" not in f for f in frames)
    assert all(0 < f.get("toggle_after", 1) <= 30 for f in frames if f["on"])


def test_status_json_reports_running_scenes(tmp_path):
    async def go():
        feed, sh = MockFeed().start(), MockShelly(2, component="rgb").start()
        app, cfg = _app(tmp_path, feed, sh)
        t = asyncio.create_task(app.run())
        await asyncio.sleep(0.3)
        commands.submit(commands.command_dir(cfg.db_path), "light", "10.0.0.3", device="d", action="effect",
                        effect="fire", hold=True)
        await asyncio.sleep(6)  # status.json is written every 5 s
        st = json.loads((tmp_path / "status.json").read_text())
        app.stop(); await asyncio.wait_for(t, 15)
        feed.stop(); sh.stop()
        return st

    st = asyncio.run(go())
    assert st["lights"]["d"]["label"] == "Fire" and st["lights"]["d"]["hold"] is True
