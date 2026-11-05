from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import tempfile
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import __version__
from .app import App, TestRefused
from .config import (AppCfg, deadman_url_from_env, load_config, load_config_with_fallback, save_last_good,
                     telegram_from_env)
from .devices import make_device, make_device_client
from .feed import FeedFetcher, FeedFormatError, FetchError, parse_feed
from .store import Store


RESTART_EXIT = 75  # settings changed; docker "restart: unless-stopped" brings the service back with them


def setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO, which would put the Telegram bot token in the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _cfg(args) -> AppCfg:
    try:
        cfg = load_config(args.config)
    except Exception as e:
        sys.exit(f"config error ({args.config}): {e}")
    setup_logging(cfg.log_level)
    return cfg


def _cfg_with_fallback(args) -> tuple[AppCfg, str | None]:
    try:
        cfg, err = load_config_with_fallback(args.config, args.fallback)
    except Exception as e:
        sys.exit(f"config error ({args.config}): {e}")
    setup_logging(cfg.log_level)
    if err:
        logging.getLogger("sherpa").error("%s is INVALID, running the last good copy %s: %s", args.config, args.fallback, err)
    return cfg, err


FATAL_MARKER = Path(tempfile.gettempdir()) / "sherpa-fatal-reported"


def _report_fatal(text: str, api_base: str = "https://api.telegram.org") -> None:
    """Best effort: tell Telegram that the service cannot run. Docker restarts it forever, which is otherwise
    silent. At most every 30 min (the marker in /tmp survives Docker's restarts of the same container)."""
    creds = telegram_from_env()
    if not creds:
        return
    try:
        if time.time() - FATAL_MARKER.stat().st_mtime < 1800:
            return
    except OSError:
        pass
    token, chat = creds
    msg = (f"[SHERPA {socket.gethostname()}] 🚨 SHERPA cannot run: {text[:500]}. Docker keeps retrying; nothing is "
           "monitored and no LED will switch until this is fixed (docker compose logs sherpa).")
    try:
        with httpx.Client(timeout=10, trust_env=False) as c:
            c.post(f"{api_base.rstrip('/')}/bot{token}/sendMessage", json={"chat_id": chat, "text": msg})
        FATAL_MARKER.touch()
    except Exception:
        pass  # nothing else we can do; the traceback is in the container log


def cmd_run(args) -> int:
    api_base = "https://api.telegram.org"
    try:
        cfg, err = _cfg_with_fallback(args)
        api_base = cfg.alerts.telegram_api_base
        if not err:
            try:
                save_last_good(args.config, args.fallback)
            except OSError as e:
                logging.getLogger("sherpa").warning("cannot keep a last-good copy of the config: %s", e)

        async def main() -> int:
            app = App(cfg, config_path=args.config, deadman=deadman_url_from_env(), config_error=err)
            FATAL_MARKER.unlink(missing_ok=True)  # started: report the next failure at once
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, app.stop)
            await app.run()
            return RESTART_EXIT if app.restart_requested else 0

        return asyncio.run(main())
    except SystemExit as e:
        if e.code not in (0, None, RESTART_EXIT):
            _report_fatal(str(e.code), api_base)
        raise
    except Exception as e:
        _report_fatal(f"{type(e).__name__}: {e}", api_base)
        raise


