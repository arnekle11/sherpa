"""Fan out to all Shellies in parallel; raise/clear per-device alerts."""
from __future__ import annotations

import asyncio
import logging

from .alerts import AlertManager
from .devices import DeviceResult, ShellyDevice

log = logging.getLogger("sherpa.dispatch")


class Dispatcher:
    def __init__(self, devices: list[ShellyDevice], alerts: AlertManager):
        self.devices = devices
        self.alerts = alerts

    async def dispatch(self, reason: str, duration_s: int | None = None) -> list[DeviceResult]:
        results = await asyncio.gather(*(self._one(d, reason, duration_s) for d in self.devices))
        return list(results)

    async def _one(self, dev: ShellyDevice, reason: str, duration_s: int | None) -> DeviceResult:
        key = f"activate:{dev.name}"
        try:
            res = await dev.activate(duration_s)
        except Exception as e:  # an adapter bug must not stop the other devices
            log.exception("adapter crashed for %s", dev.name)
            res = DeviceResult(dev.name, False, 0, f"internal error: {e!r}", 0.0)
        if res.ok:
            log.info("DEVICE_ON %s ok in %.2fs (attempt %d) reason=%s", res.name, res.elapsed_s, res.attempts, reason)
            self.alerts.clear(key, f"{res.name} confirmed ON again after earlier failure")
        else:
            log.error("DEVICE_FAIL %s after %d attempts: %s", res.name, res.attempts, res.detail)
            self.alerts.raise_(key, f"{res.name} ({dev.cfg.ip}) FAILED to switch on after "
                                    f"{res.attempts} attempts: {res.detail}. Trigger: {reason}")
        return res
