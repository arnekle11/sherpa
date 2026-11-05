# Handoff (historical): first deployment over SSH

This file briefed the first deployment session (November 2026). That deployment is done; the current
procedures are in `README.md` (install, update) and `docs/RUNBOOK.md` (operation, backup, troubleshooting).

## State of the deployment (as of 2026-11-06)
- Ubuntu 24.04 VM on Proxmox (`sherpa`), Docker Compose with two containers: `sherpa` and `sherpa-web`.
- Repo cloned with a deploy key at `~/sherpa`; config in `~/sherpa/config/config.yaml`; data in the Docker volume
  `sherpa_sherpa-data`.
- Verified against the real feed: fetch, self-link canary (`OD/52034`), empty-feed handling, web UI settings
  save + automatic service restart.

## Still open
- Telegram token / chat id in `.env` (until then alerts reach nobody; the dashboard says so).
- `SHERPA_DEADMAN_URL` (dead-man's switch) in `.env`.
- The real Shelly: replace the placeholder device (`192.0.2.1`), run `check`, then a 10 s test event.
- Proxmox "Start at boot" and a reboot test.
- Operator: rate limit / whether 1 s polling is allowed; real feed lag (`sherpa lag` after real events).
- A backup, independent alert path for responders.

## Rules that still apply to anyone working on the deployment
- Ask the owner before switching LEDs, rebooting, deleting volumes or changing system-wide config.
- Never ask for secrets in chat; the owner edits `.env` and creates keys.
- Commit fixes to `main` in small commits; no PR unless asked. Never commit `.env` or `config/`.
- Never poll the feed faster than every 2 s.
