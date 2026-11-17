"""Extra feeds that only notify a phone through Pushover (no relay, no light).

Same polling discipline as the main feed (keep-alive, hard timeout, strict parse, dedupe by guid, ignore stale
items on the first poll), but a separate failure domain: an error here is reported through the normal alerts and
can never touch the main poll loop or the devices. Items are remembered in their own table, so they do not
count as "a real event" for the light-safety windows.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import httpx

from .config import FeedCfg, PushFeedCfg
from .feed import FeedFetcher, FeedFormatError, FeedItem, FetchError, parse_feed

log = logging.getLogger("sherpa.push")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
FAIL_AFTER = 3          # consecutive bad polls/sends before an alert (one odd response is noise)
GIVE_UP_AFTER = timedelta(hours=2)   # an item that still cannot be delivered after this long is dropped, loudly


def pushover_from_env() -> tuple[str, str] | None:
    token = os.environ.get("SHERPA_PUSHOVER_TOKEN", "").strip()
    user = os.environ.get("SHERPA_PUSHOVER_USER", "").strip()
    return (token, user) if token and user else None


class PushoverNotifier:
    def __init__(self, token: str, user: str, url: str = PUSHOVER_URL):
        self._token, self._user, self._url = token, user, url
        self._client = httpx.AsyncClient(timeout=10, trust_env=False)
        self.last_error: str | None = None

    def _redact(self, s: str) -> str:
        return s.replace(self._token, "***").replace(self._user, "***")

    async def send(self, title: str, message: str, url: str = "", url_title: str = "", priority: int = 1,
                   sound: str = "", timestamp: int | None = None) -> bool:
        data = {"token": self._token, "user": self._user, "title": title[:250], "message": (message or title)[:1024],
                "priority": str(priority)}
        if url:
            data["url"], data["url_title"] = url[:512], url_title[:100]
        if sound:
            data["sound"] = sound
        if timestamp:
            data["timestamp"] = str(timestamp)
        try:
            r = await self._client.post(self._url, data=data)
            try:
                body = r.json()
            except ValueError:
                body = None
            if r.status_code == 200 and isinstance(body, dict) and body.get("status") == 1:
                self.last_error = None
                return True
            self.last_error = self._redact(f"HTTP {r.status_code} {r.text[:160]}")
        except Exception as e:
            self.last_error = self._redact(f"{type(e).__name__}: {e}")[:200]
        log.error("Pushover send failed: %s", self.last_error)
        return False

    async def aclose(self) -> None:
        await self._client.aclose()


class PushFeed:
    def __init__(self, cfg: PushFeedCfg, store, alerts, notifier: PushoverNotifier | None):
        self.cfg, self.store, self.alerts, self.notifier = cfg, store, alerts, notifier
        fc = FeedCfg(url=cfg.url, poll_interval_s=cfg.poll_interval_s, timeout_s=cfg.timeout_s,
                     max_age_min=cfg.max_age_min, expected_link_contains=cfg.expected_link_contains,
                     user_agent=cfg.user_agent)
        self.fetcher = FeedFetcher(fc)
        self._first = True
        self._bad_polls = 0
        self._bad_sends = 0
        self.ok: bool | None = None
        self.last_ok_at: float | None = None
        self.notified = 0
        self.last_error = ""

    @property
    def _key(self) -> str:
        return f"pushfeed:{self.cfg.name}"

    def snapshot(self) -> dict:
        return {"url": self.cfg.url, "ok": self.ok, "last_ok_at": self.last_ok_at, "notified": self.notified,
                "error": self.last_error, "pushover": self.notifier is not None}

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # nothing may end this loop
                log.exception("push feed %s: poll crashed", self.cfg.name)
            await asyncio.sleep(self.cfg.poll_interval_s)

    async def poll_once(self) -> None:
        try:
            parsed = parse_feed(await self.fetcher.fetch())
        except (FetchError, FeedFormatError) as e:
            self.ok, self.last_error = False, f"{type(e).__name__}: {e}"
            self._bad_polls += 1
            if self._bad_polls >= FAIL_AFTER:
                self.alerts.raise_(self._key, f"push feed '{self.cfg.name}' cannot be read: {self.last_error}. "
                                              "No phone notifications for it until this is fixed.")
            return
        self.ok, self.last_error, self.last_ok_at = True, "", time.time()
        self._bad_polls = 0
        self.alerts.clear(self._key, f"push feed '{self.cfg.name}' is readable again")
        exp = self.cfg.expected_link_contains
        if exp and (not parsed.self_link or exp not in parsed.self_link):
            self.alerts.raise_(f"{self._key}:link", f"push feed '{self.cfg.name}': self-link {parsed.self_link!r} no "
                                                    f"longer contains {exp!r}; the server-side filter may have changed")
        else:
            self.alerts.clear(f"{self._key}:link", f"push feed '{self.cfg.name}' self-link is as expected again")
        await self._handle(parsed.items)

    async def _handle(self, items: list[FeedItem]) -> None:
        name, now = self.cfg.name, datetime.now(UTC)
        max_age = timedelta(minutes=self.cfg.max_age_min)
        unseen = sorted((it for it in items if not self.store.push_seen(name, it.guid)),
                        key=lambda it: it.pub_date or now)
        sent_any = failed_any = False
        for it in unseen:
            age = (now - it.pub_date) if it.pub_date else None
            if self._first and (age is None or age > max_age):
                self.store.push_record(name, it, now, "ignored_old")
                continue
            if self.notifier is None:
                self.alerts.raise_("pushover", "a push feed has news but Pushover is not configured "
                                               "(SHERPA_PUSHOVER_TOKEN / SHERPA_PUSHOVER_USER in .env)")
                continue  # not recorded: it is delivered once Pushover works, if still young
            if age is not None and age > GIVE_UP_AFTER:
                self.store.push_record(name, it, now, "dropped_old")
                self.alerts.info(f"push feed '{name}': dropped '{it.title}' after failing to deliver it for "
                                 f"{int(age.total_seconds() // 60)} min")
                continue
            local = it.pub_date.astimezone().strftime("%d.%m.%Y %H:%M") if it.pub_date else ""
            body = " | ".join(x for x in (it.description, local) if x) or it.title
            ok = await self.notifier.send(it.title or name, body, it.link, "Zemljevid", self.cfg.priority,
                                          self.cfg.sound, int(it.pub_date.timestamp()) if it.pub_date else None)
            if ok:
                self.store.push_record(name, it, now, "notified")
                self.notified += 1
                sent_any = True
                log.warning("PUSH_SENT feed=%s guid=%s title=%r", name, it.guid, it.title)
            else:
                failed_any = True
        self._first = False
        if self.notifier is not None:
            if failed_any:
                self._bad_sends += 1
                if self._bad_sends >= FAIL_AFTER:
                    self.alerts.raise_("pushover", f"Pushover is rejecting notifications: {self.notifier.last_error}")
            elif sent_any or not unseen:
                self._bad_sends = 0
                self.alerts.clear("pushover", "Pushover delivers again")
