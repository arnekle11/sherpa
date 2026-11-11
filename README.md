# SHERPA

Smart Home Emergency Response Platform for Alerting: a new item in the SOS 112 feed switches Shelly relays
(LED bars) ON with a device-side auto-off timer, and Telegram says when anything is wrong.
Design and decisions: `docs/SPEC.md`. Operating it: **`docs/RUNBOOK.md`**. Working on the code: `CLAUDE.md`.

> The public feed must not be the only alert path for first responders.

## Install on the Ubuntu VM
```bash
git clone git@github.com:arnekle11/sherpa.git && cd sherpa
mkdir config && cp config.example.yaml config/config.yaml   # set each device's ip and generation
cp .env.example .env && chmod 600 .env                     # Telegram token + chat id, web password, dead-man URL
sudo timedatectl set-ntp true                              # the startup age filter needs a correct clock
```
Colour, effects and phone control of the lights: web UI → Lights (see docs/RUNBOOK.md 5.2a).

Output type: if `/shelly` shows `"profile": "rgb"` (e.g. Plus RGBW PM) set `component: rgb` on the device.
On such outputs a real alert blinks red until you press Turn off on the Lights page (`alert_effect: blink`, the default; `solid` = plain 300 s light). The blink renews the full device timer and never goes dark, so a dead SHERPA leaves it lit until that timer ends.

Shelly generation: open `http://SHELLY_IP/shelly`. A `"gen": 2`, `3` or `4` field means `generation: 2`
(same RPC API); no `gen` field means Gen1. Shelly authentication must be off.

Build, preflight, test, run:
```bash
docker compose build                                                   # also runs the self-test
docker compose run --rm sherpa check          # feed, clock, database, devices (read-only)
docker compose up -d
docker compose exec sherpa sherpa test-event --duration 10 --yes   # every LED on for 10 s
docker compose exec sherpa sherpa status
```
Then open `http://VM_IP:8080`, log in, and use **Settings → Maintenance tools → Send test message**.
In Proxmox, enable *Start at boot* for the VM. Strongly recommended: a dead-man's switch (below).

## What runs
- `sherpa` container: polls the feed (default every 2 s), dedupes by guid in SQLite, switches every Shelly,
  alerts via Telegram, writes status for the web UI. `restart: always`.
- `sherpa-web` container (port 8080, plain HTTP: LAN only; `SHERPA_WEB_BIND` limits it to one interface):
  - **Dashboard**: status in plain words with *what to do*, feed health over 24 h, devices, what is in the feed,
    alert paths (Telegram, dead-man's switch, VM clock), lag, latest events. The tab title and icon show the state.
  - **Events / Alerts / Stats**: history with filters and paging; per-day/type/hour statistics; CSV export.
  - **Settings** (password): poll timing, alerting, devices, with safe bounds. Saving restarts the service, which
    reports what changed. **Maintenance tools**: Telegram test, test event (refused while a real alert may be lit).
  - **Help**: every alert explained. `/api/status`, `/metrics` (Prometheus), `/events.csv`, `/healthz`.

## Commands (inside the container: `docker compose exec sherpa sherpa ...`)
| Command | What it does |
|---|---|
| `status` | one-screen summary (exit 0 = all good) |
| `check` | read-only preflight: feed, clock, database, every Shelly |
| `test-event --duration 10 --yes` | real dispatch with a short timer; goes through the running service |
| `lag` | first-seen vs pubDate statistics |
| `backup --out /data/backup.db` | consistent database copy while running |
| `selftest` | whole pipeline against local mocks |

## Dead-man's switch (recommended)
SHERPA's own alerts cannot tell you that the VM died. Create a check at e.g. healthchecks.io (period 1 min,
grace 3 min), put its ping URL in `.env` as `SHERPA_DEADMAN_URL`, and `docker compose up -d`. SHERPA pings only
while polls succeed, so a hung or crashing service stops the pings too. The URL is never logged or shown.

## Update
```bash
git pull && SHERPA_REV=$(git rev-parse --short HEAD) docker compose build && docker compose up -d
```
Details, rollback, backup and restore: `docs/RUNBOOK.md`. Never `docker compose down -v` (deletes the database).

## Develop
```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest && .venv/bin/sherpa selftest
```
Manual fake setup (three terminals): `sherpa mock-feed`, `sherpa mock-shelly --gen 2`, then point a config at
`http://127.0.0.1:8099/feed` and `127.0.0.1:8098` with `db_path: data/sherpa.db`; inject an event with
`curl -X POST 'http://127.0.0.1:8099/inject?title=Test'`. Dependencies for the image are pinned in
`requirements.lock`; change them deliberately and rebuild (the build runs the self-test).
