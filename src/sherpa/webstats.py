"""Statistics page: what the feed produced, how fast, and how reliably SHERPA delivered it."""
from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta

from .webcharts import hbars, vbars
from .webpages import STATE_BADGE, _avail_cls, _dt, _e, empty, layout, page_head, tile

PERIODS = {"7": ("7 days", 7), "30": ("30 days", 30), "60": ("60 days", 60)}
_TYPE = re.compile(r"^(.*?)\s+\d{1,2}\.\s*\d{1,2}\.\s*\d{4}")
LAG_BUCKETS = (("< 2 s", 2), ("2-5 s", 5), ("5-10 s", 10), ("10-30 s", 30), ("30-60 s", 60), ("1-5 min", 300), ("> 5 min", float("inf")))


def event_type(title: str) -> str:
    """'Prometna nesreča 5. 11. 2026 20:41' -> 'Prometna nesreča' (the feed puts date and time last)."""
    m = _TYPE.match(title or "")
    t = (m[1] if m else (title or "")).strip().rstrip(",")
    return t or "(no title)"


def _card(title: str, meta: str, body: str) -> str:
    return (f'<section class="card"><div class="card-h"><h2>{_e(title)}</h2><span class="meta">{_e(meta)}</span></div>'
            f'<div class="card-b">{body}</div></section>')


def stats_page(w, user: bool, q: dict) -> str:
    p = q.get("days", "30")
    p = p if p in PERIODS else "30"
    label, days = PERIODS[p]
    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=days)).isoformat()
    rows = w.query("SELECT title, first_seen, lag_s, state FROM events WHERE is_test=0 AND state!='ignored_old' "
                   "AND first_seen > ? ORDER BY first_seen", (cutoff,))
    period_chips = '<div class="chips">' + "".join(
        f'<a class="chip" href="/stats?days={k}" aria-current="{"true" if k == p else "false"}">{_e(v[0])}</a>'
        for k, v in PERIODS.items()) + "</div>"
    out = [page_head("Statistics", "Events from the feed and how SHERPA handled them. Test events and items "
                                   "ignored at startup are left out."), period_chips]

    n = len(rows)
    delivered = sum(r["state"] == "triggered" for r in rows)
    lags = [r["lag_s"] for r in rows if r["lag_s"] is not None]
    today = now.astimezone().date()
    day_list = [today - timedelta(days=days - 1 - i) for i in range(days)]
    polls_by_day: dict = {}
    for r in w.query("SELECT minute, ok, failed FROM poll_log WHERE minute >= ?", (cutoff[:16],)):
        try:
            d = datetime.strptime(r["minute"], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).astimezone().date()
        except ValueError:
            continue
        acc = polls_by_day.setdefault(d, [0, 0])
        acc[0] += r["ok"]; acc[1] += r["failed"]
    ok = sum(v[0] for v in polls_by_day.values())
    failed = sum(v[1] for v in polls_by_day.values())
    out.append('<div class="kpis">'
               + tile("Events", str(n), f"in the last {label}")
               + tile("Delivered", f"{100 * delivered / n:.0f}%" if n else "-",
                      f"{n - delivered} with problems" if n - delivered else ("every event switched every device" if n else "no events yet"),
                      "bad" if n - delivered else ("ok" if n else ""))
               + tile("Median lag", f"{statistics.median(lags):.1f} s" if lags else "-", "first seen minus feed pubDate")
               + tile("Feed availability", f"{100 * ok / (ok + failed):.2f}%" if ok + failed else "-", f"{ok + failed:,} polls")
               + "</div>")
    if not rows and not ok + failed:
        out.append('<section class="card">' + empty("Nothing to show yet", "Statistics fill in as SHERPA runs and the feed produces events.") + "</section>")
        return layout(w, "Statistics", "".join(out), "stats", user)

    # events per day (local dates)
    per_day = Counter(_dt(r["first_seen"]).astimezone().date() for r in rows if _dt(r["first_seen"]))
    every = 1 if days <= 7 else (5 if days <= 30 else 10)
    out.append(_card("Events per day", f"last {label}, local dates",
                     vbars([per_day.get(d, 0) for d in day_list], labels=[d.strftime("%d.%m") for d in day_list],
                           label_every=every, titles=[d.strftime("%a %d %b") for d in day_list], aria="Events per day")
                     if rows else empty("No events", "The feed was quiet in this period.")))

    by_type = Counter(event_type(r["title"]) for r in rows).most_common(10)
    per_hour = Counter(_dt(r["first_seen"]).astimezone().hour for r in rows if _dt(r["first_seen"]))
    out.append('<div class="cols">'
               + _card("By event type", "top 10", hbars(by_type, width=440, label_w=195, aria="Events by type") if by_type else empty("No events", ""))
               + _card("By hour of day", "local time",
                       vbars([per_hour.get(h, 0) for h in range(24)], labels=[f"{h:02d}" for h in range(24)], label_every=3,
                             titles=[f"{h:02d}:00-{h:02d}:59" for h in range(24)], width=440, height=170,
                             aria="Events by hour of day")
                       if rows else empty("No events", ""))
               + "</div>")

    lag_counts = [0] * len(LAG_BUCKETS)
    for v in lags:
        lag_counts[next(i for i, (_, hi) in enumerate(LAG_BUCKETS) if v < hi)] += 1
    avail = [100 * polls_by_day[d][0] / sum(polls_by_day[d]) if sum(polls_by_day.get(d, [0, 0])) else None for d in day_list]
    out.append('<div class="cols">'
               + _card("Feed lag", "first seen minus feed pubDate",
                       vbars(lag_counts, labels=[x[0] for x in LAG_BUCKETS], titles=[x[0] for x in LAG_BUCKETS], width=440, height=170,
                             aria="Lag distribution")
                       if lags else empty("No lag data", ""))
               + _card("Feed availability per day", "successful polls; grey = no data",
                       vbars(avail, labels=[d.strftime("%d.%m") for d in day_list], label_every=every, unit="%", top=100,
                             cls=_avail_cls, titles=[d.strftime("%a %d %b") for d in day_list], width=440, height=170,
                             aria="Feed availability per day"))
               + "</div>")

    states = Counter(r["state"] for r in rows)
    if states:
        out.append(_card("Delivery results", "per event",
                         hbars([(STATE_BADGE.get(k, ("", k))[1], v) for k, v in states.most_common()], width=680, label_w=160,
                               aria="Delivery results")))
    return layout(w, "Statistics", "".join(out), "stats", user)

