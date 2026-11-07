"""Shelly adapters, one per generation, behind a common interface.

Safety design: the *device* owns the auto-off timer (Gen1 `timer=`, Gen2 `toggle_after=`),
so the LED turns itself off even if this app dies. Re-sending ON restarts the timer,
which is how a new event extends an active one. After every ON we read the state back
and only report success if the relay is on AND a timer is armed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from .config import DeviceCfg

log = logging.getLogger("sherpa.devices")


class DeviceError(Exception):
    pass


@dataclass
class DeviceState:
    on: bool
    timer_active: bool
    remaining_s: float | None = None


@dataclass
class DeviceResult:
    name: str
    ok: bool
    attempts: int
    detail: str
    elapsed_s: float


class ShellyDevice(ABC):
    def __init__(self, cfg: DeviceCfg, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client
        self.base = f"http://{cfg.ip}"

    @property
    def name(self) -> str:
        return self.cfg.name

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = await asyncio.wait_for(self.client.get(self.base + path, params=params), self.cfg.timeout_s)
        except TimeoutError as e:
            raise DeviceError(f"timeout after {self.cfg.timeout_s}s") from e
        except httpx.HTTPError as e:
            raise DeviceError(f"{type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise DeviceError(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise DeviceError("invalid JSON in response") from e
        if not isinstance(data, dict):
            raise DeviceError("unexpected JSON shape")
        return data

    @abstractmethod
    async def _send_on(self, duration_s: int) -> None: ...

    @abstractmethod
    async def _send_off(self) -> None: ...

    @abstractmethod
    async def read_state(self) -> DeviceState: ...

    async def deactivate(self) -> DeviceResult:
        """Switch off (manual control only; an alert never calls this); retry; verify by read-back."""
        t0 = time.monotonic()
        last_err = "unknown"
        for attempt in range(1, self.cfg.attempts + 1):
            try:
                await self._send_off()
                if not (await self.read_state()).on:
                    return DeviceResult(self.name, True, attempt, "off", time.monotonic() - t0)
                last_err = "read-back mismatch (still on)"
            except DeviceError as e:
                last_err = str(e)
            if attempt < self.cfg.attempts:
                await asyncio.sleep(min(self.cfg.retry_delay_s * (2 ** (attempt - 1)), 5.0))
        return DeviceResult(self.name, False, self.cfg.attempts, last_err, time.monotonic() - t0)

    async def activate(self, duration_s: int | None = None) -> DeviceResult:
        """Switch on with device-side timer; retry; verify by read-back."""
        duration = duration_s or self.cfg.on_duration_s
        t0 = time.monotonic()
        last_err = "unknown"
        for attempt in range(1, self.cfg.attempts + 1):
            try:
                await self._send_on(duration)
                st = await self.read_state()
                if st.on and st.timer_active:
                    return DeviceResult(self.name, True, attempt,
                                        f"on, timer remaining={st.remaining_s}", time.monotonic() - t0)
                last_err = f"read-back mismatch (on={st.on}, timer_active={st.timer_active})"
            except DeviceError as e:
                last_err = str(e)
            log.warning("device %s attempt %d/%d failed: %s", self.name, attempt, self.cfg.attempts, last_err)
            if attempt < self.cfg.attempts:
                # exponential, but never more than 5 s per step: a failure must be reported within seconds
                await asyncio.sleep(min(self.cfg.retry_delay_s * (2 ** (attempt - 1)), 5.0))
        return DeviceResult(self.name, False, self.cfg.attempts, last_err, time.monotonic() - t0)


class ShellyGen1(ShellyDevice):
    async def _send_on(self, duration_s: int) -> None:
        await self._get(f"/relay/{self.cfg.channel}", {"turn": "on", "timer": duration_s})

    async def _send_off(self) -> None:
        await self._get(f"/relay/{self.cfg.channel}", {"turn": "off"})

    async def read_state(self) -> DeviceState:
        d = await self._get(f"/relay/{self.cfg.channel}")
        if "ison" not in d:
            raise DeviceError("no 'ison' in response (is this a Gen1 relay?)")
        return DeviceState(bool(d["ison"]), bool(d.get("has_timer")), d.get("timer_remaining"))


class ShellyGen2(ShellyDevice):
    async def _send_on(self, duration_s: int) -> None:
        await self._get(f"/rpc/{self._rpc}.Set",
                        {"id": self.cfg.channel, "on": "true", "toggle_after": duration_s})

    async def _send_off(self) -> None:
        await self._get(f"/rpc/{self._rpc}.Set", {"id": self.cfg.channel, "on": "false"})

    @property
    def _rpc(self) -> str:
        return {"switch": "Switch", "light": "Light", "rgb": "RGB", "rgbw": "RGBW"}[self.cfg.component]

    async def read_state(self) -> DeviceState:
        d = await self._get(f"/rpc/{self._rpc}.GetStatus", {"id": self.cfg.channel})
        if "output" not in d:
            raise DeviceError(f"no 'output' in response (does this Shelly have a {self.cfg.component} component?)")
        # timer_duration is only present while a timer is armed.
        dur = d.get("timer_duration")
        return DeviceState(bool(d["output"]), bool(dur), dur)


def make_device(cfg: DeviceCfg, client: httpx.AsyncClient) -> ShellyDevice:
    return {1: ShellyGen1, 2: ShellyGen2}[cfg.generation](cfg, client)


def make_device_client() -> httpx.AsyncClient:
    # LAN only: never use proxy env vars for Shellies. No client timeout: each request is bounded by the
    # device's own timeout_s (asyncio.wait_for in _get); httpx's 5 s default would silently cap it.
    return httpx.AsyncClient(trust_env=False, timeout=None, limits=httpx.Limits(keepalive_expiry=30))
