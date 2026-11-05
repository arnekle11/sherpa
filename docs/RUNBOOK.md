# SHERPA operator runbook

For whoever looks after the SHERPA VM. No programming needed. All commands run on the VM
(`ssh` in first) inside the `sherpa` folder: `cd ~/sherpa`.

> **SHERPA is a convenience, not the alarm.** It depends on a public information feed that can be
> late or down, and it cannot notice if the feed silently stops publishing or the filter selects the wrong
> events. Responders must keep their primary alerting.

## 1. Is it working? (10 seconds)

Open `http://VM_IP:8080`. The coloured box at the top says it all:

| Box | Meaning | Do |
|---|---|---|
| Green **All systems operational** | Feed polled, devices reachable, Telegram set up | Nothing |
| Amber **Running, but nobody is notified** | LEDs work, but problems reach nobody (Telegram missing or failing) | Fix Telegram (section 4) |
| Red **N active alerts** | Something is broken; each alert says *what to do* | Follow the text under each alert |
| Red **Polling has stopped** / **Service not responding** | SHERPA cannot react to emergencies | `docker compose restart sherpa`, then section 3 |
| Grey **No data from the service** | The service has not started or the data volume is wrong | `docker compose up -d`, then `docker compose logs sherpa` |

The browser tab shows the same: its icon dot turns red or amber, and the tab title starts with ● or ▲.
From a terminal: `docker compose exec sherpa sherpa status` (exit code 0 = all good).
If the box is amber or red, the text under it says what to do; section 2 lists every alert.

## 2. Alerts and what to do

The dashboard shows these with instructions; the **Help** link at the bottom of every page has the full list.

| Alert | What it means | What to do |
|---|---|---|
| Feed unreachable | The VM cannot get the SOS 112 feed | Check the VM's internet; open https://spin3.sos112.si in a browser. LEDs will **not** light meanwhile. |
| Feed structure changed | The feed sent something unexpected | Open the feed URL in a browser; a format change needs a software update. |
| Feed filter changed | The feed's self-link lost the expected filter | Events may be missing/wrong; check the feed URL. |
| *device* unreachable | SHERPA cannot reach a Shelly | Power, Wi-Fi, and its IP address (see 5.3). |
| *device* did not switch on | A Shelly did not confirm ON with its timer | Check the device, then run a test event (section 6). |
| Settings file invalid | `config/config.yaml` was edited by hand and is broken | Fix it, or save the settings again in the web UI. Old settings stay active. |
| Internal / dispatch error | Unexpected software error | `docker compose logs sherpa`; restart if it repeats. |
| VM clock is wrong | The clock differs from the feed server by > 1 min | `sudo timedatectl set-ntp true`; `timedatectl` must say *synchronized: yes*. |
| Cannot write the database | Disk full or filesystem read-only | `df -h`; LEDs still switch, but fix it soon. |
| *device* stuck ON / re-armed | A relay was ON without its timer (power cut or switched by hand) | SHERPA re-arms it; if re-arming failed, switch it off by hand. |
| Telegram messages are failing | Alerts cannot be sent | Check token and chat id in `.env` (section 4). |
| Dead-man's switch ping is failing | The external monitor will raise an alarm | Check the VM's internet; check the URL in `.env`. |
| **SHERPA cannot run** (Telegram) | The service crashes at start and Docker keeps retrying | `docker compose logs --tail 100 sherpa`; nothing is monitored meanwhile. |
| **Dead-man alarm** (from your monitor) | No ping: VM, Docker or SHERPA is down | Proxmox console; `docker compose ps`; `docker compose logs --tail 200 sherpa`; `docker compose up -d`. |

Every alert is also sent to Telegram once, repeated every 30 minutes while it lasts, and followed by a
"✅ ... again" message when it clears.

## 3. Everyday commands

```bash
docker compose ps                         # both containers "Up (healthy)"?
docker compose logs --since 1h sherpa     # what the service did
docker compose logs -f web                # logins, settings changes, maintenance tools
docker compose exec sherpa sherpa status  # one-screen summary
docker compose restart sherpa             # restart the service (a few seconds, device timers keep running)
docker compose up -d                      # start, and apply .env changes (restart does NOT re-read .env)
timedatectl                               # "System clock synchronized: yes"
```
Never run SHERPA's commands with `sudo`, and never edit files inside the containers.

## 4. Telegram, passwords and other secrets

