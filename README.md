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
`docker compose up -d` also starts `sherpa-web` on port 8080 (`http://VM_IP:8080`): live status, devices, latest events with feed lag, alert history, and a settings page.
Anyone on the LAN can view; changing settings needs `SHERPA_WEB_PASSWORD` from `.env` (empty = read-only). It is plain HTTP, so keep it on the LAN.
Editable: poll interval (minimum 2 s), max age, feed-down time, alert timing, and the Shelly device list. Not editable: feed URL, DB path, Telegram secrets.
Saving rewrites `config/config.yaml` (previous copy kept as `config.yaml.bak`); the service notices within ~5 s and restarts itself to apply it.

## Develop
```bash
pip install -e '.[dev]' && pytest && sherpa selftest
```
Manual fake setup (three terminals): `sherpa mock-feed`, `sherpa mock-shelly --gen 2`, then point `config.yaml` at `http://127.0.0.1:8099/feed` and `127.0.0.1:8098`; inject with `curl -X POST 'http://127.0.0.1:8099/inject?title=Test'`.
