"""HTML for the web UI. Every dynamic value goes through `_e`; feed text is untrusted."""
from __future__ import annotations

import html
import re
import time
from datetime import UTC, datetime, timedelta

from . import __version__, settings
from .store import LAG_SQL, lag_summary
from .webcharts import vbars
from .webstyle import CSS_VERSION, LOGO, icon

STALE_STATUS_S = 30
_e = html.escape

NAV = (("dashboard", "/", "Dashboard"), ("events", "/events", "Events"),
       ("alerts", "/alerts", "Alerts"), ("stats", "/stats", "Stats"), ("settings", "/settings", "Settings"))

ALERT_LABELS = {
    "feed-down": "Feed unreachable", "feed-structure": "Feed structure changed",
    "feed-link": "Feed filter changed", "internal": "Internal error", "dispatch": "Dispatch error",
    "config-invalid": "Settings file invalid",
}
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
    if age > STALE_STATUS_S:
        return {"level": "bad", "pill": "Service down", "st": st, "age": age, "down": True, "alerts": []}
    alerts = st.get("active_alerts", [])
    if alerts:
        n = len(alerts)
        return {"level": "bad", "pill": f"{n} alert{'s' if n != 1 else ''}", "st": st, "alerts": alerts}
    return {"level": "ok", "pill": "Operational", "st": st, "alerts": []}


def layout(w, title: str, body: str, active: str, user: bool, refresh: int = 0, ov: dict | None = None) -> str:
    ov = ov or overview(w)
    nav = "".join(f'<a href="{h}"{" aria-current=page" if k == active else ""}>{n}</a>' for k, h, n in NAV)
    who = ('<form method="post" action="/logout" class="inline"><button class="btn ghost sm" type="submit">Log out</button></form>'
           if user else '<a class="btn ghost sm" href="/login">Log in</a>')
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'{meta}<title>{_e(title)} · SHERPA</title>'
        f'<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="/static.css?v={CSS_VERSION}"></head><body>'
        '<div class="topbar"><div class="topbar-in">'
        f'<a class="brand" href="/">{LOGO}<span>SHERPA<small>SOS 112 alerting</small></span></a>'
        f'<nav class="nav" aria-label="Main">{nav}</nav>'
        f'<div class="top-right"><span class="pill {ov["level"]}" title="Service status"><i></i>{_e(ov["pill"])}</span>{who}</div>'
        f'</div></div><main class="wrap">{body}</main>'
        f'<footer><span>SHERPA v{_e(__version__)}</span><span>Times shown in the server time zone</span>'
        '<span>The public feed must not be the only alert path.</span></footer></body></html>'
    )


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
        f'<li><b>{_e(alert_label(k))}</b><span>{_e(latest.get(k, "No details recorded."))}</span></li>' for k in keys)
    return f'<ul class="alert-list">{items}</ul>'