def _service_alive(cfg: AppCfg) -> bool:
    try:
        st = json.loads((Path(cfg.db_path).parent / "status.json").read_text())
        return abs(time.time() - st["written"]) < 30
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _test_event_via_service(cfg: AppCfg, duration: int) -> int:
    """Queue the test for the running service, so a real event arriving meanwhile cancels it and re-arms
    the real timer. Waits for the outcome."""
    import sqlite3

    from . import commands
    if not 1 <= duration <= commands.TEST_EVENT_MAX_S:
        print(f"With the service running, --duration must be 1-{commands.TEST_EVENT_MAX_S} s."); return 2
    t0 = datetime.now(UTC).isoformat()
    commands.submit(commands.command_dir(cfg.db_path), "test_event", "cli", duration_s=duration)
    print(f"Queued for the running service ({duration} s); waiting for the result...")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            db = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=2)
            refused = db.execute("SELECT text FROM alert_log WHERE ts >= ? AND text LIKE '%test event%refused%' "
                                 "ORDER BY id DESC LIMIT 1", (t0,)).fetchone()
            row = db.execute("SELECT state, detail FROM events WHERE is_test=1 AND first_seen >= ? AND state != 'pending' "
                             "ORDER BY first_seen DESC LIMIT 1", (t0,)).fetchone()
            db.close()
        except sqlite3.Error:
            continue
        if refused:
            print("REFUSED:", refused[0].split("refused: ", 1)[-1]); return 3
        if row:
            print(f"{'OK  ' if row[0] == 'triggered' else 'FAIL'} {row[0]}: {row[1]}")
            return 0 if row[0] == "triggered" else 2
    print("No result within 90 s: check the Alerts page and `docker compose logs sherpa`."); return 4


def cmd_test_event(args) -> int:
    cfg = _cfg(args)
    dur = args.duration or None
    names = ", ".join(f"{d.name}@{d.ip}" for d in cfg.devices)
    if not args.yes:
        if input(f"This will switch ON for real: {names}. Continue? [y/N] ").strip().lower() != "y":
            return 1
    if _service_alive(cfg) and not args.force:
        return _test_event_via_service(cfg, dur or 10)

    async def main() -> int:
        app = App(cfg)
        worker = asyncio.create_task(app.alerts.run())
        try:
            results = await app.send_test_event(dur, force=args.force)
        except TestRefused as e:
            print(f"REFUSED: {e}\n(use --force only if you are sure no real alert is active)")
            await app.alerts.drain(); worker.cancel()
            await app.fetcher.aclose(); await app._dev_client.aclose()
            return 3
        for r in results:
            print(f"{'OK  ' if r.ok else 'FAIL'} {r.name}: {r.detail} (attempts={r.attempts}, {r.elapsed_s:.2f}s)")
        await app.alerts.drain()
        worker.cancel()
        await app.fetcher.aclose()
        await app._dev_client.aclose()
        return 0 if all(r.ok for r in results) else 2

    return asyncio.run(main())


def cmd_check(args) -> int:
    """Read-only preflight: fetch the real feed once, probe every Shelly. Switches nothing."""
    cfg = _cfg(args)

    async def main() -> int:
        rc = 0
        fetcher = FeedFetcher(cfg.feed)
        t0 = time.monotonic()
        try:
            body = await fetcher.fetch()
            parsed = parse_feed(body)
            print(f"feed OK: {len(body)} bytes in {(time.monotonic()-t0)*1000:.0f} ms, {len(parsed.items)} item(s)")
            if fetcher.last_server_date:
                from email.utils import parsedate_to_datetime
                skew = (datetime.now().astimezone() - parsedate_to_datetime(fetcher.last_server_date)).total_seconds()
                print(f"  clock: VM {skew:+.1f} s vs the feed server" + ("  PROBLEM: enable NTP" if abs(skew) > 60 else ""))
                rc = 1 if abs(skew) > 60 else rc
            print(f"  self-link: {parsed.self_link}")
            exp = cfg.feed.expected_link_contains
            if exp and (not parsed.self_link or exp not in parsed.self_link):
                print(f"  WARNING: self-link does not contain {exp!r}"); rc = 1
            for p in parsed.problems:
                print(f"  PROBLEM: {p}"); rc = 1
            for it in parsed.items[:5]:
                print(f"  - {it.pub_date} {it.title!r} {it.guid}")
        except (FetchError, FeedFormatError) as e:
            print(f"feed FAILED: {e}"); rc = 1
        await fetcher.aclose()
        try:  # the service must be able to create/write its database (a wrong db_path crash-loops it)
            Store(cfg.db_path).close()
            print(f"database OK: {cfg.db_path}")
        except Exception as e:
            print(f"database FAILED ({cfg.db_path}): {e}"); rc = 1
        client = make_device_client()
        for dc in cfg.devices:
            dev = make_device(dc, client)
            try:
                st = await dev.read_state()
                print(f"device {dc.name}@{dc.ip} (gen{dc.generation}) OK: relay {'ON' if st.on else 'off'}")
            except Exception as e:
                print(f"device {dc.name}@{dc.ip} FAILED: {e}"); rc = 1
        await client.aclose()
        return rc

    return asyncio.run(main())


