# SHERPA

Smart Home Emergency Response Platform for Alerting: new item in the SOS 112 feed -> Shelly relay (LED bar) ON for 5 minutes, with Telegram alerts when anything is wrong. See `docs/SPEC.md` and `CLAUDE.md`.

> The public feed must not be the only alert path for first responders.

## Deploy on the Ubuntu VM
```bash
git clone git@github.com:arnekle11/sherpa.git && cd sherpa
mkdir config && cp config.example.yaml config/config.yaml   # set device ip/generation; set db_path: /data/sherpa.db
cp .env.example .env                  # Telegram token + chat id + web password
sudo timedatectl set-ntp true         # lag measurement and the startup age filter need a correct clock
```
Find your Shelly generation: `curl http://SHELLY_IP/shelly` (a `"gen":2`+ field = Gen2/Plus/Pro; absent = Gen1).

Preflight, then a short real test, then run:
```bash
docker compose build
docker compose run --rm sherpa check -c /config/config.yaml
docker compose run --rm sherpa test-event -c /config/config.yaml --duration 10 --yes
docker compose up -d
docker compose logs -f
```
Measure the real feed lag after some events: `docker compose exec sherpa sherpa lag -c /config/config.yaml`

## Web UI
`docker compose up -d` also starts `sherpa-web` on port 8080 (`http://VM_IP:8080`). Plain HTTP: keep it on the LAN.
Anyone on the LAN can view; changing settings and the maintenance tools need `SHERPA_WEB_PASSWORD` from `.env` (empty = read-only).

- **Dashboard** - status, active alerts in plain words, feed availability/latency over 24 h (gaps where the service was down stay visible), Shelly reachability (checked 3 s after start, then every `device_check_interval_s`), what is in the feed right now, alert paths, feed lag, latest events. Warns when Telegram is not configured.
- **Events / Alerts** - full history with filters. **Stats** - events per day/type/hour, lag distribution, daily feed availability, delivery results.
- **Settings** - poll interval (minimum 2 s), max age, feed-down time, alert timing, dead-man ping interval and the Shelly list. Saving rewrites `config/config.yaml` (previous copy kept as `config.yaml.bak`); the service notices within ~5 s, restarts itself, and reports what changed.
- **Maintenance tools** (Settings page) - send a Telegram test message; run a test event (switches every device ON for 1-60 s, needs a confirmation tick, at most one per 30 s). The web process only queues the request; the service runs it.
- **Machine-readable** - `/events.csv`, `/api/status` (JSON), `/metrics` (Prometheus), `/healthz`.

## Dead-man's switch (recommended)
Telegram alerts come from SHERPA itself, so a dead VM is silent. Create a check at e.g. healthchecks.io (period 1 min, grace 3 min), put its ping URL in `.env` as `SHERPA_DEADMAN_URL`, and `docker compose up -d`. SHERPA pings only while its poll loop is healthy, so a hung loop also stops the pings. The URL is never logged or shown.

## Develop
```bash
pip install -e '.[dev]' && pytest && sherpa selftest
```
Manual fake setup (three terminals): `sherpa mock-feed`, `sherpa mock-shelly --gen 2`, then point `config.yaml` at `http://127.0.0.1:8099/feed` and `127.0.0.1:8098`; inject with `curl -X POST 'http://127.0.0.1:8099/inject?title=Test'`.
