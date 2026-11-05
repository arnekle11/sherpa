# SHERPA - Specification and decisions

SHERPA = Smart Home Emergency Response Platform for Alerting.

## Goal
When a new item appears in a public SOS 112 RSS feed, immediately switch on a Shelly relay
that powers an LED bar for 5 minutes. Used by first responders: **reliability and visible
failures matter more than features**. One Shelly now; designed for many.

**This feed is a public information service and must NOT be the only alert path.**

## The feed (confirmed from real responses)
- `https://spin3.sos112.si/javno/ODApi/52034` - 52034 is a *server-side filter*. Every new item is a trigger; no client-side matching of type/place.
- RSS 2.0, GET only (HEAD -> 405), HTTP/1.1. No push; polling is the only option.
- No ETag/Last-Modified/Cache-Control/rate-limit headers -> conditional requests impossible. Rate limit unknown (to ask the operator).
- Idle = valid RSS, empty channel, ~287 bytes. **Empty is the normal state, not an error.**
- Timing from author's PC: DNS 19 ms, TCP 13, TLS 26, server think ~142 (dominant), total ~200 ms. Keep-alive saves ~60 ms/poll.
- Item: `title` "<Event type> D. M. YYYY HH:MM" (local time); `description` = municipality (new) or a Slovenian report (filled in later); `guid` = `link` = `https://spin3.sos112.si/javno/zemljevid/<id>`; `pubDate` GMT.
- Item order is NOT reliable; items stay ~31 h (~35 on the unfiltered feed); content changes after first appearance, guid stays.
- Channel self-link reflects the filter (`.../OD/52034`) - canary for a changed filter.
- Event types seen: Prometna nesreča, Tehnična in druga pomoč, Požar eksplozija, Onesnaženje nesreče z nevarnimi snovmi.

## Decisions
| Area | Decision |
|---|---|
| Stack | Python 3.12, asyncio, httpx (persistent connection), lxml, SQLite, YAML config (pydantic-validated), Docker Compose |
| Deploy | Small Ubuntu VM on Proxmox, fixed IP. Not Windows Server. |
| Polling | `poll_interval_s` in config; start 2 s, aim 1 s if operator agrees. Polls never overlap: ~3 s hard timeout; a slow poll **skips ticks, never queues**. |
| Dedupe | By guid, persisted in SQLite (`synchronous=FULL`). Trigger only the first time a guid is seen; later description edits ignored. |
| Startup | Items with `pubDate` older than `max_age_min` are recorded as `ignored_old` on the first successful poll, never triggered. After that, any unseen guid triggers (so events that appeared during an outage are not lost). |
| Crash safety | An event is recorded `pending` *before* dispatch; on restart, `pending` rows younger than `max_age_min` are re-dispatched. |
| Action | ON with **device-side** auto-off timer (Gen1 `timer=300`; Gen2 `toggle_after=300`). Survives app death. A new event re-sends ON, restarting the timer (extends). |
| Confirmation | Read back state; success = relay on **and** a timer armed. Relay on without a timer is a failure (it would never turn off). |
| Devices | By LAN IP, config list (name, ip, generation, model label, channel, on_duration_s, timeout, attempts). Dispatched in parallel, per-device timeout + retries (exponential backoff). One adapter per generation behind `ShellyDevice`. |
| Alerts | Telegram (token/chat id in `.env`, never git): feed unreachable > `down_after_s`; Shelly fails to confirm; feed structure or self-link change; start/restart; optional heartbeat; Shelly unreachable (periodic read-only probe). State-based: sent once, reminder every `repeat_min`, "recovered" message on clear. |
| Logging | Every new event logs `first_seen` vs `pubDate` (`EVENT_NEW ... lag_s=`) and stores it in SQLite; `sherpa lag` summarises. Needs NTP-synced VM clock. |
| Tests | `sherpa test-event` (real dispatch path to real Shellies), `sherpa selftest` (full pipeline vs mocks), `sherpa check` (read-only preflight vs the real feed + Shellies). |
| Runtime | Docker, `restart: unless-stopped`, healthcheck on poll-loop liveness. |

## Web UI (added after the first deploy)
- Separate `sherpa web` process/container: read-only for anyone on the LAN (status, devices, events + lag, alert history); settings need `SHERPA_WEB_PASSWORD`. Plain HTTP, LAN only.
- Data flow: service writes `/data/status.json` (every >=5 s) and the SQLite `events` + `alert_log` tables; web opens the DB read-only.
- Editable: poll interval (floor 2 s), max_age, down_after, repeat/heartbeat/device-check, device list. Not editable: feed URL, canary, db_path, Telegram.
- Apply = web atomically rewrites `config/config.yaml` (+ `.bak`); the service polls the file mtime every 5 s, validates it, and exits with code 75 so `restart: unless-stopped` restarts it. Invalid file => alert `config-invalid`, old settings keep running. Restart is safe: unseen items newer than max_age are still triggered, pending rows are re-dispatched.
- Hardening: no JS, CSP `default-src 'none'`, escaped output, CSRF token, SameSite=Strict cookie, 30 min idle session, 5 failed logins / 5 min lockout.
- Maintenance tools: the web writes a JSON command to `/data/commands`; the service claims it (read + delete = at most once), re-validates kind/age (60 s)/duration (1-60 s), rate-limits test events to 1 per 30 s, and runs it through the normal alert/dispatch path. The web never talks to devices.
- Observability: `poll_log` table (per-minute ok/failed/skipped + latency, flushed once a minute, 60-day retention); Stats page; `/metrics`, `/api/status`, `/events.csv` (cells starting with = + - @ are prefixed with ').

## Dead-man's switch
Optional `SHERPA_DEADMAN_URL` (secret, `.env`): pinged every `deadman_interval_s` (>= 10 s) only while the poll loop completed an iteration within max(30 s, 5 x poll interval). Failures are logged by type/HTTP status only, never with the URL. This is the only way to learn that the VM itself is down; strongly recommended in production.

## Open items
- Shelly model/generation: write both adapters; config choice until `http://SHELLY_IP/shelly` output is known.
- Operator's rate limit / permission for 1 s polling (email pending).
- Real lag between emergency and feed item: measure with `sherpa lag` first.
- Shelly authentication (Gen1 basic / Gen2 digest) is **not implemented**; assumes LAN with auth disabled.
- Adapters are verified only against mock Shellies written from the Shelly docs; verify with `sherpa test-event --duration 10` on real hardware.

## Known limitations
- Web sessions live in memory: restarting the web container logs everyone out.
- A silent failure of the *feed itself* (items stop being published) is undetectable; the heartbeat shows "last event" but an empty feed is legitimately normal.
- The self-test proves the pipeline, not that the real filter 52034 selects the right events.
