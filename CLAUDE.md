# SHERPA - working notes for Claude

Full decisions: `docs/SPEC.md`. Read it before changing behaviour.

## What this is
Polls a public SOS 112 RSS feed (server-side filtered), and on each *new guid* switches a Shelly relay ON with a device-side 300 s auto-off timer. First-responder use: reliability and **visible failures** beat features.

## Layout
- `src/sherpa/app.py` poll loop, dedupe, recovery, probes, heartbeat
- `feed.py` fetch (keep-alive, hard timeout, size cap) + strict parse; `store.py` SQLite
- `devices.py` Gen1/Gen2 adapters (ON + timer + read-back); `dispatcher.py` parallel fan-out
- `alerts.py` state-based alerts, Telegram; `mock.py` mock feed/Shelly/Telegram; `selftest.py`; `cli.py`
- `pollstats.py` per-minute poll history; `deadman.py` optional external ping; `commands.py` web -> service command queue (test event, Telegram test)
- `web.py` (server) + `webpages.py`/`webstats.py`/`webcharts.py`/`webexport.py`/`webstyle.py` (HTML, SVG charts, CSV/JSON/metrics, CSS) + `settings.py`: separate `sherpa web` process (stdlib, no JS) reading the DB and `status.json`, editing `config/config.yaml`; the service watches the file and restarts itself (exit 75) on a valid change

## Invariants - do not break
- Never trigger twice for one guid; never trigger on stale items at startup; never rely on feed item order.
- Polls never overlap; slow polls skip ticks. No exception may kill the poll loop.
- Auto-off timer is always device-side. "Relay on, no timer" is a failure.
- Empty feed is normal; structure/self-link changes and outages must alert.
- The web process never switches devices or sends Telegram itself: it queues a command, the service validates and runs it.
- The web UI must never be able to stop polling (separate process), never expose Telegram secrets, and never allow poll_interval < 2 s. All feed text shown in it is untrusted: escape it.
- Secrets only in `.env`; never log the Telegram token (httpx INFO logging is silenced for this reason).

## Commands
```
pip install -e '.[dev]'; pytest; sherpa selftest
sherpa check -c config.yaml            # read-only preflight
sherpa test-event -c config.yaml --duration 10
sherpa mock-feed / mock-shelly --gen 2 # manual testing against a local fake
```

## Environment constraints
Cloud dev sandbox cannot reach spin3.sos112.si or the LAN: develop against mocks only. Fixtures are synthetic, built from the observed item structure. Don't open PRs unless asked.
