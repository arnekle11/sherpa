"""The SHERPA service: poll -> dedupe -> dispatch, with visible failures."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import BUILD, commands
from .alerts import AlertManager, LogNotifier, Notifier, TelegramNotifier
from .config import AppCfg, describe_changes, load_config, telegram_from_env
from .deadman import Deadman
from .devices import make_device, make_device_client
from .dispatcher import Dispatcher
from .pollstats import PollStats
from .feed import FeedFetcher, FeedFormatError, FeedItem, FetchError, parse_feed
from .store import Store

log = logging.getLogger("sherpa.app")
_ENV = object()


def _file_hash(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def dur_text(d: timedelta) -> str:
    s = int(d.total_seconds())
    return f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{s // 60} min"


class TestRefused(Exception):
    """A test event would interfere with a (possibly) active real alert, or another test."""
    __test__ = False  # not a pytest test class


def build_notifier(cfg: AppCfg, telegram=_ENV) -> Notifier:
    creds = telegram_from_env() if telegram is _ENV else telegram
    if creds:
        return TelegramNotifier(creds[0], creds[1], cfg.alerts.telegram_api_base)
    log.warning("Telegram NOT configured (SHERPA_TELEGRAM_TOKEN/CHAT_ID): alerts go to the log only")
    return LogNotifier()


class App:
    def __init__(self, cfg: AppCfg, telegram=_ENV, config_path: str | None = None, deadman: str | None = None,
                 config_error: str | None = None):
        self.cfg = cfg
        self.config_error = config_error  # set: config.yaml was invalid at start, cfg is the last-good copy
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
        self.device_status: dict[str, dict] = {}
        self.config_poll_s = 5.0
        self.pollstats = PollStats()
        self.last_items: list[dict] = []  # what the feed currently contains, for the web UI
        self.last_items_at: float | None = None  # wall-clock time of the last successful parse
        self._last_poll_ok = time.monotonic()  # last poll iteration that did not crash
        self._last_poll_done = time.monotonic()  # last poll iteration, crashed or not
        self.status_every_s = 5.0
        self.clock_skew_s: float | None = None  # local clock minus the feed server's Date header
        self._streak: dict[str, int] = {}  # per-poll alert hysteresis: +n consecutive bad, -n consecutive good
        self.flap_polls = 3
        self._last_parse_ok_at: datetime | None = None  # wall clock of the previous successful parse
        self._last_tg_test = float("-inf")
        self.config_hash = _file_hash(config_path) if config_path else None
        self.poll_health_grace_s = max(30.0, 5 * cfg.feed.poll_interval_s)
        self.command_poll_s = 2.0
        self._real_dispatches = 0  # bumped whenever a real activation starts
        self._last_real_dispatch = float("-inf")
        self._test_task: asyncio.Task | None = None
        # Only `sherpa run` passes the URL: selftest/test-event must never tell the monitor we are alive.
        self.deadman = Deadman(deadman, cfg.alerts.deadman_interval_s, self._poll_healthy) if deadman else None
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
            f"started v{BUILD}; polling {self.cfg.feed.url} every {self.cfg.feed.poll_interval_s}s; "
            f"devices: {', '.join(f'{d.name}@{d.ip}' for d in self.cfg.devices)}")
        loops = [asyncio.create_task(self._poll_loop(), name="poll"),
                 asyncio.create_task(self._status_loop(), name="status"),
                 asyncio.create_task(self._housekeeping_loop(), name="housekeeping"),
                 asyncio.create_task(self._device_probe_loop(), name="probe"),
                 asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
                 asyncio.create_task(self._summary_loop(), name="summary"),
                 asyncio.create_task(self._config_watch_loop(), name="config-watch"),
                 asyncio.create_task(self._stats_loop(), name="poll-stats"),
                 asyncio.create_task(self._command_loop(), name="commands")]
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
            self._write_status()
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

    def _spawn(self, coro, name: str) -> asyncio.Task:
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _recover_pending(self) -> None:
        """A crash between 'recorded' and 'dispatched' must not lose an alert."""
        max_age = timedelta(minutes=self.cfg.feed.max_age_min)
        now = datetime.now(UTC)
        redo = []
        for row in self.store.pending():
            if row["is_test"]:  # a test interrupted by a restart must never come back as a real 300 s activation
                self.store.set_state(row["guid"], "abandoned", "test event interrupted by a restart")
                continue
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
            self._last_poll_ok = time.monotonic()
            self._touch_health()  # only polls that did not crash count as "alive" for the Docker healthcheck
            self.alerts.clear("internal", "the poll loop works normally again")
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
        self._check_clock()
        self.alerts.clear("feed-down", "feed reachable again")

        try:
            parsed = parse_feed(body)
        except FeedFormatError as e:
            self.polls_failed += 1
            self.pollstats.record(False, latency)
            log.error("feed format error: %s", e)
            self._flap_raise("feed-structure", f"feed structure CHANGED / invalid: {e}")
            return
        self.polls_ok += 1
        self.pollstats.record(True, latency)
        self.last_items_at = time.time()
        self.last_items = [{"title": it.title, "pub": it.pub_date.isoformat(timespec="seconds") if it.pub_date else None}
                           for it in parsed.items[:30]]

        if not self._logged_link:
            self._logged_link = True
            log.info("feed self-link: %s (%d items, %d bytes)", parsed.self_link, len(parsed.items), len(body))
        exp = fc.expected_link_contains
        if exp and (not parsed.self_link or exp not in parsed.self_link):
            self._flap_raise("feed-link", f"feed self-link changed: {parsed.self_link!r} no longer "
                                          f"contains {exp!r} - the server-side filter may have changed")
        else:
            self._flap_clear("feed-link", f"feed self-link is back to expected ({parsed.self_link})")
        if parsed.problems:
            self._flap_raise("feed-structure", "feed items look unusual (events still trigger): "
                                               + "; ".join(parsed.problems[:5]))
        else:
            self._flap_clear("feed-structure", "feed structure is valid again")

        self._handle_items(parsed.items)
        self._last_parse_ok_at = datetime.now(UTC)

    def _flap_raise(self, key: str, text: str) -> None:
        """Raise only after flap_polls consecutive bad polls (one odd response is noise, not a change)."""
        n = max(self._streak.get(key, 0), 0) + 1
        self._streak[key] = n
        if n >= self.flap_polls:
            self.alerts.raise_(key, text)

    def _flap_clear(self, key: str, text: str) -> None:
        n = min(self._streak.get(key, 0), 0) - 1
        self._streak[key] = n
        if -n >= self.flap_polls:
            self.alerts.clear(key, text)

    def _record(self, it: FeedItem, now: datetime, state: str, detail: str = "") -> bool:
        """Insert; True if new. On a storage error (disk full, read-only filesystem) fail OPEN: remember the
        guid in memory and treat it as new, so the LED still lights. A restart could then re-trigger it."""
        try:
            return self.store.record(it.guid, it.title, it.link, it.pub_date, now, state, detail)
        except Exception as e:
            log.exception("cannot record %s", it.guid)
            self.store.mark_seen(it.guid)
            self.alerts.raise_("storage", f"cannot write the database ({type(e).__name__}: {e}). Events still "
                                          "trigger, but duplicates are only suppressed in memory until this is "
                                          "fixed: check the VM's disk space and filesystem.")
            return True

    def _handle_items(self, items: list[FeedItem]) -> None:
        now = datetime.now(UTC)
        max_age = timedelta(minutes=self.cfg.feed.max_age_min)
        first = not self._baseline_done
        unseen = [it for it in items if not self.store.seen(it.guid)]  # feed order is NOT reliable
        if not first and len(unseen) >= 3:
            # Several items appearing at once that are older than anything the last poll could have missed:
            # the feed re-numbered its items (or was replaced), they are not new emergencies. Items that
            # appeared during a feed outage are younger than (outage + max_age) and still trigger.
            gap = (now - self._last_parse_ok_at) if self._last_parse_ok_at else timedelta(0)
            limit = max_age + gap + timedelta(minutes=1)
            stale = [it for it in unseen if it.pub_date and now - it.pub_date > limit]
            if len(stale) >= 3:
                for it in stale:
                    self._record(it, now, "ignored_old", "many old items appeared at once (ids changed?)")
                log.error("FEED_FLOOD: %d old unseen items appeared at once; not triggering them", len(stale))
                self.alerts.info(f"⚠️ {len(stale)} OLD items appeared in the feed at once (oldest "
                                 f"{dur_text(now - min(it.pub_date for it in stale))} old). They look like "
                                 "re-numbered old events, so they did NOT trigger. Check the Events page.")
                unseen = [it for it in unseen if it not in stale]
        new: list[FeedItem] = []
        for it in unseen:
            if self.store.seen(it.guid):
                continue  # duplicate guid within this response
            age = (now - it.pub_date) if it.pub_date else None
            lag = age.total_seconds() if age is not None else None
            if first and (age is None or age > max_age):
                self._record(it, now, "ignored_old", "older than max_age at startup")
                log.info("EVENT_IGNORED_OLD guid=%s pub=%s title=%r", it.guid, it.pub_date, it.title)
                continue
            if self._record(it, now, "pending"):
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
        self._real_dispatches += 1
        self._last_real_dispatch = time.monotonic()
        test = self._test_task
        if test and not test.done():  # a real alert always wins over a test: stop the test's retries
            log.warning("real event during a test event: cancelling the test")
            test.cancel()
        try:
            results = await self.dispatcher.dispatch(reason)
        except Exception as e:
            log.exception("dispatch crashed")
            self._set_states(guids, "failed", f"dispatch crashed: {e!r}")
            self.alerts.raise_("dispatch", f"dispatch crashed: {e!r}")
            return
        self.alerts.clear("dispatch", "dispatching works again")
        ok = sum(r.ok for r in results)
        state = "triggered" if ok == len(results) else ("partial" if ok else "failed")
        detail = "; ".join(f"{r.name}={'ok' if r.ok else r.detail}" for r in results)
        self._set_states(guids, state, detail)

    def _set_states(self, guids: list[str], state: str, detail: str) -> None:
        try:
            for g in guids:
                self.store.set_state(g, state, detail)
        except Exception as e:  # the switching already happened; only the history entry is lost
            log.exception("cannot record dispatch result")
            self.alerts.raise_("storage", f"cannot write the database ({type(e).__name__}: {e}); check disk space.")

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
                if st.on and not st.timer_active:
                    await self._rearm_stuck(d)
            except Exception as e:
                out[d.name] = f"UNREACHABLE: {e}"
                self.device_status[d.name] = {"ip": d.cfg.ip, "ok": False, "detail": out[d.name], "checked": checked}
                self.alerts.raise_(key, f"{d.name} ({d.cfg.ip}) is not reachable: {e}. "
                                        f"It would NOT light up on an emergency.")
        return out

    async def _rearm_stuck(self, d) -> None:
        """A relay ON without an auto-off timer (power cut with 'restore last state', or switched by hand)
        would stay on forever and confuse responders: give it the device-side timer back."""
        key = f"stuck-on:{d.name}"
        log.warning("%s is ON without an auto-off timer: re-arming", d.name)
        res = await d.activate()
        if res.ok:
            self.alerts.clear(key, f"{d.name} has its auto-off timer again")
            self.alerts.info(f"⚠️ {d.name} was ON without an auto-off timer (power restore or switched by hand) and "
                             f"would have stayed on. SHERPA re-armed it: it turns off in {d.cfg.on_duration_s} s.")
        else:
            self.alerts.raise_(key, f"{d.name} is ON without an auto-off timer and re-arming it FAILED "
                                    f"({res.detail}). It will stay on: switch it off by hand.")

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

    def _check_clock(self) -> None:
        """The startup age filter and the lag figures trust the VM clock: compare it with the feed server's."""
        raw = self.fetcher.last_server_date
        if not raw:
            return
        try:
            server = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return
        if server.tzinfo is None:
            return
        skew = (datetime.now(UTC) - server).total_seconds()
        self.clock_skew_s = skew
        if abs(skew) > 60:
            self.alerts.raise_("clock-skew", f"the VM clock is {abs(skew):.0f} s {'ahead of' if skew > 0 else 'behind'} the "
                                             "feed server. The startup age filter and lag figures are wrong: enable NTP "
                                             "(sudo timedatectl set-ntp true).")
        elif abs(skew) < 30:
            self.alerts.clear("clock-skew", f"the VM clock agrees with the feed server again ({skew:+.0f} s)")

    async def _housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(24 * 3600)
            try:
                self.store.prune()
            except Exception:
                log.exception("pruning old history failed")

    def _poll_healthy(self) -> bool:
        """True while polls keep completing without crashing (used by the dead-man's switch).

        A feed outage is NOT unhealthy here: SHERPA itself alerts about that. A hung loop, or a poll that
        raises every time (so no event could ever trigger), is: then only the external monitor can tell you.
        """
        return time.monotonic() - self._last_poll_ok < self.poll_health_grace_s

    def _flush_stats(self) -> None:
        rows = self.pollstats.drain()
        if not rows:
            return
        try:
            self.store.add_poll_minutes(rows)
            self.alerts.clear("storage", "the database is writable again")
        except Exception as e:
            log.warning("could not write poll statistics; will retry", exc_info=True)
            self.pollstats.requeue(rows)
            self.alerts.raise_("storage", f"cannot write the database ({type(e).__name__}: {e}). Events still "
                                          "trigger; check the VM's disk space and filesystem.")

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
        """Poll-loop liveness for the Docker healthcheck (touched after every poll)."""
        try:
            self.health_file.parent.mkdir(parents=True, exist_ok=True)
            self.health_file.touch()
        except OSError:
            pass

    async def _status_loop(self) -> None:
        """Written on its own clock, not after polls: a slow poll interval must not look like a dead service.
        'written' proves the process is alive; 'last_poll_age_s' says whether polling is."""
        while True:
            self._write_status()
            await asyncio.sleep(self.status_every_s)

    def _write_status(self) -> None:
        """Snapshot for the web UI (a separate process). Best effort: must never disturb polling."""
        now = time.monotonic()
        try:
            snap = {
                "written": time.time(), "version": BUILD,
                "uptime_s": now - self._started,
                "poll_interval_s": self.cfg.feed.poll_interval_s,
                "last_poll_age_s": now - self._last_poll_done,
                "clock_skew_s": self.clock_skew_s,
                "config_hash": self.config_hash, "config_fallback": bool(self.config_error),
                "polls_ok": self.polls_ok, "polls_failed": self.polls_failed,
                "polls_skipped": self.polls_skipped, "events_triggered": self.events_triggered,
                "last_event_at": self.last_event_at.isoformat(timespec="seconds") if self.last_event_at else None,
                "feed_ok_age_s": now - self._last_http_ok,
                "active_alerts": self.alerts.active_keys(),
                "telegram_configured": isinstance(self.notifier, TelegramNotifier),
                "telegram_delivery": self.alerts.delivery_snapshot(),
                "feed_items_at": self.last_items_at,
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
        error = self.config_error
        while True:
            if error:  # raise_ itself de-duplicates and repeats every repeat_min until the file is fixed
                self.alerts.raise_("config-invalid", f"{path.name} is INVALID; SHERPA keeps running the previous "
                                                     "settings (also after a restart). Fix it, or use Settings → "
                                                     f"Restore last good settings in the web UI. Error: {error[:300]}")
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
                error = str(e)
                continue
            error = None
            self.alerts.clear("config-invalid", f"{path.name} is valid again")
            if new.model_dump() != self.cfg.model_dump():
                try:
                    changes = "; ".join(describe_changes(self.cfg, new)) or "no visible difference"
                except Exception:
                    log.exception("describing config changes failed")
                    changes = "details unavailable"
                log.warning("config changed on disk: restarting to apply it (%s)", changes)
                self.alerts.info(f"settings changed ({changes}): restarting to apply them"[:600])
                self.restart_requested = True
                self.stop()
                return

    # ---------- admin commands (from the web UI) ----------
    async def _command_loop(self) -> None:
        directory = commands.command_dir(self.cfg.db_path)
        while True:
            await asyncio.sleep(self.command_poll_s)
            try:
                cmds = commands.take(directory)
            except Exception:
                log.exception("reading admin commands failed")
                continue
            for cmd in cmds:
                try:
                    self._run_command(cmd)
                except Exception:
                    log.exception("admin command %s failed", cmd.get("kind"))

    def _run_command(self, cmd: dict) -> None:
        who = "the command line" if cmd["source"] == "cli" else f"the web UI ({cmd['source']})"
        if cmd["kind"] == "telegram_test":
            if time.monotonic() - self._last_tg_test < 10:
                log.warning("Telegram test from %s ignored: one ran less than 10 s ago", who)
                return
            self._last_tg_test = time.monotonic()
            log.warning("Telegram test requested from %s", who)
            if isinstance(self.notifier, TelegramNotifier):
                self.alerts.info(f"Telegram test message, requested from {who}. "
                                 "If you can read this, SHERPA alerts reach you.")
            else:
                self.alerts.info(f"Telegram test requested from {who}, but Telegram is NOT "
                                 "configured: alerts only go to the log and the web UI.")
        elif cmd["kind"] == "test_event":
            if self._test_task and not self._test_task.done():
                self.alerts.info(f"test event from {who} refused: a test event is already running")
                return
            d = cmd["duration_s"]
            log.warning("TEST EVENT requested from %s (%d s)", who, d)
            self._test_task = self._spawn(self._safe_test_event(d, who), "test-event")

    async def _safe_test_event(self, duration_s: int, who: str) -> None:
        try:
            reason = await self.test_event_blocker()
            if reason:
                raise TestRefused(reason)
            self.alerts.info(f"test event requested from {who}: switching every device ON for {duration_s} s")
            await self.send_test_event(duration_s, force=True)  # just checked
        except TestRefused as e:
            log.warning("test event refused: %s", e)
            self.alerts.info(f"test event from {who} refused: {e}")
        except Exception as e:  # CancelledError (a real event took over) is not caught: nothing to report
            log.exception("test event failed")
            self.alerts.info(f"test event FAILED internally: {e!r}")

    async def test_event_blocker(self) -> str | None:
        """Why a test event must not run now, or None. A test re-arms every timer to a few seconds, which
        would cut short a real alert that is still lighting the LED."""
        now = datetime.now(UTC)
        last_test = self.store.last_event_time(test=True)
        if last_test and (now - last_test).total_seconds() < commands.TEST_EVENT_COOLDOWN_S:
            return f"the previous test event was less than {commands.TEST_EVENT_COOLDOWN_S} s ago"
        window = max(d.on_duration_s for d in self.cfg.devices) + 30
        last_real = self.store.last_event_time(test=False)
        if last_real and (now - last_real).total_seconds() < window:
            return (f"a real event was seen {int((now - last_real).total_seconds())} s ago and its LED may still "
                    f"be on; try again in {int(window - (now - last_real).total_seconds()) + 1} s")
        if time.monotonic() - self._last_real_dispatch < window:
            return "a real activation ran recently (e.g. recovered after a restart); its LED may still be on"
        states = await asyncio.gather(*(d.read_state() for d in self.devices), return_exceptions=True)
        on = [d.name for d, st in zip(self.devices, states) if not isinstance(st, BaseException) and st.on]
        if on:
            return f"{', '.join(on)} {'is' if len(on) == 1 else 'are'} already ON - a real alert may be active"
        return None

    # ---------- test event ----------
    async def send_test_event(self, duration_s: int | None = None, force: bool = False) -> list:
        """Real dispatch path with a short timer. Refused (TestRefused) while a real alert may be active,
        unless force=True. If a real event arrives meanwhile, the test is cancelled and the real ON re-sent."""
        if not force:
            reason = await self.test_event_blocker()
            if reason:
                raise TestRefused(reason)
        seq = self._real_dispatches
        now = datetime.now(UTC)
        guid = f"test:{now.strftime('%Y%m%dT%H%M%S.%f')}"
        self.store.record(guid, "SHERPA test event", "", now, now, "pending", is_test=True)
        try:
            results = await self.dispatcher.dispatch("TEST EVENT", duration_s)
        except asyncio.CancelledError:
            self.store.set_state(guid, "abandoned", "cancelled: a real event arrived during the test")
            self.alerts.info("test event cancelled: a real event arrived; its activation was re-sent")
            raise
        finally:
            if self._real_dispatches != seq:  # a real event overlapped: make sure the real timer wins
                self._spawn(self._rearm_after_test(), "rearm")
        ok = all(r.ok for r in results)
        self.store.set_state(guid, "triggered" if ok else "failed",
                             "; ".join(f"{r.name}={'ok' if r.ok else r.detail}" for r in results))
        self.alerts.info(f"test event dispatched: " + ", ".join(
            f"{r.name}={'OK' if r.ok else 'FAIL'}" for r in results))
        return results

    async def _rearm_after_test(self) -> None:
        """Re-send the real ON now and once more after a device timeout, so a late in-flight test
        request (short timer) can never be the last word on a device during a real alert."""
        delay = max(d.timeout_s for d in self.cfg.devices) + 1
        for i in range(2):
            if i:
                await asyncio.sleep(delay)
            try:
                await self.dispatcher.dispatch("re-arm: a test event overlapped a real event")
            except Exception:
                log.exception("re-arm after test overlap failed")
        log.warning("re-armed the real activation after a test event overlapped it")
