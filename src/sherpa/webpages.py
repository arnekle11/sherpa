"""HTML for the web UI. Every dynamic value goes through `_e`; feed text is untrusted."""
from __future__ import annotations

import html
import re
import time
from urllib.parse import urlencode
from datetime import UTC, datetime, timedelta

from . import BUILD, settings
from .store import LAG_SQL, lag_summary
from .webcharts import vbars
from .webstyle import CSS_VERSION, LOGO, icon

STALE_STATUS_S = 30
_e = html.escape

NAV = (("dashboard", "/", "Dashboard"), ("events", "/events", "Events"),
       ("alerts", "/alerts", "Alerts"), ("lights", "/lights", "Lights"), ("stats", "/stats", "Stats"),
       ("settings", "/settings", "Settings"))

ALERT_LABELS = {
    "feed-down": "Feed unreachable", "feed-structure": "Feed structure changed",
    "feed-link": "Feed filter changed", "internal": "Internal error", "dispatch": "Dispatch error",
    "config-invalid": "Settings file invalid", "clock-skew": "VM clock is wrong",
}
# What to do, per alert. Plain words for whoever is looking at the dashboard during a callout.
ALERT_HELP = {
    "feed-down": "The VM cannot get the SOS 112 feed. Check the VM's internet connection and whether "
                 "https://spin3.sos112.si opens in a browser. While this lasts, new emergencies will NOT switch the LED.",
    "feed-structure": "The feed answered with something SHERPA does not recognise (a format change or an error page). "
                      "Open the feed URL in a browser. If the format changed, the parser needs an update.",
    "feed-link": "The feed's self-link no longer contains the expected filter. The operator may have changed the filter, "
                 "so events could be missing or wrong. Compare the feed URL with the self-link canary under Settings.",
    "offline": "SHERPA cannot reach this Shelly. Check its power and Wi-Fi, and that its address under Settings is still "
               "right (a router may have given it a new IP). It will NOT light up on an emergency until this clears.",
    "activate": "The Shelly did not confirm ON with its auto-off timer. Check the device, then run a test event under "
                "Settings to confirm it works again.",
    "config-invalid": "config.yaml is invalid (edited by hand, or a setting an update no longer accepts). SHERPA keeps "
                      "running the last good settings, also after a restart. Settings → Restore last good settings, "
                      "or fix the file.",
    "internal": "Unexpected error in the poll loop. Look at `docker compose logs sherpa`; if it keeps happening, "
                "`docker compose restart sherpa`.",
    "dispatch": "Switching the devices crashed. Look at `docker compose logs sherpa`.",
    "clock-skew": "The VM clock disagrees with the feed server by more than a minute, so the startup age filter "
                  "(which decides what is too old to trigger) and the lag figures are wrong. On the VM: "
                  "`sudo timedatectl set-ntp true`, then check `timedatectl`.",
}


