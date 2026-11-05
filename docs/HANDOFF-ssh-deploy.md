# Handoff: deploy and verify SHERPA on the Ubuntu VM over SSH

Paste this file (or point the new session at it) as the first message of a new Claude Code session that runs on a machine that can SSH to the VM (your Windows PC, or the VM itself).

## Context
SHERPA (Smart Home Emergency Response Platform for Alerting) polls the public SOS 112 RSS feed
`https://spin3.sos112.si/javno/ODApi/52034` (server-side filtered: every new item is a trigger) and
switches a Shelly relay (LED bar) ON with a device-side 300 s auto-off timer. Used by first responders:
reliability and visible failures matter more than features. The public feed must NOT be the only alert path.

- Repo: `arnekle11/sherpa` (private), branch `main`. Read `CLAUDE.md` and `docs/SPEC.md` first; keep the invariants in CLAUDE.md.
- The code was written in a cloud sandbox that could not reach the feed or the LAN. It is verified ONLY against mocks:
  20 pytest tests and `sherpa selftest` pass. **Never run against the real feed, a real Shelly, or Docker.**
  The Dockerfile/docker-compose.yml have never been built.

## Goal of the new session
Using SSH to the Ubuntu VM (Proxmox, fixed IP), get SHERPA running for real and verify each step.

1. Connect: ask the user for `user@VM_IP`. Prefer key auth. Confirm `ssh user@VM_IP hostname` works before anything else.
2. Install Docker Engine + compose plugin from Docker's official apt repo if missing (`docker compose version` to verify). Enable NTP (`timedatectl set-ntp true`) - the startup age filter and lag measurement need a correct clock.
3. Get the code onto the VM: `git clone` the private repo (needs a deploy key or token created by the user; never paste secrets into chat).
4. Ask the user for the output of `curl http://SHELLY_IP/shelly` to decide generation: `"gen": 2`+ => `generation: 2`, otherwise Gen1. Create `config/config.yaml` from `config.example.yaml` (set device `ip`, `generation`, and `db_path: /data/sherpa.db` for Docker).
5. `.env` with `SHERPA_TELEGRAM_TOKEN` and `SHERPA_TELEGRAM_CHAT_ID`: have the USER create/edit this file themselves; do not ask them to paste the token into the chat and do not print it.
6. `docker compose build` - fix any Docker build problems (first real build; expect small issues).
7. Read-only preflight: `docker compose run --rm sherpa check -c /config/config.yaml`. This fetches the real feed once and probes the Shelly without switching anything. Check: feed OK, self-link contains `OD/52034`, no PROBLEM lines. Report whether the real feed's structure matches the assumptions in docs/SPEC.md (fixtures are synthetic) and adapt `feed.py`/fixtures if not.
8. **Ask the user before switching hardware**, then `docker compose run --rm sherpa test-event -c /config/config.yaml --duration 10 --yes`. This really turns the LED ON for 10 s and sends a Telegram message. Confirm the Telegram message arrived and the LED turned itself off. If the Shelly adapter fails against the real device (the adapters are written from docs only), fix it and add a test.
9. `docker compose up -d`; check `docker compose logs`, `docker ps` health, and that the container restarts after `docker restart`/VM reboot (`restart: unless-stopped`). Also tick "Start at boot" for the VM in Proxmox.
10. After real events occur: `docker compose exec sherpa sherpa lag -c /config/config.yaml` to measure first-seen vs pubDate lag.

## Rules for this session
- Confirm with the user before anything outward-facing or hard to reverse: switching the LED, `docker compose down -v` (deletes the dedupe DB), reboots, editing firewall/SSH config, installing packages system-wide.
- Report faithfully: say what passed and what failed, with output.
- Commit fixes to `main` in small commits and push; no PR unless asked. Never commit `.env` or `config.yaml`.
- Don't hit the feed faster than 2 s polling; the operator's rate limit is unknown (the user is emailing them).

## Open items (unchanged)
- Shelly model/generation (step 4). Shelly authentication is not implemented (assumes auth off on the LAN).
- Operator's rate limit / whether 1 s polling is allowed.
- Real lag between emergency and feed item.
- A backup, independent alert path for responders.
