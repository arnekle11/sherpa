"""Manual light scenes: solid colours and effects (rainbow, disco, police ...), driven by the service.

Safety design, same as an alert's: every ON frame carries a *device-side* timer that is a few seconds long
and is renewed by the next frame, so if SHERPA dies the light goes off by itself within seconds. "Until I turn
it off" is therefore a renewed short timer, never a timer-less ON. A real alert cancels all scenes
(`Scenes.cancel_all`) before it lights the devices.
"""
from __future__ import annotations

import asyncio
import colorsys
import logging
import math
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace

from .devices import ShellyDevice

log = logging.getLogger("sherpa.lights")

MIN_STEP_S = 0.4          # never faster than 2.5 frames/s: stays below the photosensitivity range (3 Hz)
HOLD_TIMER_S = 60         # "until turned off": device timer, renewed every HOLD_RENEW_S
HOLD_RENEW_S = 15
MAX_FAILS = 5             # consecutive failed frames before a scene gives up (and says so)
SPEED_FACTOR = {1: 1.6, 2: 1.0, 3: 0.6}   # step multiplier for speed 1 (slow) .. 3 (fast)
CAP_RANK = {"onoff": 0, "bright": 1, "color": 2}


@dataclass(frozen=True)
class Frame:
    on: bool
    rgb: tuple[int, int, int] | None = None
    brightness: int | None = None
    step: float = 0.6          # seconds until the next frame
    timer: int | None = None   # device-side auto-off for an ON frame; None = derived from step


