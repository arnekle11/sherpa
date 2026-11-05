"""Alerting: state-based alerts (raise/clear with reminders) over Telegram or log.

Sending never blocks polling: messages go on a queue drained by a worker with retries.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Callable
from typing import Protocol

import httpx

log = logging.getLogger("sherpa.alerts")


class Notifier(Protocol):
    async def send(self, text: str) -> bool: ...


class LogNotifier:
    async def send(self, text: str) -> bool:
        log.warning("ALERT (Telegram not configured): %s", text)
        return True


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, api_base: str = "https://api.telegram.org"):
        self._token = token
        self._chat = chat_id
        self._url = f"{api_base.rstrip('/')}/bot{token}/sendMessage"
        # trust_env=False: no proxy surprises; short timeouts, the worker retries.
        self._client = httpx.AsyncClient(timeout=10, trust_env=False)

    def _redact(self, s: str) -> str:
        return s.replace(self._token, "***")

    async def send(self, text: str) -> bool:
        try:
            r = await self._client.post(self._url, json={"chat_id": self._chat, "text": text})
            if r.status_code == 200 and r.json().get("ok"):
                return True
            log.error("Telegram rejected message: HTTP %s %s", r.status_code, self._redact(r.text[:200]))
        except (httpx.HTTPError, ValueError) as e:
            log.error("Telegram send failed: %s", self._redact(f"{type(e).__name__}: {e}"))
        return False

    async def aclose(self) -> None:
        await self._client.aclose()


class AlertManager:
    def __init__(self, notifier: Notifier, repeat_s: float = 1800.0, host: str | None = None,
                 clock=time.monotonic, on_event: Callable[[str, str | None, str], None] | None = None):
        self.notifier = notifier
        self.repeat_s = repeat_s
        self.host = host or socket.gethostname()
        self._clock = clock
        self._on_event = on_event  # (kind, key, text): history for the web UI; must never break alerting
        self._active: dict[str, float] = {}
        self._q: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str] = []  # everything queued, for tests/diagnostics

    def _enqueue(self, text: str, kind: str = "info", key: str | None = None) -> None:
        if self._on_event:
            try:
                self._on_event(kind, key, text)
            except Exception:
                log.exception("alert history hook failed")
        msg = f"[SHERPA {self.host}] {text}"
        self.sent.append(msg)
        self._q.put_nowait(msg)

    def is_active(self, key: str) -> bool:
        return key in self._active

    def active_keys(self) -> list[str]:
        return sorted(self._active)

    def raise_(self, key: str, text: str) -> None:
        """Send once when the condition starts, then a reminder every repeat_s."""
        now = self._clock()
        last = self._active.get(key)
        if last is None or (self.repeat_s and now - last >= self.repeat_s):
            self._active[key] = now
            self._enqueue(("🚨 " if last is None else "🚨 (still) ") + text,
                          "raise" if last is None else "repeat", key)

    def clear(self, key: str, text: str) -> None:
        if self._active.pop(key, None) is not None:
            self._enqueue("✅ " + text, "clear", key)

    def info(self, text: str) -> None:
        self._enqueue("ℹ️ " + text)

    async def run(self) -> None:
        while True:
            msg = await self._q.get()
            try:
                for attempt in range(6):
                    if await self.notifier.send(msg):
                        break
                    await asyncio.sleep(min(2 ** attempt, 30))
                else:
                    log.error("giving up delivering alert: %s", msg)
            finally:
                self._q.task_done()

    async def drain(self, timeout: float = 10.0) -> None:
        try:
            await asyncio.wait_for(self._q.join(), timeout)
        except TimeoutError:
            log.error("alert queue not drained on shutdown (%d pending)", self._q.qsize())
