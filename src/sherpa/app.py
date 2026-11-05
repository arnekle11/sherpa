"""The SHERPA service: poll -> dedupe -> dispatch, with visible failures."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .alerts import AlertManager, LogNotifier, Notifier, TelegramNotifier
from .config import AppCfg, deadman_url_from_env, describe_changes, load_config, telegram_from_env
from .deadman import Deadman
from .devices import make_device, make_device_client
from .dispatcher import Dispatcher
from .pollstats import PollStats
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
    def __init__(self, cfg: AppCfg, telegram=_ENV, config_path: str | None = None, deadman=_ENV):
        self.cfg = cfg
        self.config_path = config_path  # set => watched; a valid change restarts the service to apply it
        self.restart_requested = False
        self.store = Store(cfg.db_path)
        self.notifier = build_notifier(cfg, telegram)
        self.alerts = AlertManager(self.notifier, cfg.alerts.repeat_min * 60, on_event=self.store.log_alert)
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
        self.status_file = Path(cfg.db_path).parent / "status.json"  # read by the web UI
        self._status_written = 0.0
        self.device_status: dict[str, dict] = {}
        self.config_poll_s = 5.0
        self.pollstats = PollStats()
        self.last_items: list[dict] = []  # what the feed currently contains, for the web UI
        self._last_poll_done = time.monotonic()
        url = deadman_url_from_env() if deadman is _ENV else deadman
        self.deadman = Deadman(url, cfg.alerts.deadman_interval_s, self._poll_healthy) if url else None
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
                 asyncio.create_task(self._summary_loop(), name="summary"),
                 asyncio.create_task(self._config_watch_loop(), name="config-watch"),
                 asyncio.create_task(self._stats_loop(), name="poll-stats")]
        if self.deadman:
            loops.append(asyncio.create_task(self.deadman.run(), name="deadman"))
        try:
            await self._stop.wait()
        finally:
            for t in loops:
                t.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            if self._tasks:  # let in-flight activations finish: they are the whole point
                await asyncio.wait(self._tasks, timeout=15)
            self._flush_stats()
            await self.alerts.drain()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await self.fetcher.aclose()
            await self._dev_client.aclose()
            if self.deadman:
                await self.deadman.aclose()
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
            self._last_poll_done = time.monotonic()
            self._touch_health()
            next_tick += interval
            now = loop.time()
            if next_tick <= now:  # slow poll: skip ticks, never queue or overlap
                missed = int((now - next_tick) // interval) + 1
                self.polls_skipped += missed
                self.pollstats.skipped(missed)
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
        t0 = time.monotonic()
        try:
            body = await self.fetcher.fetch()
        except FetchError as e:
            self.polls_failed += 1
            self.pollstats.record(False, None)
            log.warning("feed fetch failed: %s", e)
            down_for = time.monotonic() - self._last_http_ok
            if down_for >= fc.down_after_s:
                self.alerts.raise_("feed-down", f"feed UNREACHABLE for {down_for:.0f}s: {e}. "
                                                f"New emergencies will NOT trigger the LED.")
            return
        self._last_http_ok = time.monotonic()
        latency = self._last_http_ok - t0
        self.alerts.clear("feed-down", "feed reachable again")

        try:
            parsed = parse_feed(body)
        except FeedFormatError as e:
            self.polls_failed += 1
            self.pollstats.record(False, latency)
            log.error("feed format error: %s", e)
            self.alerts.raise_("feed-structure", f"feed structure CHANGED / invalid: {e}")
            return
        self.polls_ok += 1
        self.pollstats.record(True, latency)
        self.last_items = [{"title": it.title, "pub": it.pub_date.isoformat(timespec="seconds") if it.pub_date else None}
                           for it in parsed.items[:30]]

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
        await asyncio.sleep(3)  # first look right after start, so a dead device is visible at once
        while True:
            await self.probe_devices()
            await asyncio.sleep(every)

    async def probe_devices(self) -> dict[str, str]:
        """Read-only reachability check (never switches anything)."""
        out = {}
        for d in self.devices:
            key = f"offline:{d.name}"
            checked = datetime.now(UTC).isoformat(timespec="seconds")
            try:
                st = await d.read_state()
                out[d.name] = f"reachable, relay {'ON' if st.on else 'off'}"
                self.device_status[d.name] = {"ip": d.cfg.ip, "ok": True, "detail": out[d.name], "checked": checked}
                self.alerts.clear(key, f"{d.name} is reachable again")
            except Exception as e:
                out[d.name] = f"UNREACHABLE: {e}"
                self.device_status[d.name] = {"ip": d.cfg.ip, "ok": False, "detail": out[d.name], "checked": checked}
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

    def _poll_healthy(self) -> bool:
        """True while the poll loop keeps completing iterations (used by the dead-man's switch)."""
        return time.monotonic() - self._last_poll_done < max(30.0, 5 * self.cfg.feed.poll_interval_s)

    def _flush_stats(self) -> None:
        rows = self.pollstats.drain()
        if not rows:
            return
        try:
            self.store.add_poll_minutes(rows)
        except Exception:
            log.warning("could not write poll statistics; will retry", exc_info=True)
            self.pollstats.requeue(rows)

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self._flush_stats()

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
        self._write_status()

    def _write_status(self, min_interval_s: float = 5.0) -> None:
        """Snapshot for the web UI (a separate process). Best effort: must never disturb polling."""
        now = time.monotonic()
        if now - self._status_written < min_interval_s:
            return
        self._status_written = now
        try:
            snap = {
                "written": time.time(), "version": __version__,
                "uptime_s": now - self._started,
                "poll_interval_s": self.cfg.feed.poll_interval_s,
                "polls_ok": self.polls_ok, "polls_failed": self.polls_failed,
                "polls_skipped": self.polls_skipped, "events_triggered": self.events_triggered,
                "last_event_at": self.last_event_at.isoformat(timespec="seconds") if self.last_event_at else None,
                "feed_ok_age_s": now - self._last_http_ok,
                "active_alerts": self.alerts.active_keys(),
                "telegram_configured": isinstance(self.notifier, TelegramNotifier),
                "deadman": self.deadman.snapshot() if self.deadman else {"configured": False},
                "latency": self.pollstats.latency_summary(),
                "feed_items": self.last_items,
                "devices": {d.name: self.device_status.get(d.name, {"ip": d.cfg.ip, "ok": None,
                            "detail": "not probed yet", "checked": None}) for d in self.devices},
            }
            tmp = self.status_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(snap), encoding="utf-8")
            os.replace(tmp, self.status_file)
        except Exception:
            log.debug("status snapshot failed", exc_info=True)

    # ---------- config changes (made by the web UI) ----------
    async def _config_watch_loop(self) -> None:
        if not self.config_path:
            return
        path = Path(self.config_path)
        try:
            seen = path.stat().st_mtime_ns
        except OSError:
            seen = 0
        while True:
            await asyncio.sleep(self.config_poll_s)
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if mtime == seen:
                continue
            seen = mtime
            try:
                new = load_config(path)
            except Exception as e:
                self.alerts.raise_("config-invalid", f"{path.name} changed but is INVALID, still running the "
                                                     f"old settings: {str(e)[:300]}")
                continue
            self.alerts.clear("config-invalid", f"{path.name} is valid again")
            if new.model_dump() != self.cfg.model_dump():
                changes = "; ".join(describe_changes(self.cfg, new))
                log.warning("config changed on disk: restarting to apply it (%s)", changes)
                self.alerts.info(f"settings changed ({changes}): restarting to apply them"[:600])
                self.restart_requested = True
                self.stop()
                return

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