def _rgb_hex(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _hsv(h: float, s: float = 1.0, v: float = 1.0) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return round(r * 255), round(g * 255), round(b * 255)


def _blink(color, speed, step):
    while True:
        yield Frame(True, color, step=step)
        yield Frame(False, step=step)


def _rainbow(color, speed, step):
    h = 0.0
    while True:
        yield Frame(True, _hsv(h), step=step * 0.6)
        h += 10 / 360


def _disco(color, speed, step):
    rnd = random.Random()
    while True:
        yield Frame(True, _hsv(rnd.random()), brightness=rnd.choice((60, 80, 100)), step=step * rnd.uniform(0.8, 1.4))


def _police(color, speed, step):
    while True:
        yield Frame(True, (255, 0, 0), step=step * 0.7)
        yield Frame(True, (0, 0, 255), step=step * 0.7)


def _fire(color, speed, step):
    rnd = random.Random()
    while True:
        yield Frame(True, (255, rnd.randint(40, 120), 0), brightness=rnd.randint(35, 100), step=step * rnd.uniform(0.5, 1.0))


def _breathe(color, speed, step):
    n = 0
    while True:
        yield Frame(True, color, brightness=round(5 + 95 * (0.5 - 0.5 * math.cos(n * math.pi / 8))), step=step * 0.7)
        n += 1


def _sos(color, speed, step):
    unit = max(step * 0.6, MIN_STEP_S)

    dot, dash = 1, 3
    pattern = [dot] * 3 + [dash] * 3 + [dot] * 3
    while True:
        for i, length in enumerate(pattern):
            yield Frame(True, color, step=unit * length)
            gap = 3 if (i + 1) % 3 == 0 else 1  # letter gap after every third signal
            yield Frame(False, step=unit * (7 if i == len(pattern) - 1 else gap))


def _christmas(color, speed, step):
    while True:
        yield Frame(True, (255, 0, 0), step=step * 1.2)
        yield Frame(True, (0, 200, 0), step=step * 1.2)


def _traffic(color, speed, step):
    while True:
        yield Frame(True, (255, 0, 0), step=step * 3)
        yield Frame(True, (255, 160, 0), step=step * 1.2)
        yield Frame(True, (0, 255, 0), step=step * 3)
        yield Frame(True, (255, 160, 0), step=step * 1.2)


def _ocean(color, speed, step):
    n = 0
    while True:
        yield Frame(True, _hsv((170 + 50 * math.sin(n / 6)) / 360), brightness=round(80 + 20 * math.sin(n / 4)), step=step)
        n += 1


# name -> (label, needed capability, generator, base step seconds). Order = order in the UI.
EFFECTS: dict[str, tuple[str, str, Callable, float]] = {
    "rainbow": ("Rainbow", "color", _rainbow, 0.8),
    "disco": ("Disco", "color", _disco, 0.7),
    "police": ("Police", "color", _police, 0.7),
    "fire": ("Fire", "color", _fire, 0.7),
    "ocean": ("Ocean", "color", _ocean, 0.8),
    "christmas": ("Christmas", "color", _christmas, 1.0),
    "traffic": ("Traffic light", "color", _traffic, 1.0),
    "breathe": ("Breathe", "bright", _breathe, 0.8),
    "blink": ("Blink", "onoff", _blink, 0.8),
    "strobe": ("Strobe (fast blink)", "onoff", _blink, MIN_STEP_S),
    "sos": ("SOS", "onoff", _sos, 0.8),
}


def supports(cap: str, effect: str) -> bool:
    if effect == "solid":
        return True
    return effect in EFFECTS and CAP_RANK[cap] >= CAP_RANK[EFFECTS[effect][1]]


def frames(effect: str, color: tuple[int, int, int], speed: int) -> Iterator[Frame]:
    """Endless frames of one effect. Pure: no I/O, so it is easy to test."""
    _label, _cap, gen, base = EFFECTS[effect]
    step = max(base * SPEED_FACTOR.get(speed, 1.0), MIN_STEP_S)
    # the limit holds for every single frame, whatever an effect multiplies its step by
    return (replace(f, step=max(f.step, MIN_STEP_S)) for f in gen(color, speed, step))


def timer_for(fr: Frame) -> int:
    return fr.timer if fr.timer is not None else max(10, math.ceil(fr.step * 8))


@dataclass
class Scene:
    device: str
    effect: str
    label: str
    hold: bool
    since: float
    ends: float | None   # epoch seconds, None = until turned off
    task: asyncio.Task | None = None


class Scenes:
    """One scene per device. Runs in the service's event loop; all methods are cheap and never raise."""

    def __init__(self, say: Callable[[str], None]):
        self._say = say
        self._scenes: dict[str, Scene] = {}

    def active(self) -> dict[str, dict]:
        return {n: {"effect": s.effect, "label": s.label, "hold": s.hold, "since": s.since, "ends": s.ends}
                for n, s in self._scenes.items()}

    def start(self, dev: ShellyDevice, effect: str, color: tuple[int, int, int], brightness: int | None,
              speed: int, duration_s: int | None) -> None:
        old = self._scenes.pop(dev.name, None)
        if old and old.task:
            old.task.cancel()
        label = "Solid colour" if effect == "solid" else EFFECTS[effect][0]
        now = time.time()
        sc = Scene(dev.name, effect, label, duration_s is None, now, now + duration_s if duration_s else None)
        self._scenes[dev.name] = sc
        sc.task = asyncio.get_running_loop().create_task(
            self._run(dev, sc, color, brightness, speed, duration_s), name=f"scene-{dev.name}")

    async def stop(self, dev: ShellyDevice) -> bool:
        """Stop the scene (if any) and put the device's own look back; True if one was running."""
        sc = self._scenes.pop(dev.name, None)
        if sc is None or sc.task is None:
            return False
        sc.task.cancel()
        await asyncio.gather(sc.task, return_exceptions=True)
        await dev.finish_scene()
        return True

    def cancel_all(self) -> list[str]:
        """A real alert arrived: stop every scene at once, without touching the devices (the alert does)."""
        names = list(self._scenes)
        for sc in self._scenes.values():
            if sc.task:
                sc.task.cancel()
        self._scenes.clear()
        return names

    async def _run(self, dev: ShellyDevice, sc: Scene, color, brightness, speed, duration_s) -> None:
        try:
            await dev.remember_look()
            deadline = time.monotonic() + duration_s if duration_s else None
            if sc.effect == "solid":
                gen: Iterator[Frame] = iter(lambda: Frame(True, color, brightness, step=HOLD_RENEW_S, timer=HOLD_TIMER_S), None)
            else:
                gen = frames(sc.effect, color, speed)
            fails = 0
            for fr in gen:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    break
                timer = timer_for(fr)
                if left is not None and sc.effect == "solid":
                    timer = max(1, math.ceil(left))  # a timed colour needs no renewal: the device ends it itself
                fr = Frame(fr.on, fr.rgb, fr.brightness if fr.brightness is not None else brightness, fr.step, timer)
                ok = await dev.set_frame(fr.on, fr.rgb, fr.brightness, timer)
                fails = 0 if ok else fails + 1
                if fails >= MAX_FAILS:
                    self._say(f"light control: {sc.label} on {dev.name} stopped: the device did not answer "
                              f"{MAX_FAILS} times in a row")
                    break
                step = fr.step if left is None else min(fr.step, left)
                if sc.effect == "solid" and left is not None:
                    step = left
                await asyncio.sleep(step)
        except asyncio.CancelledError:
            raise  # stopped by the user (stop() restores) or by a real alert (which lights the device)
        except Exception as e:
            log.exception("scene crashed")
            self._say(f"light control: {sc.label} on {dev.name} crashed: {e!r}")
        if self._scenes.get(dev.name) is sc:  # ended by itself (time up / failures)
            del self._scenes[dev.name]
            await dev.finish_scene()
