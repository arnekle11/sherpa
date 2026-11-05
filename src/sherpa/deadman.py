"""Dead-man's switch: ping an external URL while the poll loop is healthy.

The Telegram alerts come from this same service, so if the VM or the service dies they go silent.
An external watcher (e.g. healthchecks.io, Uptime Kuma push monitor) that expects this ping and
alerts when it STOPS closes that gap. Optional: only active when SHERPA_DEADMAN_URL is set.
The URL is a secret: it is never logged and never put in an exception message.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import httpx

log = logging.getLogger("sherpa.deadman")


class Deadman:
    def __init__(self, url: str, interval_s: float, healthy: Callable[[], bool],
                 client: httpx.AsyncClient | None = None):
        self._url = url
        self.interval_s = interval_s
        self._healthy = healthy
        self._client = client or httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False)
        self._own_client = client is None
        self.last_ok: float | None = None  # monotonic
        self.last_error: str | None = None
        self.pings_ok = self.pings_failed = 0
        self._failing = False

    async def ping_once(self) -> bool:
        """One ping, only if the poll loop is alive. Never raises."""
        if not self._healthy():
            self.last_error = "poll loop not healthy: ping withheld on purpose"
            return False
        try:
            r = await self._client.get(self._url)
            if 200 <= r.status_code < 300:
                if self._failing:
                    log.info("dead-man's switch ping working again")
                    self._failing = False
                self.last_ok = time.monotonic()
                self.last_error = None
                self.pings_ok += 1
                return True
            err = f"HTTP {r.status_code}"
        except Exception as e:  # type name only: httpx messages can contain the URL
            err = type(e).__name__
        if not self._failing:
            log.warning("dead-man's switch ping failed: %s", err)
            self._failing = True
        self.last_error = err
        self.pings_failed += 1
        return False

    async def run(self) -> None:
        while True:
            await self.ping_once()
            await asyncio.sleep(self.interval_s)

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    def snapshot(self) -> dict:
        return {"configured": True, "interval_s": self.interval_s,
                "last_ok_age_s": None if self.last_ok is None else time.monotonic() - self.last_ok,
                "last_error": self.last_error, "ok": self.pings_ok, "failed": self.pings_failed}
