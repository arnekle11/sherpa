# SHERPA

Smart Home Emergency Response Platform for Alerting: new item in the SOS 112 feed -> Shelly relay (LED bar) ON for 5 minutes, with Telegram alerts when anything is wrong. See `docs/SPEC.md` and `CLAUDE.md`.

> The public feed must not be the only alert path for first responders.

## Deploy on the Ubuntu VM
```bash
git clone git@github.com:arnekle11/sherpa.git && cd sherpa
cp config.example.yaml config.yaml    # set device ip/generation; set db_path: /data/sherpa.db
cp .env.example .env                  # Telegram token + chat id
sudo timedatectl set-ntp true         # lag measurement and the startup age filter need a correct clock
```
Find your Shelly generation: `curl http://SHELLY_IP/shelly` (a `"gen":2`+ field = Gen2/Plus/Pro; absent = Gen1).

Preflight, then a short real test, then run:
```bash
docker compose build
docker compose run --rm sherpa check -c /app/config.yaml
docker compose run --rm sherpa test-event -c /app/config.yaml --duration 10 --yes
docker compose up -d
docker compose logs -f
```
Measure the real feed lag after some events: `docker compose exec sherpa sherpa lag -c /app/config.yaml`

## Develop
```bash
pip install -e '.[dev]' && pytest && sherpa selftest
```
Manual fake setup (three terminals): `sherpa mock-feed`, `sherpa mock-shelly --gen 2`, then point `config.yaml` at `http://127.0.0.1:8099/feed` and `127.0.0.1:8098`; inject with `curl -X POST 'http://127.0.0.1:8099/inject?title=Test'`.
