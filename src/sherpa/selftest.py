"""End-to-end self-test of the real App against mock feed / Shellies / Telegram.

`sherpa selftest` exits non-zero on any failure. It proves the *pipeline*; it cannot
prove the real feed filter is right (an empty real feed looks identical to a broken
filter) - that is what the self-link canary and `sherpa check` are for.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from .app import App
from .config import AppCfg, DeviceCfg, FeedCfg, AlertsCfg
from .mock import MockFeed, MockShelly, MockTelegram


async def _until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


async def run_selftest(verbose: bool = True) -> list[str]:
    """Returns a list of failure descriptions (empty = all passed)."""
    failures: list[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        if verbose:
            print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            failures.append(name + (f" [{extra}]" if extra else ""))

    feed, s1, s2, tg = MockFeed().start(), MockShelly(1).start(), MockShelly(2).start(), MockTelegram().start()
    tmp = tempfile.TemporaryDirectory()
    try:
        # three stale items (2 h old) that must be ignored at startup
        for i in range(3):
            feed.add_item(f"Prometna nesreča 2. 11. 2026 1{i}:00", age_s=7200)
        cfg = AppCfg(
            feed=FeedCfg(url=feed.url, poll_interval_s=0.2, timeout_s=1.0, max_age_min=10, down_after_s=1.5),
            devices=[DeviceCfg(name="gen1", ip=s1.hostport, generation=1, attempts=3, retry_delay_s=0.05, timeout_s=1),
                     DeviceCfg(name="gen2", ip=s2.hostport, generation=2, attempts=3, retry_delay_s=0.05, timeout_s=1)],
            alerts=AlertsCfg(telegram_api_base=tg.api_base, repeat_min=0, device_check_interval_s=0),
            db_path=str(Path(tmp.name) / "t.db"),
        )

        def msgs(sub: str) -> bool:
            return any(sub in m for m in tg.messages)

        async def start() -> tuple[App, asyncio.Task]:
            a = App(cfg, telegram=("TESTTOKEN", "1"))
            return a, asyncio.create_task(a.run())

        async def stop(a: App, t: asyncio.Task) -> None:
            a.stop()
            await asyncio.wait_for(t, 15)

        app, task = await start()
        await _until(lambda: app.polls_ok >= 3)
        check("startup alert delivered to Telegram", await _until(lambda: msgs("started")))
        check("old items at startup do NOT trigger", not s1.on_calls and not s2.on_calls)
        check("empty/idle state raises no alert", not msgs("🚨"))

        # 1. new event
        guid = feed.add_item("Požar, eksplozija 2. 11. 2026 17:30", description="Ljubljana")
        check("new event switches both Shellies ON",
              await _until(lambda: s1.is_on and s2.is_on))
        check("Gen1 got timer=300", s1.on_calls[-1].get("timer") == "300", str(s1.on_calls))
        check("Gen2 got toggle_after=300", s2.on_calls[-1].get("toggle_after") == "300", str(s2.on_calls))
        await _until(lambda: app.store.recent(1)[0]["state"] == "triggered")
        check("event recorded as triggered with lag", app.store.recent(1)[0]["state"] == "triggered"
              and app.store.recent(1)[0]["lag_s"] is not None)

        # 2. description edit must not re-trigger
        n1, n2 = len(s1.on_calls), len(s2.on_calls)
        feed.set_description(guid, "Gori garaža. Gasilci na kraju dogodka.")
        await asyncio.sleep(1.0)
        check("changed description does NOT re-trigger", (len(s1.on_calls), len(s2.on_calls)) == (n1, n2))

        # 3. second event extends timer
        feed.add_item("Prometna nesreča 2. 11. 2026 17:40")
        check("second event re-arms timer (extends)", await _until(lambda: len(s1.on_calls) == n1 + 1 and len(s2.on_calls) == n2 + 1))

        # 4. transient device failure is retried and confirmed
        with s1.lock:
            s1.fail_next = 2
        feed.add_item("Tehnična in druga pomoč 2. 11. 2026 17:50")
        check("transient Shelly failure retried to success", await _until(lambda: len(s1.on_calls) >= n1 + 2))
        check("no failure alert after successful retry", not msgs("FAILED"))

        # 5. dead device -> alert, other device still fires, then recovery
        with s2.lock:
            s2.dead = True
        n1 = len(s1.on_calls)
        feed.add_item("Požar, eksplozija 2. 11. 2026 18:00")
        check("dead Shelly raises FAILED alert", await _until(lambda: msgs("gen2") and msgs("FAILED")))
        check("healthy Shelly still switched despite other failing", len(s1.on_calls) > n1)
        s2.reset()
        feed.add_item("Požar, eksplozija 2. 11. 2026 18:05")
        check("device recovery alert sent", await _until(lambda: msgs("confirmed ON again")))

        # 6. self-link canary
        feed.channel_link = "https://spin3.sos112.si/javno/OD/True"
        check("changed self-link raises alert", await _until(lambda: msgs("self-link changed")))
        feed.channel_link = "https://spin3.sos112.si/javno/OD/52034"
        check("self-link restored clears alert", await _until(lambda: msgs("self-link is back")))

        # 7. feed down / recovery
        feed.mode = "500"
        check("feed outage raises UNREACHABLE alert", await _until(lambda: msgs("UNREACHABLE"), 6))
        feed.mode = "ok"
        check("feed recovery alert sent", await _until(lambda: msgs("reachable again")))

        # 8. structure change
        feed.mode = "garbage"
        check("non-RSS body raises structure alert", await _until(lambda: msgs("structure CHANGED")))
        feed.mode = "ok"
        check("valid structure clears alert", await _until(lambda: msgs("structure is valid again")))

        # 9. restart durability: known guids never re-trigger
        await stop(app, task)
        n1, n2 = len(s1.on_calls), len(s2.on_calls)
        app, task = await start()
        await _until(lambda: app.polls_ok >= 3)
        await asyncio.sleep(0.6)
        check("restart does NOT re-trigger known events", (len(s1.on_calls), len(s2.on_calls)) == (n1, n2))
        # ...but an event that appeared while we were down is caught
        await stop(app, task)
        feed.add_item("Požar, eksplozija 2. 11. 2026 18:30", age_s=120)
        app, task = await start()
        check("event that appeared during downtime IS triggered on restart",
              await _until(lambda: len(s1.on_calls) == n1 + 1))
        await stop(app, task)
    finally:
        for s in (feed, s1, s2, tg):
            s.stop()
        tmp.cleanup()
    return failures