All secrets live in `~/sherpa/.env` (never in the web UI, never in git). Edit it with `nano .env`, then
`docker compose up -d` (a `restart` does not pick up `.env` changes). Keep it private: `chmod 600 .env`.
Put values that contain `$` in single quotes.

- `SHERPA_TELEGRAM_TOKEN` / `SHERPA_TELEGRAM_CHAT_ID`: create a bot with @BotFather, send it a message,
  read the chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
- `SHERPA_WEB_PASSWORD`: needed to change settings. Empty = the web UI is read-only.
- `SHERPA_DEADMAN_URL` (recommended): see section 7.
- `SHERPA_WEB_BIND` (optional): the VM's LAN IP, so the web UI listens only there. Docker-published ports
  bypass `ufw`.

A daily "still alive" message is reassuring: Settings → Heartbeat = 720 (minutes).

After changing Telegram: Settings → Maintenance tools → **Send test message**.

## 5. Changing things

### 5.1 Settings
Web UI → **Settings** (log in). Values are checked before saving; problems are marked in red at the field.
Saving restarts the service by itself within ~5 s and the restart message lists what changed.

### 5.2 Adding a Shelly
1. Give it a fixed IP (DHCP reservation in the router).
2. Find its generation: open `http://SHELLY_IP/shelly` in a browser; `"gen": 2` or higher means Gen2.
3. Settings → **Add a device**: name, address, generation. Save.
4. Run a test event (section 6). Shelly authentication must be **off** (not supported yet).

### 5.3 A Shelly got a new IP address
Fix the address under Settings → the device, save, then run a test event. Prevent it with a DHCP reservation.

### 5.4 Editing `config/config.yaml` by hand
Possible, but check it afterwards: `docker compose run --rm sherpa check`. If the file is invalid, SHERPA keeps
running (and restarts with) the last good copy and alerts until it is fixed; Settings offers **Restore last good
settings**.

## 6. End-to-end check (monthly, and after any change)

1. Settings → Maintenance tools → **Send test message** → it arrives in Telegram.
2. Settings → Maintenance tools → tick the box → **Run test event** (10 s) → every LED lights and turns
   itself off. A test is refused while a real alert may still be lighting the LED; that is on purpose.
3. Dashboard green; every device "reachable".
4. Dead-man's switch monitor shows recent pings.

## 7. Dead-man's switch (strongly recommended)

SHERPA's own alerts cannot tell you that the VM itself died. Create a free check at e.g. healthchecks.io
(period 1 minute, grace 3 minutes, notifications to your phone), put its ping URL into `.env` as
`SHERPA_DEADMAN_URL=...`, and `docker compose up -d`. If pings stop, that service alerts you.

## 8. Updating SHERPA (and going back)

```bash
cd ~/sherpa
docker tag sherpa:local sherpa:prev                         # keep the running version for a rollback
git pull
SHERPA_REV=$(git rev-parse --short HEAD) docker compose build   # the build runs the self-test
docker compose run --rm sherpa check
docker compose up -d
docker compose exec sherpa sherpa status
```
Then run the end-to-end check (section 6). Rollback: `docker tag sherpa:prev sherpa:local && docker compose up -d`.
Old images: `docker image prune`.

## 9. Backup and restore

The database holds the list of emergencies already seen (so none triggers twice) and the history.

```bash
# backup (safe while running)
docker compose exec sherpa sherpa backup --out /data/backup.db
docker compose cp sherpa:/data/backup.db ./sherpa-backup-$(date +%F).db
docker compose exec sherpa rm /data/backup.db

# restore: stop BOTH containers first, and remove the old files before copying the backup in
# (an old -wal file next to a new database would corrupt it)
docker compose stop
docker compose run --rm --no-deps --entrypoint sh sherpa -c 'rm -f /data/sherpa.db /data/sherpa.db-wal /data/sherpa.db-shm'
docker compose cp ./sherpa-backup-YYYY-MM-DD.db sherpa:/data/sherpa.db
docker compose start
```
Losing the database is not dangerous: on start SHERPA ignores items older than the startup max-age.

**Never run `docker compose down -v`**: `-v` deletes the database volume.

## 10. After a VM reboot or power cut

Both containers start by themselves (`restart: always`; in Proxmox the VM must have *Start at boot*).
Within about two minutes Telegram gets a "started" message. Check `timedatectl` (clock synchronized) and the
dashboard. Events that appeared while SHERPA was down and are younger than the startup max-age (default 10 min)
are triggered when it comes back; older ones are recorded but do not light the LED.