def hero(w, ov: dict) -> str:
    st = ov["st"]
    if st is None:
        return ('<section class="hero unknown">' + icon("unknown") + '<div class="body"><h1>No data from the service</h1>'
                "<p>The poller has not written a status yet. It may be starting, stopped, or not sharing the data volume.</p>"
                "</div></section>")
    if ov.get("down"):
        return ('<section class="hero bad">' + icon("down") + f'<div class="body"><h1>Service not responding</h1>'
                f'<p>Last status {ago(ov["age"])}. New emergencies will <b>not</b> switch the LED while the service is down.</p>'
                "</div></section>")
    if ov["alerts"]:
        n = len(ov["alerts"])
        return ('<section class="hero bad">' + icon("alert") + f'<div class="body"><h1>{n} active alert{"s" if n != 1 else ""}</h1>'
                "<p>Something needs attention. The Alerts page has the full history.</p>"
                + _active_alert_items(w, ov["alerts"]) + "</div></section>")
    devs = list(st["devices"].values())
    checked = [d for d in devs if d["ok"] is not None]
    dev_txt = (f'{sum(1 for d in checked if d["ok"])} of {len(devs)} devices reachable' if checked
               else "devices not checked yet")
    return ('<section class="hero ok">' + icon("ok") + '<div class="body"><h1>All systems operational</h1>'
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
        when_txt = f"checked {ago((datetime.now(UTC) - chk).total_seconds())}" if chk else "checked every " + (
            f"{cfg.alerts.device_check_interval_s / 60:g} min" if cfg.alerts.device_check_interval_s else "never")
        cards.append(
            f'<div class="dev"><div class="dev-top"><h3>{_e(d.name)}</h3>{badge("mute", f"Gen{d.generation}")}</div>'
            f'<div class="sub">{_e(d.model or "Shelly")} · <span class="mono">{_e(d.ip)}</span> · auto-off {d.on_duration_s} s</div>'
            f'<div class="state {cls}"><i></i><span>{_e(s["detail"])} <span class="faint">· {_e(when_txt)}</span></span></div></div>')
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


def notices(st: dict) -> str:
    out = []
    if not st.get("telegram_configured", True):
        out.append('<div class="notice warn">' + icon("alert") + '<div class="txt"><b>Telegram is not configured.</b> '
                   "Alerts are only logged and shown here; nobody is notified when something breaks. Put "
                   "<code>SHERPA_TELEGRAM_TOKEN</code> and <code>SHERPA_TELEGRAM_CHAT_ID</code> in <code>.env</code>, "
                   "then run <code>docker compose up -d</code>.</div></div>")
    tdel = st.get("telegram_delivery") or {}
    if st.get("telegram_configured") and tdel.get("ok") is False:
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
    start = now - timedelta(hours=hours) + timedelta(minutes=1)
    n = hours * 60 // bucket_min
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
    stamps = [(start + timedelta(minutes=bucket * i)).astimezone() for i in range(len(b))]
    labels = [t.strftime("%H:%M") for t in stamps]
    titles = [t.strftime("%H:%M") + "-" + (t + timedelta(minutes=bucket)).strftime("%H:%M") for t in stamps]
    ok, failed = sum(x["ok"] for x in b), sum(x["failed"] for x in b)
    with_data = [i for i, x in enumerate(b) if x["minutes"]]
    head = '<div class="card-h"><h2>Feed health, last 24 h</h2><span class="meta">15-minute buckets · grey = no data (service not running)</span></div>'
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
            + vbars(avail, labels=labels, label_every=16, titles=titles, unit="%", top=100, cls=_avail_cls,
                    height=130, aria="Feed availability per 15 minutes")
            + summary + '<h3 class="chart-h">Average response time (ms)</h3>'
            + vbars(lat, labels=labels, label_every=16, titles=titles, unit="", cls=_lat_cls, height=110,
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
        tg_cell = badge("bad", "failing") + f' <span class="mute">{_e(str(tdel.get("error") or ""))[:80]}</span>'
    elif tdel.get("ok"):
        tg_cell = badge("ok", "delivering")
    else:
        tg_cell = badge("ok", "configured") + ' <span class="faint">nothing sent yet</span>'
    rows = [("Telegram", tg_cell),
            ("Dead-man's switch", dm_cell),
            ("LED devices", f"{n_dev} configured"),
            ("Feed", f'polled every {st["poll_interval_s"]:g} s')]
    dl = "".join(f"<dt>{_e(k)}</dt><dd>{v}</dd>" for k, v in rows)
    return ('<section class="card"><div class="card-h"><h2>Alert paths</h2><span class="meta">how you find out</span></div>'
            f'<div class="card-b"><dl class="dl">{dl}</dl><p class="small mute">The public feed must not be the only '
            "alert path for responders. A dead-man's switch (e.g. healthchecks.io) catches a dead VM, which "
            "Telegram alerts from this service cannot.</p></div></section>")


def dashboard(w, user: bool) -> str:
    ov = overview(w)
    st = ov["st"]
    out = [page_head("Dashboard", "Live view of the poller and the Shelly devices.",
                     '<span class="small mute">Refreshes every 10 s</span>'), hero(w, ov)]
    if st and not ov.get("down"):
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        r = w.query("SELECT COUNT(*) AS n, SUM(state IN ('failed','partial','abandoned')) AS bad FROM events "
                    "WHERE is_test=0 AND state!='ignored_old' AND first_seen > ?", (cutoff,))
        n, bad = (r[0]["n"], r[0]["bad"] or 0) if r else (0, 0)
        feed_age = st["feed_ok_age_s"]
        out.append('<div class="kpis">'
                   + tile("Last good response", f"{feed_age:.0f} s", f'polling every {st["poll_interval_s"]:g} s',
                          "bad" if feed_age > 60 else "")
                   + tile("Polls OK", f'{st["polls_ok"]:,}', f'{st["polls_failed"]} failed · {st["polls_skipped"]} skipped (since start)')
                   + tile("Events, 24 h", str(n), (f'{bad} with delivery problems' if bad else ("all delivered" if n else "feed is quiet")),
                          "bad" if bad else "")
                   + tile("Uptime", dur(st["uptime_s"]), "since last start") + "</div>")
    if st is not None:
        out.insert(2, notices(st))
        out.append(feed_health_card(w, st))
        out.append('<section class="card"><div class="card-h"><h2>Shelly devices</h2>'
                   '<span class="meta">read-only checks, nothing is switched</span></div>'
                   f'<div class="devs">{device_cards(w, st)}</div></section>')
        out.append(f'<div class="cols">{feed_now_card(st)}{alert_paths_card(w, st)}</div>')
    out.append(f'<div class="cols">{lag_card(w)}'
               '<section class="card"><div class="card-h"><h2>Latest events</h2><a class="meta" href="/events">View all</a></div>'
               f'<div class="card-b flush">{events_table(w.query("SELECT * FROM events ORDER BY first_seen DESC LIMIT 6"), compact=True)}</div></section></div>')
    return layout(w, "Dashboard", "".join(out), "dashboard", user, refresh=10, ov=ov)


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


def events_page(w, user: bool, q: dict) -> str:
    f = q.get("f", "all")
    f = f if f in EVENT_FILTERS else "all"
    rows = w.query(f"SELECT * FROM events WHERE {EVENT_FILTERS[f][1]} ORDER BY first_seen DESC LIMIT 200")
    body = (page_head("Events", "Every new item seen in the feed, and what happened to it.") + chips("/events", EVENT_FILTERS, f)
            + f'<section class="card"><div class="card-h"><h2>{"All events" if f == "all" else _e(EVENT_FILTERS[f][0])}</h2>'
              f'<span class="meta">latest {len(rows)}, newest first</span></div>'
              f'<div class="card-b flush">{events_table(rows)}</div></section>')
    return layout(w, "Events", body, "events", user)


# ------------------------------------------------------------------ alerts
def alerts_page(w, user: bool, q: dict) -> str:
    f = q.get("f", "all")
    f = f if f in ALERT_FILTERS else "all"
    rows = w.query(f"SELECT * FROM alert_log WHERE {ALERT_FILTERS[f][1]} ORDER BY id DESC LIMIT 200")
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
    body = (page_head("Alerts", "Alerts raised and cleared, plus start-up notices. Telegram gets the same messages when it is configured.")
            + chips("/alerts", ALERT_FILTERS, f)
            + f'<section class="card"><div class="card-h"><h2>{"All alerts" if f == "all" else _e(ALERT_FILTERS[f][0])}</h2>'
              f'<span class="meta">latest {len(rows)}, newest first</span></div><div class="card-b flush">{table}</div></section>')
    return layout(w, "Alerts", body, "alerts", user)


# ------------------------------------------------------------------ login
def login_page(w, error: str = "", disabled: bool = False) -> str:
    if disabled:
        inner = (f'{icon("lock")}<h1>Editing is disabled</h1><p>Set <code>SHERPA_WEB_PASSWORD</code> in '
                 "<code>.env</code> and restart the web service to enable login and settings changes.</p>")
    else:
        err = f'<div class="notice bad"><div class="txt">{_e(error)}</div></div>' if error else ""
        inner = (f'{LOGO}<h1>Log in</h1><p>Enter the admin password to view and change settings.</p>{err}'
                 '<form method="post" action="/login"><div class="field"><label for="p">Password</label>'
                 '<input id="p" name="password" type="password" autocomplete="current-password" autofocus></div>'
                 '<button class="btn" type="submit">Log in</button></form>')
    return layout(w, "Log in", f'<div class="login"><section class="card"><div class="card-b">{inner}</div></section></div>',
                  "", False)


# ------------------------------------------------------------------ settings
UNITS = {"poll_interval_s": "s", "max_age_min": "min", "down_after_s": "s", "repeat_min": "min",
         "heartbeat_min": "min", "device_check_interval_s": "s"}
DEV_LABELS = {
    "name": ("Name", ""), "ip": ("Address", "IP or host, optionally :port"), "generation": ("Generation", "Gen1 or Gen2+"),
    "model": ("Model label", "free text"), "channel": ("Relay channel", "usually 0"),
    "on_duration_s": ("On duration", "device-side auto-off timer"), "timeout_s": ("Timeout", "per attempt"),
    "attempts": ("Attempts", "tries including the first"), "retry_delay_s": ("Retry delay", "first back-off step"),
}
DEV_UNITS = {"on_duration_s": "s", "timeout_s": "s", "retry_delay_s": "s"}


def _field(fid: str, label: str, value, hint: str = "", unit: str = "") -> str:
    inp = f'<input type="text" id="{_e(fid)}" name="{_e(fid)}" value="{_e(str(value))}" inputmode="decimal" autocomplete="off">'
    box = f'<div class="ig">{inp}<span class="u">{_e(unit)}</span></div>' if unit else inp
    h = f'<div class="hint">{_e(hint)}</div>' if hint else ""
    return f'<div class="field"><label for="{_e(fid)}">{_e(label)}</label>{box}{h}</div>'


def _device_card(i: int, d: dict, is_new: bool, live: dict | None) -> str:
    fields = []
    for k in settings.DEVICE_FIELDS:
        label, hint = DEV_LABELS[k]
        if k == "generation":
            opts = "".join(f'<option value="{g}"{" selected" if str(d[k]) == str(g) else ""}>Gen{g}</option>' for g in (1, 2))
            fields.append(f'<div class="field"><label for="dev.{i}.generation">{label}</label>'
                          f'<select id="dev.{i}.generation" name="dev.{i}.generation">{opts}</select><div class="hint">{hint}</div></div>')
        else:
            fields.append(_field(f"dev.{i}.{k}", label, d[k], hint, DEV_UNITS.get(k, "")))
    if is_new:
        extra = f'<input type="hidden" name="dev.{i}.new" value="1">'
        opened = " open" if str(d.get("name", "")).strip() else ""
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


def settings_page(w, sess: dict | None, errors: list[str] | None = None, saved: bool = False,
                  form: dict[str, str] | None = None, tool_msg: tuple[str, str] | None = None) -> str:
    if sess is None:
        body = (page_head("Settings") + '<section class="card"><div class="card-b">'
                f'<p>{icon("lock")} Log in to view and change settings.</p>'
                '<p><a class="btn" href="/login">Log in</a></p></div></section>')
        return layout(w, "Settings", body, "settings", False)
    cfg = w.cfg()
    if cfg is None:
        return layout(w, "Settings", page_head("Settings") + '<div class="notice bad"><div class="txt">'
                      "Cannot read or validate the config file. Fix it on the server.</div></div>", "settings", True)
    raw = cfg.model_dump()  # effective values: keys omitted from the file show their defaults
    digest = settings.config_hash(w.config_path)
    f = form if form is not None else {}
    out = [page_head("Settings", "Changes are validated, saved to the config file, and applied by an automatic service restart.")]
    if saved:
        out.append('<div class="notice ok">' + icon("ok") + '<div class="txt"><b>Saved.</b> The service restarts itself within a few '
                   "seconds to apply the change; Telegram will report the restart.</div></div>")
    if errors:
        out.append('<div class="notice bad">' + icon("alert") + '<div class="txt"><b>Not saved</b><ul>'
                   + "".join(f"<li>{_e(x)}</li>" for x in errors) + "</ul></div></div>")

    def section(title: str, sub: str, key: str, spec: dict) -> str:
        cur = raw[key]
        fields = "".join(_field(f"{key}.{k}", label, f.get(f"{key}.{k}", cur[k]), hint, UNITS.get(k, ""))
                         for k, (_t, label, hint) in spec.items())
        return (f'<section class="card"><div class="card-h"><h2>{title}</h2><span class="meta">{sub}</span></div>'
                f'<div class="card-b"><div class="fgrid">{fields}</div></div></section>')

    out.append(f'<form method="post" action="/settings"><input type="hidden" name="csrf" value="{_e(sess["csrf"])}">'
               f'<input type="hidden" name="base" value="{_e(digest)}">')
    out.append(section("Feed polling", "how often and how strictly the feed is read", "feed", settings.FEED_FIELDS))
    out.append(section("Alerting", "Telegram message timing", "alerts", settings.ALERT_FIELDS))

    devs = [dict(settings.DEVICE_DEFAULTS, **d.model_dump()) for d in cfg.devices]
    if form is not None:  # re-render what the user typed
        idxs = sorted({int(k.split(".")[1]) for k in form if k.startswith("dev.") and k.split(".")[1].isdigit()})
        devs = [{k: f.get(f"dev.{i}.{k}", "") for k in settings.DEVICE_FIELDS} | {"_del": f.get(f"dev.{i}.delete"), "_new": f.get(f"dev.{i}.new")}
                for i in idxs]
    else:
        devs.append(dict(settings.DEVICE_DEFAULTS, _new="1"))
    live = (w.status() or {}).get("devices", {})
    cards = "".join(_device_card(i, d, bool(d.get("_new")), live.get(str(d.get("name")))) for i, d in enumerate(devs))
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


def not_found(w, user: bool) -> str:
    return layout(w, "Not found", page_head("Page not found") + '<section class="card"><div class="card-b">'
                  '<a href="/">Back to the dashboard</a></div></section>', "", user)