def cmd_lag(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    st = store.lag_stats()
    if st:
        print(f"first_seen - pubDate over {st['n']} events (s): min={st['min']:.1f} "
              f"median={st['median']:.1f} p95={st['p95']:.1f} max={st['max']:.1f}")
    else:
        print("no events recorded yet")
    for r in store.recent(args.n):
        lag = f"{r['lag_s']:.1f}s" if r["lag_s"] is not None else "n/a"
        print(f"{r['first_seen']}  lag={lag:>8}  {r['state']:<11} {r['title']}")
    return 0


def cmd_status(args) -> int:
    """One-screen summary for SSH sessions; same rules as the web dashboard. Exit 0 ok, 1 attention, 2 no data."""
    from .web import Web
    from .webpages import alert_help, alert_label, ago, dur, not_notifying, overview
    try:
        w = Web(args.config, None)
    except Exception as e:
        sys.exit(f"config error ({args.config}): {e}")
    ov = overview(w)
    st = ov["st"]
    print(f"SHERPA: {ov['pill'].upper()}")
    if st is None:
        print("  no status yet: the service is not running, or this is not its config/data volume")
        return 2
    print(f"  status written {ago(time.time() - st['written'])}; last poll {ago(st.get('last_poll_age_s') or 0)}; "
          f"polling every {st['poll_interval_s']:g} s; up {dur(st['uptime_s'])}")
    print(f"  polls since start: ok={st['polls_ok']} failed={st['polls_failed']} skipped={st['polls_skipped']}; "
          f"last good feed response {ago(st['feed_ok_age_s'])}")
    why = not_notifying(st)
    tel = {"not_configured": "NOT configured", "failing": "FAILING"}.get(why or "", "ok")
    dm = st.get("deadman") or {}
    print(f"  telegram: {tel}; dead-man's switch: "
          + ("not configured" if not dm.get("configured") else (f"failing ({dm['last_error']})" if dm.get("last_error") else "pinging")))
    for name, d in (st.get("devices") or {}).items():
        print(f"  device {name} ({d.get('ip')}): {d.get('detail')}")
    for k in ov["alerts"]:
        print(f"  ALERT {alert_label(k)}" + (f"\n        what to do: {alert_help(k)}" if alert_help(k) else ""))
    rows = w.query("SELECT first_seen, state, title FROM events WHERE is_test=0 ORDER BY first_seen DESC LIMIT 3")
    for r in rows:
        seen = datetime.fromisoformat(r["first_seen"]).astimezone().strftime("%d %b %H:%M")
        print(f"  event {seen}  {r['state']:<11} {r['title']}")
    return 0 if ov["level"] == "ok" else 1


def cmd_backup(args) -> int:
    """Consistent copy of the live database (SQLite online backup; safe while the service runs)."""
    import sqlite3
    cfg = _cfg(args)
    out = Path(args.out or Path(cfg.db_path).parent / f"sherpa-backup-{time.strftime('%Y%m%d-%H%M%S')}.db")
    if out.exists():
        sys.exit(f"{out} already exists")
    src = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    with dst:
        src.backup(dst)
    n = dst.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    src.close(); dst.close()
    print(f"backup written: {out} ({n} events)")
    return 0


def cmd_selftest(args) -> int:
    from .selftest import run_selftest
    print("SHERPA end-to-end self-test (mock feed + mock Shellies + mock Telegram)")
    failures = asyncio.run(run_selftest())
    print("ALL PASSED" if not failures else f"{len(failures)} FAILED")
    return 1 if failures else 0


def cmd_healthcheck(args) -> int:
    f = Path(args.file)
    try:
        age = time.time() - f.stat().st_mtime
    except OSError:
        print("no health file"); return 1
    limit = args.max_age
    try:  # a slow poll interval (set in the web UI) must not make a healthy service look unhealthy
        interval = float(json.loads((f.parent / "status.json").read_text())["poll_interval_s"])
        limit = max(limit, 3 * interval + 10)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    print(f"poll loop last alive {age:.0f}s ago (limit {limit:.0f}s)")
    return 0 if age < limit else 1


def cmd_web(args) -> int:
    import os
    import threading

    from .web import serve
    _cfg_with_fallback(args)  # validates the config (or falls back) and sets up logging
    srv = serve(args.config, os.environ.get("SHERPA_WEB_PASSWORD", ""), args.host, args.port, fallback=args.fallback)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    return 0


def cmd_mock_feed(args) -> int:
    from .mock import MockFeed
    f = MockFeed(host=args.host, port=args.port).start()
    print(f"mock feed on http://{args.host}:{f.port}/feed  (idle/empty)\n"
          f"inject an event:  curl -X POST 'http://{args.host}:{f.port}/inject?title=Test&description=Ljubljana'\n"
          f"break it:         curl -X POST 'http://{args.host}:{f.port}/mode?mode=500'   (ok|500|garbage|slow)")
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_mock_shelly(args) -> int:
    from .mock import MockShelly
    s = MockShelly(args.gen, host=args.host, port=args.port).start()
    print(f"mock Gen{args.gen} Shelly on http://{args.host}:{s.port}  (use ip: {args.host}:{s.port})")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sherpa", description="SHERPA - SOS 112 feed -> Shelly LED alerting")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help, config=True):
        sp = sub.add_parser(name, help=help)
        if config:
            sp.add_argument("-c", "--config", default=os.environ.get("SHERPA_CONFIG", "config.yaml"))
        sp.set_defaults(fn=fn)
        return sp

    sp = add("run", cmd_run, "run the service")
    sp.add_argument("--fallback", help="last-good config copy: written after a valid start, used if the config is invalid")
    sp = add("test-event", cmd_test_event, "send a synthetic event through the real dispatch path (switches Shellies ON)")
    sp.add_argument("--duration", type=int, default=0, help="override on-duration in seconds (e.g. 10)")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.add_argument("--force", action="store_true", help="run even if a real alert may be active (cuts it short)")
    add("check", cmd_check, "read-only preflight: fetch the real feed, probe Shellies")
    sp = add("lag", cmd_lag, "show first-seen vs pubDate lag statistics")
    sp.add_argument("-n", type=int, default=15)
    add("status", cmd_status, "one-screen summary of the running service (same rules as the dashboard)")
    sp = add("backup", cmd_backup, "consistent copy of the database, safe while the service runs")
    sp.add_argument("--out", help="target file (default: next to the database, timestamped)")
    add("selftest", cmd_selftest, "end-to-end self-test against mocks", config=False)
    sp = add("healthcheck", cmd_healthcheck, "exit 1 if the poll loop is stale", config=False)
    sp.add_argument("--file", default="/data/health")
    sp.add_argument("--max-age", type=float, default=30)
    sp = add("web", cmd_web, "run the web UI (dashboard, history, settings); password in SHERPA_WEB_PASSWORD")
    sp.add_argument("--fallback", help="last-good config copy (see run --fallback)")
    sp.add_argument("--host", default="0.0.0.0"); sp.add_argument("--port", type=int, default=8080)
    sp = add("mock-feed", cmd_mock_feed, "serve a controllable mock RSS feed", config=False)
    sp.add_argument("--host", default="127.0.0.1"); sp.add_argument("--port", type=int, default=8099)
    sp = add("mock-shelly", cmd_mock_shelly, "serve a mock Shelly", config=False)
    sp.add_argument("--gen", type=int, choices=(1, 2), default=1)
    sp.add_argument("--host", default="127.0.0.1"); sp.add_argument("--port", type=int, default=8098)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
