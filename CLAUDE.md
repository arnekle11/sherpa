# SHERPA - working notes for Claude

Full decisions: `docs/SPEC.md`. Read it before changing behaviour. Operating it: `docs/RUNBOOK.md`.

## What this is
Polls a public SOS 112 RSS feed (server-side filtered), and on each *new guid* switches a Shelly relay ON with a device-side 300 s auto-off timer. First-responder use: reliability and **visible failures** beat features.

## Layout
- `src/sherpa/app.py` poll loop, dedupe, recovery, probes, heartbeat
- `feed.py` fetch (keep-alive, hard timeout, size cap) + strict parse; `store.py` SQLite
- `devices.py` Gen1/Gen2 adapters (ON + timer + read-back); `dispatcher.py` parallel fan-out
- `alerts.py` state-based alerts, Telegram; `mock.py` mock feed/Shelly/Telegram; `selftest.py`; `cli.py`
- `lights.py` manual colour/effect scenes; `pollstats.py` per-minute poll history; `deadman.py` optional external ping; `commands.py` web -> service command queue (test event, Telegram test)
- `web.py` (server) + `webpages.py`/`webstats.py`/`webcharts.py`/`webexport.py`/`webstyle.py` (HTML, SVG charts, CSV/JSON/metrics, CSS) + `settings.py`: separate `sherpa web` process (stdlib, no JS) reading the DB and `status.json`, editing `config/config.yaml`; the service watches the file and restarts itself (exit 75) on a valid change

## Invariants - do not break
- Never trigger twice for one guid; never trigger on stale items at startup; never rely on feed item order.
- Fail open toward lighting the LED: a malformed item must not hide the others (lenient parse + alert), a
  database write error must not stop a dispatch (in-memory dedupe + alert).
- Polls never overlap; slow polls skip ticks. No exception may kill the poll loop.
- Auto-off timer is always device-side. "Relay on, no timer" is a failure.
- Empty feed is normal; structure/self-link changes and outages must alert.
- A test event must never shorten a real alert (it re-arms timers to a few seconds): it is refused while a real
  event is younger than on_duration + 30 s or any relay is ON, a real event cancels a running test and re-sends
  its ON, and test rows are never replayed as real activations after a restart.
- Manual light scenes (colours, effects, 'until I turn it off', `lights.py`) must keep the invariants: every ON frame
  carries a short device-side timer (renewed by the next frame, never a timer-less ON), frames are never faster than
  2.5/s, a real dispatch cancels all scenes first and re-arms, and nothing manual starts or stops during a real alert.
- Only `sherpa run` may ping the dead-man URL (selftest/test-event must not report "alive").
- An invalid config must never stop SHERPA: it runs the last-good copy (`--fallback`) and alerts until fixed.
- Per-poll alerts use hysteresis (3 polls); alert queue is bounded and device alerts go first.
- The web process never switches devices or sends Telegram itself: it queues a command, the service validates and runs it.
- The web UI must never be able to stop polling (separate process), never expose Telegram secrets, and never allow poll_interval < 2 s. All feed text shown in it is untrusted: escape it.
- Secrets only in `.env`; never log the Telegram token (httpx INFO logging is silenced for this reason).

## Testing habits that found real bugs
Independent reviewer subagents per area; mutation-test safety fixes (remove the fix, the test must fail);
run the suite on Python 3.12 too (`python:3.12-slim` container): the image is 3.12, a dev venv may not be.

## Commands
```
pip install -e '.[dev]'; pytest; sherpa selftest
sherpa check -c config.yaml            # read-only preflight (feed, clock, database, devices)
sherpa status -c config.yaml           # what the dashboard says, in a terminal
sherpa test-event -c config.yaml --duration 10
sherpa mock-feed / mock-shelly --gen 2 # manual testing against a local fake
```

## Environment constraints
Cloud dev sandbox cannot reach spin3.sos112.si or the LAN: develop against mocks only. Fixtures are synthetic, built from the observed item structure. Don't open PRs unless asked.
