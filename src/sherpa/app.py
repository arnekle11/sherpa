"""The SHERPA service: poll -> dedupe -> dispatch, with visible failures."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .alerts import AlertManager, LogNotifier, Notifier, TelegramNotifier
from .config import AppCfg, telegram_from_env
from .devices import make_device, make_device_client
from .dispatcher import Dispatcher
from .feed import FeedFetcher, FeedFormatError, FeedItem, FetchError, parse_feed
from .store import Store

log = logging.getLogger("sherpa.app")
_ENV = object()


def build_notifier(cfg: AppCfg, telegram=_ENV) -> Notifier:
    creds = telegram_from_env() if telegram is _ENV else telegram
    if creds:
        return TelegramNotifier(creds[0], creds[1], cfg.alerts.telegram_api_base)
    log.warning("Telegram NOT configured (SHERPA_TELEGRAM_TOKEN/CHAT_ID): alerts go to the log only")
    return LogNotifier()


class App:
    def __init__(self, cfg: AppCfg, telegram=_ENV):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.notifier = build_notifier(cfg, telegram)
        self.alerts = AlertManager(self.notifier, cfg.alerts.repeat_min * 60)
        self.fetcher = FeedFetcher(cfg.feed)
        self._dev_client = make_device_client()
        self.devices = [make_device(d, self._dev_client) for d in cfg.devices]
        self.dispatcher = Dispatcher(self.devices, self.alerts)
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._started = time.monotonic()
        self._last_http_ok = time.monotonic()
        self._baseline_done = False  # first successful poll after start
        self._logged_link = False
        self.health_file = Path(cfg.db_path).parent / "health"
        # counters (also used by selftest)
        self.polls_ok = self.polls_failed = self.polls_skipped = 0
        self.events_triggered = 0
        self.last_event_at: datetime | None = None

    # ---------- lifecycle ----------
    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        worker = asyncio.create_task(self.alerts.run(), name="alerts")
        self.store.prune()
        await self._recover_pending()
        self.alerts.info(
            f"started v{__version__}; polling {self.cfg.feed.url} every {self.cfg.feed.poll_interval_s}s; "
            f"devices: {', '.join(f'{d.name}@{d.ip}' for d in self.cfg.devices)}")
        loops = [asyncio.create_task(self._poll_loop(), name="poll"),
                 asyncio.create_task(self._device_probe_loop(), name="probe"),
                 asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
                 asyncio.create_task(self._summary_loop(), name="summary")]
        try:
            await self._stop.wait()
        finally:
            for t in loops:
                t.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            if self._tasks:  # let in-flight activations finish: they are the whole point
                await asyncio.wait(self._tasks, timeout=15)
            await self.alerts.drain()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await self.fetcher.aclose()
            await self._dev_client.aclose()
            if hasattr(self.notifier, "aclose"):
                await self.notifier.aclose()
            self.store.close()

    def _spawn(self, coro, name: str) -> None:
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _recover_pending(self) -> None:
        """A crash between 'recorded' and 'dispatched' must not lose an alert."""
        max_age = timedelta(minutes=self.cfg.feed.max_age_min)
        now = datetime.now(UTC)
        redo = []
        for row in self.store.pending():
            if now - datetime.fromisoformat(row["first_seen"]) <= max_age:
                redo.append(row["guid"])
            else:
                self.store.set_state(row["guid"], "abandoned", "pending too long at restart")
        if redo:
            log.warning("recovering %d event(s) recorded but not dispatched before restart", len(redo))
            self._spawn(self._dispatch(redo, "recovered after restart"), "recover")

    # ---------- polling ----------
    async def _poll_loop(self) -> None:
        loop = asyncio.get_running_loop()
        interval = self.cfg.feed.poll_interval_s
        next_tick = loop.time()
        while not self._stop.is_set():
            await self.poll_once()
            self._touch_health()
            next_tick += interval
            now = loop.time()
            if next_tick <= now:  # slow poll: skip ticks, never queue or overlap
                missed = int((now - next_tick) // interval) + 1
                self.polls_skipped += missed
                next_tick += missed * interval
            try:
                await asyncio.wait_for(self._stop.wait(), max(0.0, next_tick - loop.time()))
            except TimeoutError:
                pass

    async def poll_once(self) -> None:
        try:
            await self._poll()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # nothing may kill the poll loop
            log.exception("unexpected error in poll")
            self.alerts.raise_("internal", f"unexpected internal error in poll loop: {e!r}")

    async def _poll(self) -> None:
        fc = self.cfg.feed
        try:
            body = await self.fetcher.fetch()
        except FetchError as e:
            self.polls_failed += 1
            log.warning("feed fetch failed: %s", e)
            down_for = time.monotonic() - self._last_http_ok
            if down_for >= fc.down_after_s:
                self.alerts.raise_("feed-down", f"feed UNREACHABLE for {down_for:.0f}s: {e}. "
                                                f"New emergencies will NOT trigger the LED.")
            return
        self._last_http_ok = time.monotonic()
        self.alerts.clear("feed-down", "feed reachable again")

        try:
            parsed = parse_feed(body)
        except FeedFormatError as e:
            self.polls_failed += 1
            log.error("feed format error: %s", e)
            self.alerts.raise_("feed-structure", f"feed structure CHANGED / invalid: {e}")
            return
        self.polls_ok += 1

        if not self._logged_link:
            self._logged_link = True
            log.info("feed self-link: %s (%d items, %d bytes)", parsed.self_link, len(parsed.items), len(body))
        exp = fc.expected_link_contains
        if exp and (not parsed.self_link or exp not in parsed.self_link):
            self.alerts.raise_("feed-link", f"feed self-link changed: {parsed.self_link!r} no longer "
                                            f"contains {exp!r} - the server-side filter may have changed")
        else:
            self.alerts.clear("feed-link", f"feed self-link is back to expected ({parsed.self_link})")
        if parsed.problems:
            self.alerts.raise_("feed-structure", "feed items look unusual: " + "; ".join(parsed.problems[:5]))
        else:
            self.alerts.clear("feed-structure", "feed structure is valid again")

        self._handle_items(parsed.items)

    def _handle_items(self, items: list[FeedItem]) -> None:
        now = datetime.now(UTC)
        max_age = timedelta(minutes=self.cfg.feed.max_age_min)
        first = not self._baseline_done
        new: list[FeedItem] = []
        for it in items:  # feed order is NOT reliable: examine every item every time
            if self.store.seen(it.guid):
                continue  # known guid: later description edits are ignored by design
            age = (now - it.pub_date) if it.pub_date else None
            lag = age.total_seconds() if age is not None else None
            if first and (age is None or age > max_age):
                self.store.record(it.guid, it.title, it.link, it.pub_date, now, "ignored_old",
                                  "older than max_age at startup")
                log.info("EVENT_IGNORED_OLD guid=%s pub=%s title=%r", it.guid, it.pub_date, it.title)
                continue
            if self.store.record(it.guid, it.title, it.link, it.pub_date, now, "pending"):
                log.warning("EVENT_NEW guid=%s pub=%s first_seen=%s lag_s=%s title=%r",
                            it.guid, it.pub_date, now.isoformat(),
                            f"{lag:.1f}" if lag is not None else "n/a", it.title)
                new.append(it)
        self._baseline_done = True
        if new:
            self.last_event_at = now
            self.events_triggered += len(new)
            self._spawn(self._dispatch([i.guid for i in new], "; ".join(i.title for i in new)[:200]), "dispatch")

    async def _dispatch(self, guids: list[str], reason: str) -> None:
        try:
            results = await self.dispatcher.dispatch(reason)
        except Exception as e:
            log.exception("dispatch crashed")
            for g in guids:
                self.store.set_state(g, "failed", f"dispatch crashed: {e!r}")
            self.alerts.raise_("dispatch", f"dispatch crashed: {e!r}")
            return
        ok = sum(r.ok for r in results)
        state = "triggered" if ok == len(results) else ("partial" if ok else "failed")
        detail = "; ".join(f"{r.name}={'ok' if r.ok else r.detail}" for r in results)
        for g in guids:
            self.store.set_state(g, state, detail)

    # ---------- background checks ----------
    async def _device_probe_loop(self) -> None:
        every = self.cfg.alerts.device_check_interval_s
        if not every:
            return
        while True:
            await asyncio.sleep(every)
            await self.probe_devices()

    async def probe_devices(self) -> dict[str, str]:
        """Read-only reachability check (never switches anything)."""
        out = {}
        for d in self.devices:
            key = f"offline:{d.name}"
            try:
                st = await d.read_state()
                out[d.name] = f"reachable, relay {'ON' if st.on else 'off'}"
                self.alerts.clear(key, f"{d.name} is reachable again")
            except Exception as e:
                out[d.name] = f"UNREACHABLE: {e}"
                self.alerts.raise_(key, f"{d.name} ({d.cfg.ip}) is not reachable: {e}. "
                                        f"It would NOT light up on an emergency.")
        return out

    async def _heartbeat_loop(self) -> None:
        every = self.cfg.alerts.heartbeat_min * 60
        if not every:
            return
        while True:
            await asyncio.sleep(every)
            devs = await self.probe_devices()
            up = (time.monotonic() - self._started) / 3600
            last = self.last_event_at.isoformat(timespec="seconds") if self.last_event_at else "none since start"
            self.alerts.info(
                f"heartbeat: up {up:.1f}h, polls ok={self.polls_ok} failed={self.polls_failed} "
                f"skipped={self.polls_skipped}, events={self.events_triggered}, last event: {last}; "
                + ", ".join(f"{k}: {v}" for k, v in devs.items()))

    async def _summary_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            log.info("summary: polls ok=%d failed=%d skipped=%d events=%d",
                     self.polls_ok, self.polls_failed, self.polls_skipped, self.events_triggered)

    def _touch_health(self) -> None:
        try:
            self.health_file.parent.mkdir(parents=True, exist_ok=True)
            self.health_file.touch()
        except OSError:
            pass

    # ---------- test event ----------
    async def send_test_event(self, duration_s: int | None = None) -> list:
        guid = f"test:{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
        now = datetime.now(UTC)
        self.store.record(guid, "SHERPA test event", "", now, now, "pending", is_test=True)
        results = await self.dispatcher.dispatch("TEST EVENT", duration_s)
        ok = all(r.ok for r in results)
        self.store.set_state(guid, "triggered" if ok else "failed",
                             "; ".join(f"{r.name}={'ok' if r.ok else r.detail}" for r in results))
        self.alerts.info(f"test event dispatched: " + ", ".join(
            f"{r.name}={'OK' if r.ok else 'FAIL'}" for r in results))
        return results