def rich(text: str) -> str:
    """Escape, then show `commands` as code (backticks survive html.escape, so this cannot inject markup)."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", _e(text))


def alert_help(key: str | None) -> str:
    if not key:
        return ""
    return ALERT_HELP.get(key.split(":", 1)[0], "")


STATE_BADGE = {
    "triggered": ("ok", "Delivered"), "partial": ("warn", "Partly delivered"), "failed": ("bad", "Failed"),
    "pending": ("warn", "Pending"), "abandoned": ("bad", "Abandoned"), "ignored_old": ("mute", "Ignored (old)"),
}
_EMOJI = re.compile(r"^[\U0001F000-\U0001FFFFℹ✅️\s]+")
_STILL = re.compile(r"^\(still\)\s*")


# ------------------------------------------------------------------ helpers
def _dt(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def ago(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 5:
        return "just now"
    if s < 60:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def dur(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d} d {h} h"
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min {s} s"
    return f"{s} s"


def when(iso: str | None) -> str:
    """Two-line time cell: local clock time plus relative age."""
    t = _dt(iso)
    if t is None:
        return '<span class="faint">-</span>'
    local = t.astimezone()
    rel = ago((datetime.now(UTC) - t).total_seconds())
    return f'{local.strftime("%d %b %H:%M:%S")}<small>{rel}</small>'


def clean(text: str) -> str:
    return _STILL.sub("", _EMOJI.sub("", text))


def alert_label(key: str | None) -> str:
    if not key:
        return ""
    if key.startswith("offline:"):
        return f"{key.split(':', 1)[1]} unreachable"
    if key.startswith("activate:"):
        return f"{key.split(':', 1)[1]} did not switch on"
    return ALERT_LABELS.get(key, key)


def badge(kind: str, text: str) -> str:
    return f'<span class="badge {kind}">{_e(text)}</span>'


def tile(k: str, v: str, s: str = "", cls: str = "") -> str:
    return (f'<div class="kpi"><div class="k">{_e(k)}</div><div class="v {cls}">{v}</div>'
            f'<div class="s">{s}</div></div>')


# ------------------------------------------------------------------ overview
def overview(w) -> dict:
    st = w.status()
    if st is None:
        return {"level": "unknown", "pill": "No data", "st": None, "alerts": []}
    age = time.time() - st["written"]
    if abs(age) > STALE_STATUS_S:  # also a timestamp far in the future (clock stepped back): not proof of life
        return {"level": "bad", "pill": "Service down", "st": st, "age": abs(age), "down": True, "alerts": []}
    lp = st.get("last_poll_age_s")
    if lp is not None and lp > poll_stall_limit(st):
        return {"level": "bad", "pill": "Polling stopped", "st": st, "stalled": lp, "alerts": st.get("active_alerts", [])}
    alerts = st.get("active_alerts", [])
    if alerts:
        n = len(alerts)
        return {"level": "bad", "pill": f"{n} alert{'s' if n != 1 else ''}", "st": st, "alerts": alerts}
    if not_notifying(st):
        return {"level": "warn", "pill": "Not notifying", "st": st, "alerts": []}
    return {"level": "ok", "pill": "Operational", "st": st, "alerts": []}


def poll_stall_limit(st: dict) -> float:
    return max(30.0, 3 * float(st.get("poll_interval_s", 2)) + 10)


def not_notifying(st: dict) -> str | None:
    """Why nobody would hear about a problem, or None."""
    if st.get("telegram_configured") is False:
        return "not_configured"
    if st.get("telegram_configured") and (st.get("telegram_delivery") or {}).get("ok") is False:
        return "failing"
    return None


def layout(w, title: str, body: str, active: str, user: bool, refresh: int = 0, ov: dict | None = None) -> str:
    ov = ov or overview(w)
    nav = "".join(f'<a href="{h}"{" aria-current=page" if k == active else ""}>{n}</a>' for k, h, n in NAV)
    who = ('<form method="post" action="/logout" class="inline"><button class="btn ghost sm" type="submit">Log out</button></form>'
           if user else '<a class="btn ghost sm" href="/login">Log in</a>')
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    # A tab left open on a wall screen shows the state in its title and icon.
    prefix = {"bad": f"● {ov['pill']} · ", "warn": f"▲ {ov['pill']} · "}.get(ov["level"], "")
    tz = datetime.now().astimezone().tzname() or "server time"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'{meta}<title>{_e(prefix)}{_e(title)} · SHERPA</title>'
        f'<link rel="icon" href="/favicon.svg?s={ov["level"]}" type="image/svg+xml">'
        f'<link rel="stylesheet" href="/static.css?v={CSS_VERSION}"></head><body>'
        '<a class="skip" href="#main">Skip to content</a>'
        '<div class="topbar"><div class="topbar-in">'
        f'<a class="brand" href="/">{LOGO}<span>SHERPA<small>SOS 112 alerting</small></span></a>'
        f'<nav class="nav" aria-label="Main">{nav}</nav>'
        f'<div class="top-right"><span class="pill {ov["level"]}" title="Service status"><i></i>{_e(ov["pill"])}</span>{who}</div>'
        f'</div></div><main class="wrap" id="main">{db_notice(w)}{body}</main>'
        f'<footer><span>SHERPA v{_e(BUILD)}</span><span>Times in {_e(tz)} (server time zone)</span>'
        '<a href="/help">Help: what the alerts mean</a>'
        '<span>The public feed must not be the only alert path.</span></footer></body></html>'
    )


def db_notice(w) -> str:
    err = getattr(w, "db_error", None)
    if not err:
        return ""
    return ('<div class="notice bad" role="alert">' + icon("alert") + '<div class="txt"><b>The database cannot be read</b> '
            f"({_e(err[:160])}). History and statistics are missing below; the live status still works. Check the "
            "VM's disk and <code>docker compose logs sherpa</code>.</div></div>")


def page_head(title: str, sub: str = "", right: str = "") -> str:
    p = f"<p>{sub}</p>" if sub else ""
    return f'<div class="page-head"><div><h1>{_e(title)}</h1>{p}</div>{right}</div>'


def empty(title: str, text: str) -> str:
    return f'<div class="empty"><b>{_e(title)}</b>{_e(text)}</div>'


# ------------------------------------------------------------------ dashboard
def _active_alert_items(w, keys: list[str]) -> str:
    latest: dict[str, str] = {}
    for r in w.query("SELECT key, text FROM alert_log WHERE key IS NOT NULL AND kind IN ('raise','repeat') "
                     "AND id IN (SELECT MAX(id) FROM alert_log WHERE key IS NOT NULL GROUP BY key)"):
        latest[r["key"]] = clean(r["text"])
    items = "".join(
        f'<li><b>{_e(alert_label(k))}</b><span>{_e(latest.get(k, "No details recorded."))}</span>'
        + (f'<span class="todo"><b>What to do:</b> {rich(alert_help(k))}</span>' if alert_help(k) else "")
        + "</li>" for k in keys)
    return f'<ul class="alert-list">{items}</ul>'


def hero(w, ov: dict) -> str:
    st = ov["st"]
    if st is None:
        return ('<section class="hero unknown">' + icon("unknown") + '<div class="body"><h2>No data from the service</h2>'
                "<p>The poller has not written a status yet. It may be starting, stopped, or not sharing the data volume.</p>"
                "</div></section>")
    if ov.get("down"):
        return ('<section class="hero bad">' + icon("down") + f'<div class="body"><h2>Service not responding</h2>'
                f'<p>Last status {ago(ov["age"])}. New emergencies will <b>not</b> switch the LED while the service is down.</p>'
                "</div></section>")
    if ov.get("stalled") is not None:
        return ('<section class="hero bad">' + icon("down") + '<div class="body"><h2>Polling has stopped</h2>'
                f'<p>The service is running but has not finished a feed poll for {dur(ov["stalled"])}. New emergencies '
                "will <b>not</b> switch the LED. Restart it: <code>docker compose restart sherpa</code>.</p>"
                + (_active_alert_items(w, ov["alerts"]) if ov["alerts"] else "") + "</div></section>")
    if ov["alerts"]:
        n = len(ov["alerts"])
        return ('<section class="hero bad">' + icon("alert") + f'<div class="body"><h2>{n} active alert{"s" if n != 1 else ""}</h2>'
                "<p>Something needs attention. The Alerts page has the full history.</p>"
                + _active_alert_items(w, ov["alerts"]) + "</div></section>")
    devs = list(st["devices"].values())
    if ov["level"] == "warn":
        why = not_notifying(st)
        tdel = st.get("telegram_delivery") or {}
        text = (("Telegram is not configured, so nobody hears about outages or failed devices. Put "
                 "<code>SHERPA_TELEGRAM_TOKEN</code> and <code>SHERPA_TELEGRAM_CHAT_ID</code> in <code>.env</code>, "
                 "then run <code>docker compose up -d</code>.") if why == "not_configured" else
                (f"Telegram messages are failing ({_e(str(tdel.get('error') or 'unknown error'))})"
                 + (f"; {n} message{'s' if n != 1 else ''} given up since the last start" if (n := tdel.get('undelivered', 0)) else "")
                 + ", so problems reach nobody. Check the bot token and chat id in <code>.env</code>."))
        return ('<section class="hero warn">' + icon("alert") + '<div class="body"><h2>Running, but nobody is notified</h2>'
                f"<p>Polling and the LEDs work. {text}</p></div></section>")
    checked = [d for d in devs if d["ok"] is not None]
    cfg = w.cfg()
    checks_off = cfg is not None and not cfg.alerts.device_check_interval_s
    dev_txt = (f'{sum(1 for d in checked if d["ok"])} of {len(devs)} devices reachable' if checked
               else ("automatic device checks are off" if checks_off else "devices not checked yet"))
    return ('<section class="hero ok">' + icon("ok") + '<div class="body"><h2>All systems operational</h2>'
            f'<p>Polling every {st["poll_interval_s"]:g} s · last good feed response {ago(st["feed_ok_age_s"])} · {dev_txt}</p>'
            "</div></section>")


def device_cards(w, st: dict | None) -> str:
    cfg = w.cfg()
    live = (st or {}).get("devices", {})
    cards = []
    for d in (cfg.devices if cfg else []):
        s = live.get(d.name, {"ok": None, "detail": "not checked yet", "checked": None})
        cls = "ok" if s["ok"] else ("unknown" if s["ok"] is None else "bad")
        chk = _dt(s.get("checked"))
        every = cfg.alerts.device_check_interval_s
        when_txt = (f"checked {ago((datetime.now(UTC) - chk).total_seconds())}" if chk else
                    (f"first check right after start, then every {every / 60:g} min" if every else "automatic checks are off"))
        cards.append(
            f'<div class="dev"><div class="dev-top"><h3>{_e(d.name)}</h3>{badge("mute", f"Gen{d.generation}")}</div>'
            f'<div class="sub">{_e(d.model or "Shelly")} · <span class="mono">{_e(d.ip)}</span> · auto-off {d.on_duration_s} s</div>'
            f'<div class="state {cls}"><i></i><span>{_e(s["detail"])} <span class="faint">· {_e(when_txt)}</span></span></div></div>')
    if cfg is None:
        return empty("Settings file invalid", "The device list cannot be shown; see Settings.")
    return "".join(cards) or empty("No devices configured", "Add a Shelly under Settings.")


def lag_chart(rows: list) -> str:
    """Bars of the most recent first-seen minus pubDate lags (seconds). Pure SVG, no script."""
    data = [(r["lag_s"], r["title"] or "", r["first_seen"]) for r in rows][::-1]
    W, H, L, B, T = 460, 160, 40, 22, 10  # sized for a half-width card
    top = next((n for n in (2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600) if n >= max(x[0] for x in data)), 7200)
    pw, ph = W - L - 8, H - B - T
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Feed lag per event in seconds">']
    for frac in (0, .5, 1):
        y = T + ph * (1 - frac)
        out.append(f'<line class="grid" x1="{L}" x2="{W - 8}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="ax" x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{top * frac:g}s</text>')
    slot = pw / max(len(data), 12)
    bw = slot * 0.66
    for i, (lag, title, seen) in enumerate(data):
        h = max(2.0, ph * min(lag, top) / top)
        x = L + i * slot + (slot - bw) / 2
        cls = "bad" if lag > 60 else ("warn" if lag > 30 else "")
        out.append(f'<rect class="bar {cls}" x="{x:.1f}" y="{T + ph - h:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="2">'
                   f'<title>{_e(title)}: {lag:.1f} s</title></rect>')
    out.append(f'<text class="ax" x="{L}" y="{H - 5}">older</text><text class="ax" x="{W - 8}" y="{H - 5}" text-anchor="end">newer</text></svg>')
    return "".join(out)


def lag_card(w) -> str:
    stats = lag_summary([r["lag_s"] for r in w.query(LAG_SQL)])
    head = '<div class="card-h"><h2>Feed lag</h2><span class="meta">first seen − feed pubDate</span></div>'
    if not stats:
        return f'<section class="card">{head}{empty("No data yet", "Lag appears after the first real event.")}</section>'
    rows = w.query("SELECT lag_s, title, first_seen FROM events WHERE is_test=0 AND state!='ignored_old' "
                   "AND lag_s IS NOT NULL ORDER BY first_seen DESC LIMIT 30")
    summary = (f'<div class="stats"><span>Events <b>{stats["n"]}</b></span><span>Min <b>{stats["min"]:.1f} s</b></span>'
               f'<span>Median <b>{stats["median"]:.1f} s</b></span><span>p95 <b>{stats["p95"]:.1f} s</b></span>'
               f'<span>Max <b>{stats["max"]:.1f} s</b></span></div>')
    return f'<section class="card">{head}<div class="card-b">{lag_chart(rows)}{summary}</div></section>'


def notices(st: dict, hero_covers_telegram: bool = False) -> str:
    out = []
    if hero_covers_telegram:
        pass
    elif not st.get("telegram_configured", True):
        out.append('<div class="notice warn">' + icon("alert") + '<div class="txt"><b>Telegram is not configured.</b> '
                   "Alerts are only logged and shown here; nobody is notified when something breaks. Put "
                   "<code>SHERPA_TELEGRAM_TOKEN</code> and <code>SHERPA_TELEGRAM_CHAT_ID</code> in <code>.env</code>, "
                   "then run <code>docker compose up -d</code>.</div></div>")
    tdel = st.get("telegram_delivery") or {}
    if st.get("telegram_configured") and tdel.get("ok") is False and not hero_covers_telegram:
        n = tdel.get("undelivered", 0)
        out.append('<div class="notice bad">' + icon("alert") + '<div class="txt"><b>Telegram messages are failing to send.</b> '
                   f'Alerts are NOT reaching you right now ({_e(str(tdel.get("error") or "unknown error"))})'
                   + (f"; {n} message{'s' if n != 1 else ''} given up since the last start" if n else "")
                   + ". Check the bot token and chat id in <code>.env</code>.</div></div>")
    dm = st.get("deadman") or {}
    if dm.get("configured") and dm.get("last_error"):
        out.append('<div class="notice warn">' + icon("alert") + '<div class="txt"><b>Dead-man\'s switch ping is failing</b> '
                   f'({_e(dm["last_error"])}). Your external monitor will raise an alarm soon if this continues.</div></div>')
    return "".join(out)


def poll_buckets(w, hours: int, bucket_min: int) -> tuple[datetime, list[dict]]:
    """poll_log aggregated into fixed buckets ending now (UTC)."""
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    n = hours * 60 // bucket_min
    current = now - timedelta(minutes=now.minute % bucket_min)  # buckets on clock boundaries, not "now minus x"
    start = current - timedelta(minutes=bucket_min * (n - 1))
    buckets = [{"ok": 0, "failed": 0, "lat_sum": 0.0, "lat_n": 0, "lat_max": 0.0, "minutes": 0, "gap": 0} for _ in range(n)]
    seen: list[datetime] = []
    for r in w.query("SELECT * FROM poll_log WHERE minute >= ? ORDER BY minute", (start.strftime("%Y-%m-%dT%H:%M"),)):
        try:
            t = datetime.strptime(r["minute"], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
        except ValueError:
            continue
        i = int((t - start).total_seconds() // (bucket_min * 60))
        if 0 <= i < n:
            b = buckets[i]
            for k in ("ok", "failed", "lat_sum", "lat_n"):
                b[k] += r[k]
            b["lat_max"] = max(b["lat_max"], r["lat_max"])
            b["minutes"] += 1
            if seen:  # minutes missing between two recorded ones = the service was not running
                b["gap"] += max(0, int((t - seen[-1]).total_seconds() // 60) - 1)
            seen.append(t)
    return start, buckets


def _avail_cls(v: float) -> str:
    return "ok" if v >= 99 else ("warn" if v >= 90 else "bad")  # one missed 2 s poll is retried 2 s later


def _lat_cls(v: float) -> str:
    return "bad" if v > 2500 else ("warn" if v > 1500 else "")


def feed_health_card(w, st: dict) -> str:
    bucket = 15
    start, b = poll_buckets(w, 24, bucket)
    avail = [100 * x["ok"] / (x["ok"] + x["failed"]) if x["ok"] + x["failed"] else None for x in b]
    lat = [1000 * x["lat_sum"] / x["lat_n"] if x["lat_n"] else None for x in b]
    noresp = [bool(x["minutes"]) and not x["lat_n"] for x in b]  # running, but the feed never answered
    stamps = [(start + timedelta(minutes=bucket * i)).astimezone() for i in range(len(b))]
    labels = [t.strftime("%H:%M") if t.minute == 0 and t.hour % 4 == 0 else "" for t in stamps]
    titles = [t.strftime("%H:%M") + "-" + (t + timedelta(minutes=bucket)).strftime("%H:%M") for t in stamps]
    ok, failed = sum(x["ok"] for x in b), sum(x["failed"] for x in b)
    with_data = [i for i, x in enumerate(b) if x["minutes"]]
    head = ('<div class="card-h"><h2>Feed health, last 24 h</h2><span class="meta">15-minute buckets · '
            'red = the feed did not answer · grey = no data (service not running)</span></div>')
    if not with_data:
        return f'<section class="card">{head}{empty("No history yet", "Poll statistics are written once a minute.")}</section>'
    covered_min = sum(x["minutes"] for x in b)
    span_min = sum(x["minutes"] for x in b) + sum(x["gap"] for x in b)
    lsum = st.get("latency") or {}
    summary = (f'<div class="stats"><span>Availability <b>{100 * ok / max(ok + failed, 1):.2f}%</b></span>'
               f'<span>Polls <b>{ok + failed:,}</b></span><span>Failed <b>{failed:,}</b></span>'
               f'<span>Running <b>{min(100.0, 100 * covered_min / max(span_min, 1)):.0f}%</b> of the time since first data</span></div>')
    lat_txt = (f'<div class="stats"><span>Latency now <b>{lsum["last_ms"]:.0f} ms</b></span><span>Median <b>{lsum["median_ms"]:.0f} ms</b></span>'
               f'<span>p95 <b>{lsum["p95_ms"]:.0f} ms</b></span><span>Max <b>{lsum["max_ms"]:.0f} ms</b></span>'
               f'<span class="faint">last {lsum["n"]} responses</span></div>') if lsum else ""
    return (f'<section class="card">{head}<div class="card-b">'
            '<h3 class="chart-h">Successful polls (%)</h3>'
            + vbars(avail, labels=labels, titles=titles, unit="%", top=100, cls=_avail_cls, rest="bad",
                    height=130, aria="Feed availability per 15 minutes")
            + summary + '<h3 class="chart-h">Average response time (ms)</h3>'
            + vbars(lat, labels=labels, titles=titles, unit="", cls=_lat_cls, height=110, failed=noresp,
                    aria="Average feed response time per 15 minutes")
            + lat_txt + "</div></section>")


def feed_now_card(st: dict) -> str:
    items = st.get("feed_items") or []
    at = st.get("feed_items_at")
    if "feed_items_at" in st and at is None:  # this service run has not parsed the feed yet
        return ('<section class="card"><div class="card-h"><h2>Currently in the feed</h2></div>'
                + empty("No successful poll yet", "The feed has not answered since the service started.") + "</section>")
    age = time.time() - at if at else 0
    stale = at is not None and age > max(30.0, 5 * st.get("poll_interval_s", 2))
    head = (f'<div class="card-h"><h2>Currently in the feed</h2><span class="meta">{len(items)} item{"s" if len(items) != 1 else ""} '
            f'{"as of " + ago(age) if stale else "at the last poll"}</span></div>')
    warn = (f'<div class="notice warn pad-notice">{icon("alert")}<div class="txt"><b>Out of date:</b> the feed has not '
            f"answered for {ago(age).replace(' ago', '')}. This is the last known content.</div></div>") if stale else ""
    if not items:
        quiet = (empty("The feed was empty", "That was the last known content; the feed is not answering now.") if stale
                 else empty("The feed is empty", "That is the normal, quiet state."))
        return f'<section class="card">{head}{warn}{quiet}</section>'
    now = datetime.now(UTC)
    rows = []
    for it in items[:12]:
        pub = _dt(it.get("pub"))
        rel = ago((now - pub).total_seconds()) if pub else "no date"
        rows.append(f'<li><span>{_e(it.get("title") or "(no title)")}</span><span class="faint nowrap">{rel}</span></li>')
    more = f'<p class="small mute pad">… and {len(items) - 12} more</p>' if len(items) > 12 else ""
    return f'<section class="card">{head}{warn}<ul class="feedlist">{"".join(rows)}</ul>{more}</section>'


def clock_cell(skew: float | None) -> str:
    if skew is None:
        return '<span class="faint">not checked yet</span>'
    if abs(skew) > 60:
        return badge("bad", "wrong") + f' <span class="mute">{abs(skew):.0f} s {"ahead" if skew > 0 else "behind"}</span>'
    return badge("ok", "in sync") + f' <span class="faint">{skew:+.1f} s vs the feed server</span>'


def alert_paths_card(w, st: dict) -> str:
    tg = st.get("telegram_configured")
    dm = st.get("deadman") or {}
    if not dm.get("configured"):
        dm_cell = badge("mute", "not configured") + ' <span class="faint">optional, recommended</span>'
    elif dm.get("last_error"):
        dm_cell = badge("bad", "failing") + f' <span class="mute">{_e(dm["last_error"])}</span>'
    elif dm.get("last_ok_age_s") is not None:
        dm_cell = badge("ok", "pinging") + f' <span class="mute">last ping {ago(dm["last_ok_age_s"])}</span>'
    else:
        dm_cell = badge("mute", "starting")
    cfg = w.cfg()
    n_dev = len(cfg.devices) if cfg else 0
    tdel = st.get("telegram_delivery") or {}
    if tg is None:  # status written by an older service version
        tg_cell = badge("mute", "unknown")
    elif not tg:
        tg_cell = badge("bad", "not configured")
    elif tdel.get("ok") is False:
        tg_cell = badge("bad", "failing") + f' <span class="mute">{_e(str(tdel.get("error") or "")[:80])}</span>'
    elif tdel.get("ok"):
        tg_cell = badge("ok", "delivering")
    else:
        tg_cell = badge("ok", "configured") + ' <span class="faint">nothing sent yet</span>'
    rows = [("Telegram", tg_cell),
            ("Dead-man's switch", dm_cell),
            ("LED devices", f"{n_dev} configured"),
            ("VM clock", clock_cell(st.get("clock_skew_s"))),
            ("Feed", f'polled every {st["poll_interval_s"]:g} s')]
    dl = "".join(f"<dt>{_e(k)}</dt><dd>{v}</dd>" for k, v in rows)
    return ('<section class="card"><div class="card-h"><h2>Alert paths</h2><span class="meta">how you find out</span></div>'
            f'<div class="card-b"><dl class="dl">{dl}</dl><p class="small mute">The public feed must not be the only '
            "alert path for responders. A dead-man's switch (e.g. healthchecks.io) catches a dead VM, which "
            "Telegram alerts from this service cannot.</p></div></section>")


def dashboard(w, user: bool, q: dict | None = None) -> str:
    ov = overview(w)
    st = ov["st"]
    paused = (q or {}).get("refresh") == "0"
    ctl = ('<span class="small mute">Auto-refresh paused · <a href="/">Resume</a></span>' if paused else
           '<span class="small mute">Refreshes every 10 s · <a href="/?refresh=0">Pause</a></span>')
    out = [page_head("Dashboard", "Live view of the poller and the Shelly devices.", ctl), hero(w, ov)]
    if st and not ov.get("down"):
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        stuck = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()  # dispatch finishes well within this
        r = w.query("SELECT COUNT(*) AS n, SUM(state IN ('failed','partial','abandoned') OR "
                    "(state='pending' AND first_seen < ?)) AS bad FROM events "
                    "WHERE is_test=0 AND state!='ignored_old' AND first_seen > ?", (stuck, cutoff))
        n, bad = (r[0]["n"], r[0]["bad"] or 0) if r else (0, 0)
        feed_age = st["feed_ok_age_s"]
        cfg = w.cfg()
        down_after = cfg.feed.down_after_s if cfg else 60
        out.append('<div class="kpis">'
                   + tile("Last good response", "just now" if feed_age < 5 else ago(feed_age),
                          f'polling every {st["poll_interval_s"]:g} s', "bad" if feed_age > down_after else "")
                   + tile("Polls OK", f'{st["polls_ok"]:,}', f'{st["polls_failed"]} failed · {st["polls_skipped"]} skipped (since start)')
                   + tile("Events, 24 h", str(n), (f'{bad} with delivery problems' if bad else ("all delivered" if n else "feed is quiet")),
                          "bad" if bad else "")
                   + tile("Uptime", dur(st["uptime_s"]), "since last start") + "</div>")
    if st is not None:
        out.insert(2, notices(st, hero_covers_telegram=ov["level"] == "warn"))
        out.append(feed_health_card(w, st))
        out.append('<section class="card"><div class="card-h"><h2>Shelly devices</h2>'
                   '<span class="meta">read-only checks, nothing is switched</span></div>'
                   f'<div class="devs">{device_cards(w, st)}</div></section>')
        out.append(f'<div class="cols">{feed_now_card(st)}{alert_paths_card(w, st)}</div>')
    out.append(f'<div class="cols">{lag_card(w)}'
               '<section class="card"><div class="card-h"><h2>Latest events</h2><a class="meta" href="/events">View all</a></div>'
               f'<div class="card-b flush">{events_table(w.query("SELECT * FROM events ORDER BY first_seen DESC LIMIT 6"), compact=True)}</div></section></div>')
    return layout(w, "Dashboard", "".join(out), "dashboard", user, refresh=0 if paused else 10, ov=ov)


# ------------------------------------------------------------------ events
def _title_cell(r) -> str:
    title = _e(r["title"] or "(no title)")
    link = r["link"] or ""
    if link.startswith("https://spin3.sos112.si/"):
        title = f'<a href="{_e(link)}" target="_blank" rel="noreferrer noopener">{title}</a>'
    test = " " + badge("info", "Test") if r["is_test"] else ""
    return title + test


def events_table(rows, compact: bool = False) -> str:
    if not rows:
        return empty("No events recorded", "An empty feed is the normal state.")
    body = []
    for r in rows:
        kind, label = STATE_BADGE.get(r["state"], ("mute", r["state"]))
        lag = f'{r["lag_s"]:.1f} s' if r["lag_s"] is not None else '<span class="faint">-</span>'
        det = "" if compact else f'<td class="det hide-sm">{_e(r["detail"] or "")}</td>'
        body.append(f'<tr><td class="t">{when(r["first_seen"])}</td><td>{_title_cell(r)}</td>'
                    f'<td class="t right hide-sm">{lag}</td><td>{badge(kind, label)}</td>{det}</tr>')
    det_h = "" if compact else '<th class="hide-sm">Detail</th>'
    return ('<div class="scroll"><table><thead><tr><th>First seen</th><th>Event</th><th class="right hide-sm">Lag</th>'
            f'<th>Result</th>{det_h}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


EVENT_FILTERS = {
    "all": ("All", "1=1"),
    "delivered": ("Delivered", "state='triggered' AND is_test=0"),
    "problems": ("Problems", "state IN ('failed','partial','abandoned','pending') AND is_test=0"),
    "ignored": ("Ignored at start", "state='ignored_old'"),
    "test": ("Tests", "is_test=1"),
}
ALERT_FILTERS = {
    "all": ("All", "1=1"),
    "raised": ("Raised", "kind IN ('raise','repeat')"),
    "cleared": ("Cleared", "kind='clear'"),
    "info": ("Info", "kind='info'"),
    "undelivered": ("Not delivered", "kind='undelivered'"),
}


def chips(base: str, options: dict, current: str) -> str:
    return '<div class="chips">' + "".join(
        f'<a class="chip" href="{base}{"" if k == "all" else "?f=" + k}" aria-current="{"true" if k == current else "false"}">{_e(v[0])}</a>'
        for k, v in options.items()) + "</div>"


PAGE = 100


def pager(path: str, f: str, before: str | None, rows: list, key: str) -> str:
    """'Older' link when the page is full; 'Newest' when not on the first page. Keyset paging, no OFFSET."""
    links = []
    if before:
        links.append(f'<a href="{path}{"?f=" + f if f != "all" else ""}">← Newest</a>')
    if len(rows) == PAGE:
        qs = urlencode({**({"f": f} if f != "all" else {}), "before": rows[-1][key]})
        links.append(f'<a href="{path}?{_e(qs)}">Older →</a>')
    return f'<div class="pager">{" ".join(links)}</div>' if links else ""


def events_page(w, user: bool, q: dict) -> str:
    f = q.get("f", "all")
    f = f if f in EVENT_FILTERS else "all"
    before = q.get("before") if _dt(q.get("before")) else None
    rows = w.query(f"SELECT * FROM events WHERE {EVENT_FILTERS[f][1]}" + (" AND first_seen < ?" if before else "")
                   + f" ORDER BY first_seen DESC LIMIT {PAGE}", (before,) if before else ())
    meta = f"{len(rows)} {'older ' if before else 'latest, '}newest first" if rows else ""
    body = (page_head("Events", "Every new item seen in the feed, and what happened to it.",
                      '<a class="btn ghost sm" href="/events.csv">Download CSV</a>') + chips("/events", EVENT_FILTERS, f)
            + f'<section class="card"><div class="card-h"><h2>{"All events" if f == "all" else _e(EVENT_FILTERS[f][0])}</h2>'
              f'<span class="meta">{meta}</span></div>'
              f'<div class="card-b flush">{events_table(rows)}</div>{pager("/events", f, before, rows, "first_seen")}</section>')
    return layout(w, "Events", body, "events", user)


# ------------------------------------------------------------------ alerts
def active_now_card(w) -> str:
    st = w.status() or {}
    keys = st.get("active_alerts") or []
    if not keys or time.time() - st.get("written", 0) > STALE_STATUS_S:
        return ""
    return ('<section class="card attention"><div class="card-h"><h2>Active now</h2>'
            f'<span class="meta">{len(keys)} condition{"s" if len(keys) != 1 else ""}</span></div>'
            f'<div class="card-b">{_active_alert_items(w, keys)}</div></section>')


def alerts_page(w, user: bool, q: dict) -> str:
    f = q.get("f", "all")
    f = f if f in ALERT_FILTERS else "all"
    before = q.get("before", "")
    before = int(before) if before.isdigit() else None
    rows = w.query(f"SELECT * FROM alert_log WHERE {ALERT_FILTERS[f][1]}" + (" AND id < ?" if before else "")
                   + f" ORDER BY id DESC LIMIT {PAGE}", (before,) if before else ())
    kinds = {"raise": ("bad", "Raised"), "repeat": ("warn", "Still active"), "clear": ("ok", "Cleared"), "info": ("info", "Info"),
             "undelivered": ("bad", "Not delivered")}
    if rows:
        body_rows = "".join(
            f'<tr><td class="t">{when(r["ts"])}</td><td>{badge(*kinds.get(r["kind"], ("mute", r["kind"])))}</td>'
            f'<td class="nowrap hide-sm">{_e(alert_label(r["key"]))}</td><td>{_e(clean(r["text"]))}</td></tr>' for r in rows)
        table = ('<div class="scroll"><table><thead><tr><th>Time</th><th>Kind</th><th class="hide-sm">Alert</th><th>Message</th>'
                 f'</tr></thead><tbody>{body_rows}</tbody></table></div>')
    else:
        table = empty("No alerts recorded", "Alerts appear here when something fails, recovers, or the service starts.")
    meta = f"{len(rows)} {'older ' if before else 'latest, '}newest first" if rows else ""
    body = (page_head("Alerts", "Alerts raised and cleared, plus start-up notices. Telegram gets the same messages when it is configured.")
            + (active_now_card(w) if not before else "") + chips("/alerts", ALERT_FILTERS, f)
            + f'<section class="card"><div class="card-h"><h2>{"All alerts" if f == "all" else _e(ALERT_FILTERS[f][0])}</h2>'
              f'<span class="meta">{meta}</span></div><div class="card-b flush">{table}</div>'
              f'{pager("/alerts", f, str(before) if before else None, rows, "id")}</section>')
    return layout(w, "Alerts", body, "alerts", user)


# ------------------------------------------------------------------ login
def login_page(w, error: str = "", disabled: bool = False, notice: bool = False) -> str:
    if disabled:
        inner = (f'{icon("lock")}<h1>Editing is disabled</h1><p>Set <code>SHERPA_WEB_PASSWORD</code> in '
                 "<code>.env</code> and restart the web service to enable login and settings changes.</p>")
    else:
        err = f'<div class="notice {"warn" if notice else "bad"}"><div class="txt">{_e(error)}</div></div>' if error else ""
        inner = (f'{LOGO}<h1>Log in</h1><p>Enter the admin password to view and change settings.</p>{err}'
                 '<form method="post" action="/login"><div class="field"><label for="p">Password</label>'
                 '<input id="p" name="password" type="password" autocomplete="current-password" autofocus></div>'
                 '<button class="btn" type="submit">Log in</button></form>')
    return layout(w, "Log in", f'<div class="login"><section class="card"><div class="card-b">{inner}</div></section></div>',
                  "", False)


# ------------------------------------------------------------------ settings
UNITS = {"poll_interval_s": "s", "max_age_min": "min", "down_after_s": "s", "repeat_min": "min",
         "heartbeat_min": "min", "device_check_interval_s": "s", "deadman_interval_s": "s"}
DEV_LABELS = {
    "name": ("Name", ""), "ip": ("Address", "IP or host, optionally :port"),
    "generation": ("Generation", "from http://IP/shelly: \"gen\": 2, 3 or 4 = Gen2 or newer"),
    "model": ("Model label", "free text"), "channel": ("Relay channel", "usually 0"),
    "component": ("Output type", "Switch for relays; RGB, RGBW or Light if /shelly shows \"profile\": \"rgb\" / \"light\""),
    "on_duration_s": ("On duration", "device-side auto-off timer"), "timeout_s": ("Timeout", "per attempt"),
    "attempts": ("Attempts", "tries including the first"), "retry_delay_s": ("Retry delay", "first back-off step"),
}
DEV_UNITS = {"on_duration_s": "s", "timeout_s": "s", "retry_delay_s": "s"}


def _field(fid: str, label: str, value, hint: str = "", unit: str = "", err: str | None = None,
           numeric: bool = True) -> str:
    bad = f' aria-invalid="true" aria-describedby="{_e(fid)}-err"' if err else ""
    mode = ' inputmode="decimal"' if numeric else ""  # a decimal keypad has no letters or ":" (names, addresses)
    inp = (f'<input type="text" id="{_e(fid)}" name="{_e(fid)}" value="{_e(str(value))}"{mode} '
           f'autocomplete="off"{bad}>')
    box = f'<div class="ig">{inp}<span class="u">{_e(unit)}</span></div>' if unit else inp
    h = f'<div class="hint">{_e(hint)}</div>' if hint else ""
    e = f'<div class="ferr" id="{_e(fid)}-err">{_e(err)}</div>' if err else ""
    return f'<div class="field{" invalid" if err else ""}"><label for="{_e(fid)}">{_e(label)}</label>{box}{e}{h}</div>'


def _device_card(i: int, d: dict, is_new: bool, live: dict | None, ferr: dict[str, str] | None = None) -> str:
    ferr = ferr or {}
    fields = []
    for k in settings.DEVICE_FIELDS:
        label, hint = DEV_LABELS[k]
        if k == "generation":
            opts = "".join(f'<option value="{g}"{" selected" if str(d[k]) == str(g) else ""}>{n}</option>'
                           for g, n in ((1, "Gen1"), (2, "Gen2 or newer")))
            fields.append(f'<div class="field"><label for="dev.{i}.generation">{label}</label>'
                          f'<select id="dev.{i}.generation" name="dev.{i}.generation">{opts}</select><div class="hint">{hint}</div></div>')
        elif k == "component":
            opts = "".join(f'<option value="{c}"{" selected" if str(d[k]) == c else ""}>{n}</option>'
                           for c, n in (("switch", "Switch (relay)"), ("rgb", "RGB"), ("rgbw", "RGBW"), ("light", "Light (dimmer)")))
            fields.append(f'<div class="field"><label for="dev.{i}.component">{label}</label>'
                          f'<select id="dev.{i}.component" name="dev.{i}.component">{opts}</select><div class="hint">{hint}</div></div>')
        else:
            fields.append(_field(f"dev.{i}.{k}", label, d[k], hint, DEV_UNITS.get(k, ""), ferr.get(f"dev.{i}.{k}"),
                                 numeric=settings.DEVICE_FIELDS[k] is not str))
    has_err = any(k.startswith(f"dev.{i}.") for k in ferr)
    if is_new:
        extra = f'<input type="hidden" name="dev.{i}.new" value="1">'
        opened = " open" if str(d.get("name", "")).strip() or has_err else ""
        head = f'<summary>{icon("plus")} Add a device <span class="mute small">leave the name empty to skip</span></summary>'
        cls = "devcard add"
    else:
        extra = (f'<label class="remove"><input type="checkbox" name="dev.{i}.delete"{" checked" if d.get("_del") else ""}> '
                 "Remove this device when saving</label>")
        opened = " open"
        state = ""
        if live:
            kind = "ok" if live.get("ok") else ("mute" if live.get("ok") is None else "bad")
            state = badge(kind, "reachable" if live.get("ok") else ("not checked" if live.get("ok") is None else "unreachable"))
        head = (f'<summary>{_e(str(d["name"]))} <span class="mute mono small">{_e(str(d["ip"]))}</span> '
                f'{badge("mute", "Gen" + str(d["generation"]))} {state}</summary>')
        cls = "devcard"
    return (f'<details class="{cls}"{opened}>{head}<div class="dc-b"><div class="fgrid">{"".join(fields)}</div>{extra}</div></details>')


def applied_note(w, digest: str) -> str:
    """Whether the running service uses exactly the file shown (its status carries the hash it started with)."""
    st = w.status() or {}
    if not st or time.time() - st.get("written", 0) > STALE_STATUS_S or "config_hash" not in st:
        return ""
    if st.get("config_hash") == digest:
        return f'<span class="small ok">{icon("ok")} The service runs exactly these settings</span>'
    return f'<span class="small warn">{icon("alert")} Saved, not applied yet: the service restarts within a few seconds</span>'


def tools_card(w, sess: dict, msg: tuple[str, str] | None = None) -> str:
    from .commands import TEST_EVENT_MAX_S
    csrf = f'<input type="hidden" name="csrf" value="{_e(sess["csrf"])}">'
    alive = w.service_alive()
    warn = "" if alive else '<div class="notice bad"><div class="txt">The service is not running; these tools need it.</div></div>'
    if msg:  # shown here, next to the buttons: the redirect lands on #tools
        warn = (f'<div class="notice {msg[0]}">' + icon("ok" if msg[0] == "ok" else "alert")
                + f'<div class="txt">{_e(msg[1])} <a href="/alerts">Open Alerts</a></div></div>') + warn
    return ('<section class="card" id="tools"><div class="card-h"><h2>Maintenance tools</h2>'
            '<span class="meta">run by the service; results appear under Alerts</span></div><div class="card-b">'
            f'{warn}<div class="tools">'
            '<div class="tool"><h3>Send a Telegram test</h3><p>Checks that alert messages actually reach your phone.</p>'
            f'<form method="post" action="/tools/telegram-test">{csrf}<button class="btn ghost" type="submit">Send test message</button></form></div>'
            '<div class="tool"><h3>Test event</h3><p>Runs the real dispatch path: <b>every device switches ON</b>, '
            "then turns itself off after the time below. Use it to verify the LED.</p>"
            f'<form method="post" action="/tools/test-event">{csrf}'
            f'<label class="check"><input type="checkbox" name="confirm" value="yes"> I understand this switches all LEDs on now.</label>'
            f'<div class="row">{_field("duration_s", "Duration", 10, f"1-{TEST_EVENT_MAX_S}", "s")}'
            f'<button class="btn danger" type="submit">{icon("bolt")} Run test event</button></div></form></div>'
            "</div></div></section>")


LIGHT_PRESETS = ((30, "30 s"), (60, "1 min"), (300, "5 min"), (900, "15 min"))


def lights_page(w, sess: dict | None, msg: tuple[str, str] | None = None) -> str:
    """Manual control of every light, made for a phone: big buttons, one tap. ON always carries the
    device-side timer; OFF is refused by the service while a real alert may be active."""
    if sess is None:
        return layout(w, "Lights", page_head("Lights") + '<section class="card"><div class="card-b">'
                      f'<p>{icon("lock")} Log in to control the lights.</p><p><a class="btn" href="/login">Log in</a></p>'
                      "</div></section>", "lights", False)
    cfg = w.cfg()
    csrf = f'<input type="hidden" name="csrf" value="{_e(sess["csrf"])}">'
    st = w.status() or {}
    alive = w.service_alive()
    notes = ""
    if msg:
        notes += (f'<div class="notice {msg[0]}">' + icon("ok" if msg[0] == "ok" else "alert")
                  + f'<div class="txt">{_e(msg[1])}</div></div>')
    if not alive:
        notes += '<div class="notice bad"><div class="txt">The service is not running, so the lights cannot be controlled from here.</div></div>'
    cards = []
    for d in (cfg.devices if cfg else []):
        live = (st.get("devices") or {}).get(d.name, {})
        reach = badge("ok", "reachable") if live.get("ok") else (badge("bad", "not reachable") if live.get("ok") is False
                                                                 else badge("mute", "not checked yet"))
        on_btns = "".join(f'<button class="btn big" type="submit" name="duration_s" value="{s}">{icon("bolt")} {lbl}</button>'
                          for s, lbl in LIGHT_PRESETS)
        cards.append(
            f'<section class="card light"><div class="card-h"><h2>{_e(d.name)}</h2>{reach}</div><div class="card-b">'
            f'<form method="post" action="/tools/light">{csrf}<input type="hidden" name="device" value="{_e(d.name)}">'
            f'<input type="hidden" name="action" value="on"><div class="lbl">Turn on for</div>'
            f'<div class="presets">{on_btns}</div></form>'
            f'<form method="post" action="/tools/light" class="offf">{csrf}<input type="hidden" name="device" value="{_e(d.name)}">'
            f'<input type="hidden" name="action" value="off"><button class="btn big ghost" type="submit">Turn off</button></form>'
            "</div></section>")
    rows = w.query("SELECT ts, text FROM alert_log WHERE text LIKE '%light control:%' ORDER BY id DESC LIMIT 8")
    recent = "".join(f'<li><div class="mute small">{when(r["ts"])}</div>{_e(_EMOJI.sub("", r["text"]))}</li>'
                     for r in rows) or '<li class="mute">Nothing yet.</li>'
    note = ("A real emergency overrides these buttons: it switches the lights on for their normal time. "
            "Manual 'on' always turns itself off.")
    return layout(w, "Lights", page_head("Lights", note) + notes + '<div class="lights">' + "".join(cards) + "</div>"
                  '<section class="card"><div class="card-h"><h2>Recent light actions</h2></div><div class="card-b">'
                  f'<ul class="plain">{recent}</ul></div></section>', "lights", True)


def settings_page(w, sess: dict | None, errors: list[str] | None = None, saved: bool = False,
                  form: dict[str, str] | None = None, tool_msg: tuple[str, str] | None = None) -> str:
    if sess is None:
        body = (page_head("Settings") + '<section class="card"><div class="card-b">'
                f'<p>{icon("lock")} Log in to view and change settings.</p>'
                '<p><a class="btn" href="/login">Log in</a></p></div></section>')
        return layout(w, "Settings", body, "settings", False)
    cfg = w.cfg()
    if cfg is None:
        err = w.config_error() or "unknown error"
        restore = (f'<form method="post" action="/settings/restore"><input type="hidden" name="csrf" value="{_e(sess["csrf"])}">'
                   '<button class="btn" type="submit">Restore last good settings</button></form>'
                   '<p class="hint">The service saves a copy each time it starts with valid settings; the broken file is kept '
                   "as <code>config.yaml.broken</code>.</p>") if w.can_restore() else (
                   "<p>No last-good copy exists yet. Fix <code>config/config.yaml</code> on the server.</p>")
        msg = (f'<div class="notice {tool_msg[0]}"><div class="txt">{_e(tool_msg[1])}</div></div>' if tool_msg else "")
        return layout(w, "Settings", page_head("Settings") + msg + '<section class="card attention"><div class="card-h">'
                      "<h2>The settings file is invalid</h2></div><div class=\"card-b\">"
                      f"<p>SHERPA keeps running the previous settings. Error:</p><pre class=\"err-text\">{_e(err)}</pre>"
                      f"{restore}</div></section>", "settings", True)
    raw = cfg.model_dump()  # effective values: keys omitted from the file show their defaults
    digest = settings.config_hash(w.config_path)
    f = form if form is not None else {}
    out = [page_head("Settings", "Changes are validated, saved to the config file, and applied by an automatic service restart.",
                     applied_note(w, digest))]
    if tool_msg and tool_msg[1].startswith("The last good settings"):
        out.append(f'<div class="notice ok">{icon("ok")}<div class="txt">{_e(tool_msg[1])}</div></div>')
    if saved:
        out.append('<div class="notice ok">' + icon("ok") + '<div class="txt"><b>Saved.</b> The service restarts itself within a few '
                   "seconds to apply the change; Telegram will report the restart.</div></div>")
    ferr: dict[str, str] = {}
    for x in errors or []:
        if getattr(x, "field", None):
            ferr.setdefault(x.field, str(x))
    if errors:
        n = len(errors)
        out.append('<div class="notice bad" role="alert">' + icon("alert") + f'<div class="txt"><b>Not saved: {n} '
                   f'problem{"s" if n != 1 else ""}</b> (marked in red below)<ul>'
                   + "".join(f"<li>{_e(x)}</li>" for x in errors) + "</ul></div></div>")

    def section(title: str, sub: str, key: str, spec: dict) -> str:
        cur = raw[key]
        fields = "".join(_field(f"{key}.{k}", label, f.get(f"{key}.{k}", cur[k]), hint, UNITS.get(k, ""), ferr.get(f"{key}.{k}"))
                         for k, (_t, label, hint) in spec.items())
        return (f'<section class="card"><div class="card-h"><h2>{title}</h2><span class="meta">{sub}</span></div>'
                f'<div class="card-b"><div class="fgrid">{fields}</div></div></section>')

    out.append(f'<form method="post" action="/settings"><input type="hidden" name="csrf" value="{_e(sess["csrf"])}">'
               f'<input type="hidden" name="base" value="{_e(digest)}">')
    out.append(section("Feed polling", "how often and how strictly the feed is read", "feed", settings.FEED_FIELDS))
    out.append(section("Alerting and checks", "Telegram timing and automatic checks", "alerts", settings.ALERT_FIELDS))

    devs = [dict(settings.DEVICE_DEFAULTS, **d.model_dump()) for d in cfg.devices]
    if form is not None:  # re-render what the user typed, under the same row numbers (errors refer to them)
        idxs = sorted({int(k.split(".")[1]) for k in form if k.startswith("dev.") and k.split(".")[1].isdigit()})
        rows = [(i, {k: f.get(f"dev.{i}.{k}", "") for k in settings.DEVICE_FIELDS}
                 | {"_del": f.get(f"dev.{i}.delete"), "_new": f.get(f"dev.{i}.new")}) for i in idxs]
    else:
        devs.append(dict(settings.DEVICE_DEFAULTS, _new="1"))
        rows = list(enumerate(devs))
    live = (w.status() or {}).get("devices", {})
    cards = "".join(_device_card(i, d, bool(d.get("_new")), live.get(str(d.get("name"))), ferr) for i, d in rows)
    out.append('<section class="card"><div class="card-h"><h2>Shelly devices</h2>'
               '<span class="meta">on duration is a device-side timer and survives a service crash</span></div>'
               f'{cards}<div class="card-b"></div></section>')
    out.append(f'<section class="card"><div class="card-h"><h2>Fixed settings</h2><span class="meta">not editable here</span></div>'
               f'<div class="card-b small"><p>Feed URL: <code>{_e(cfg.feed.url)}</code></p>'
               f'<p>Self-link canary: <code>{_e(cfg.feed.expected_link_contains or "off")}</code></p>'
               "<p class=\"mute\">Telegram token and chat id live only in <code>.env</code> on the server. "
               "Saving rewrites <code>config.yaml</code> (comments are dropped); the previous file is kept as <code>config.yaml.bak</code>.</p>"
               "</div></section>")
    out.append('<div class="savebar"><button class="btn" type="submit">Save settings</button>'
               '<a class="btn ghost" href="/settings">Discard changes</a>'
               '<span class="mute">Applying restarts the poller for about a second; the device timers keep running.</span></div></form>')
    out.append(tools_card(w, sess, tool_msg))  # after </form>: it has its own forms, and HTML forms cannot be nested
    return layout(w, "Settings", "".join(out), "settings", True)


def help_page(w, user: bool) -> str:
    rows = "".join(
        f'<tr><td class="nowrap"><b>{_e(label)}</b></td><td>{rich(ALERT_HELP[key])}</td></tr>'
        for key, label in (("feed-down", "Feed unreachable"), ("feed-structure", "Feed structure changed"),
                           ("feed-link", "Feed filter changed"), ("offline", "<device> unreachable"),
                           ("activate", "<device> did not switch on"), ("config-invalid", "Settings file invalid"),
                           ("internal", "Internal error"), ("dispatch", "Dispatch error"),
                           ("clock-skew", "VM clock is wrong")))
    states = "".join(f'<tr><td>{badge(k, v)}</td><td>{_e(t)}</td></tr>' for k, v, t in (
        ("ok", "Delivered", "every device confirmed ON with its auto-off timer"),
        ("warn", "Partly delivered", "some devices confirmed, others did not (see the detail column)"),
        ("bad", "Failed", "no device confirmed ON"),
        ("mute", "Ignored (old)", "already in the feed when SHERPA started and older than the startup max-age"),
        ("bad", "Abandoned", "recorded but not dispatched before a restart and then too old, or an interrupted test")))
    body = (page_head("Help", "What SHERPA shows, and what to do when something is wrong.")
            + '<section class="card"><div class="card-h"><h2>How it works</h2></div><div class="card-b help">'
              "<p>SHERPA polls the public SOS 112 feed every few seconds. Every item that appears in it is an emergency "
              "for this area: SHERPA switches every Shelly ON with an auto-off timer kept by the device itself, so the "
              "LED turns off on its own even if SHERPA stops. Problems are sent to Telegram and shown here.</p>"
              "<p><b>The public feed must not be the only alert path.</b> If it is late or down, SHERPA cannot know about "
              "an emergency.</p></div></section>"
            + '<section class="card"><div class="card-h"><h2>Alerts and what to do</h2></div><div class="card-b flush">'
              f'<div class="scroll"><table><tbody>{rows}</tbody></table></div></div></section>'
            + '<section class="card"><div class="card-h"><h2>Event results</h2></div><div class="card-b flush">'
              f'<div class="scroll"><table><tbody>{states}</tbody></table></div></div></section>'
            + '<section class="card"><div class="card-h"><h2>Checking everything end to end</h2></div><div class="card-b help"><ol>'
              "<li>Settings → Maintenance tools → <b>Send test message</b>: the message must arrive in Telegram.</li>"
              "<li>Settings → Maintenance tools → <b>Run test event</b> (10 s): every LED must light and turn itself off.</li>"
              "<li>Dashboard: every device <b>reachable</b>, feed health green, no active alerts.</li>"
              "<li>With a dead-man's switch configured: its monitor must show recent pings.</li></ol></div></section>")
    return layout(w, "Help", body, "", user)


def not_found(w, user: bool) -> str:
    return layout(w, "Not found", page_head("Page not found") + '<section class="card"><div class="card-b">'
                  '<a href="/">Back to the dashboard</a></div></section>', "", user)
