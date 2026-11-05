from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .app import App, TestRefused
from .config import AppCfg, deadman_url_from_env, load_config
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


def cmd_run(args) -> int:
    cfg = _cfg(args)

    async def main() -> int:
        app = App(cfg, config_path=args.config, deadman=deadman_url_from_env())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, app.stop)
        await app.run()
        return RESTART_EXIT if app.restart_requested else 0

    return asyncio.run(main())


def cmd_test_event(args) -> int:
    cfg = _cfg(args)
    dur = args.duration or None
    names = ", ".join(f"{d.name}@{d.ip}" for d in cfg.devices)
    if not args.yes:
        if input(f"This will switch ON for real: {names}. Continue? [y/N] ").strip().lower() != "y":
            return 1

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
    print(f"poll loop last alive {age:.0f}s ago")
    return 0 if age < args.max_age else 1


def cmd_web(args) -> int:
    import os
    import threading

    from .web import serve
    _cfg(args)  # validates the config and sets up logging
    srv = serve(args.config, os.environ.get("SHERPA_WEB_PASSWORD", ""), args.host, args.port)
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
            sp.add_argument("-c", "--config", default="config.yaml")
        sp.set_defaults(fn=fn)
        return sp

    add("run", cmd_run, "run the service")
    sp = add("test-event", cmd_test_event, "send a synthetic event through the real dispatch path (switches Shellies ON)")
    sp.add_argument("--duration", type=int, default=0, help="override on-duration in seconds (e.g. 10)")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.add_argument("--force", action="store_true", help="run even if a real alert may be active (cuts it short)")
    add("check", cmd_check, "read-only preflight: fetch the real feed, probe Shellies")
    sp = add("lag", cmd_lag, "show first-seen vs pubDate lag statistics")
    sp.add_argument("-n", type=int, default=15)
    add("selftest", cmd_selftest, "end-to-end self-test against mocks", config=False)
    sp = add("healthcheck", cmd_healthcheck, "exit 1 if the poll loop is stale", config=False)
    sp.add_argument("--file", default="/data/health")
    sp.add_argument("--max-age", type=float, default=30)
    sp = add("web", cmd_web, "run the web UI (dashboard, history, settings); password in SHERPA_WEB_PASSWORD")
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
