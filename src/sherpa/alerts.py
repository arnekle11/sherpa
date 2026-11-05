"""Alerting: state-based alerts (raise/clear with reminders) over Telegram or log.

Sending never blocks polling: messages go on a queue drained by a worker with retries.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

import httpx

log = logging.getLogger("sherpa.alerts")
DEVICE_KEYS = {"activate", "offline", "stuck-on"}  # alert keys "<kind>:<device>", sent before anything else
MAX_QUEUE = 300


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
        self.last_error: str | None = None  # redacted, for the web UI
        self.retry_after: float = 0.0  # set by a 429 (Telegram rate limit): how long to wait before the next try

    def _redact(self, s: str) -> str:
        return s.replace(self._token, "***")

    async def send(self, text: str) -> bool:
        self.retry_after = 0.0
        try:
            r = await self._client.post(self._url, json={"chat_id": self._chat, "text": text[:4000]})
            try:
                body = r.json()
            except ValueError:
                body = None
            if r.status_code == 200 and isinstance(body, dict) and body.get("ok"):
                self.last_error = None
                return True
            if r.status_code == 429 and isinstance(body, dict):
                self.retry_after = float((body.get("parameters") or {}).get("retry_after") or 0)
            self.last_error = self._redact(f"HTTP {r.status_code} {r.text[:160]}")
            log.error("Telegram rejected message: %s", self.last_error)
        except Exception as e:  # whatever comes back, the alert worker must survive it
            self.last_error = self._redact(f"{type(e).__name__}: {e}")[:200]
            log.error("Telegram send failed: %s", self.last_error)
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
        # Priority queue: device problems first, then other alerts, recoveries, info. Bounded: when a flood
        # builds up (e.g. Telegram down for hours), recoveries/info are dropped first, never raised alerts.
        self._q: asyncio.PriorityQueue[tuple[int, int, str]] = asyncio.PriorityQueue()
        self._seq = 0
        self.dropped = 0
        self.sent: deque[str] = deque(maxlen=2000)  # recently queued, for tests/diagnostics
        # delivery health, for the web UI: None = nothing sent yet
        self.delivery_ok: bool | None = None
        self.delivery_error: str | None = None
        self.undelivered = 0

    def _history(self, kind: str, key: str | None, text: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind, key, text)
            except Exception:
                log.exception("alert history hook failed")

    def _enqueue(self, text: str, kind: str = "info", key: str | None = None) -> None:
        self._history(kind, key, text)
        msg = f"[SHERPA {self.host}] {text}"
        self.sent.append(msg)
        device = bool(key) and key.split(":", 1)[0] in DEVICE_KEYS
        prio = (0 if device else 1) if kind in ("raise", "repeat") else (2 if kind == "clear" else 3)
        if prio >= 2 and self._q.qsize() >= MAX_QUEUE:
            self.dropped += 1
            log.error("alert queue full (%d): dropping %s message: %s", self._q.qsize(), kind, msg)
            return
        self._seq += 1
        self._q.put_nowait((prio, self._seq, msg))

    def is_active(self, key: str) -> bool:
        return key in self._active

    def active_keys(self) -> list[str]:
        return sorted(self._active)

    def delivery_snapshot(self) -> dict:
        return {"ok": self.delivery_ok, "error": self.delivery_error, "undelivered": self.undelivered,
                "queued": self._q.qsize(), "dropped": self.dropped}

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
            _prio, _seq, msg = await self._q.get()
            try:
                for attempt in range(6):
                    try:
                        ok = await self.notifier.send(msg)
                    except Exception as e:  # a notifier bug must not kill the worker (later alerts would never go)
                        log.exception("notifier crashed")
                        ok, self.notifier.last_error = False, f"{type(e).__name__}"  # type: ignore[attr-defined]
                    if ok:
                        self.delivery_ok, self.delivery_error = True, None
                        break
                    self.delivery_ok = False
                    self.delivery_error = getattr(self.notifier, "last_error", None) or "send failed"
                    wait = max(min(2 ** attempt, 30), getattr(self.notifier, "retry_after", 0) or 0)
                    await asyncio.sleep(min(wait, 120))
                else:
                    self.undelivered += 1
                    log.error("giving up delivering alert: %s", msg)
                    self._history("undelivered", None, f"NOT delivered ({self.delivery_error}): {msg}")
            finally:
                self._q.task_done()

    async def drain(self, timeout: float = 10.0) -> None:
        try:
            await asyncio.wait_for(self._q.join(), timeout)
        except TimeoutError:
            log.error("alert queue not drained on shutdown (%d pending)", self._q.qsize())
