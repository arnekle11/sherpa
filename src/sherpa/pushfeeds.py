"""Extra feeds that only notify a phone through Pushover (no relay, no light).

Same polling discipline as the main feed (keep-alive, hard timeout, strict parse, dedupe by guid, ignore stale
items on the first poll), but a separate failure domain: an error here is reported through the normal alerts and
can never touch the main poll loop or the devices. Items are remembered in their own table, so they do not
count as "a real event" for the light-safety windows.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx

from .config import FeedCfg, PushFeedCfg
from .feed import FeedFetcher, FeedFormatError, FeedItem, FetchError, parse_feed

log = logging.getLogger("sherpa.push")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
FAIL_AFTER = 3          # consecutive bad polls/sends before an alert (one odd response is noise)
LOCATION_WAIT_S = 120   # a restricted item whose location cannot be read yet is re-checked this long, then dropped
GIVE_UP_AFTER = timedelta(hours=2)   # an item that still cannot be delivered after this long is dropped, loudly


def _norm(text: str) -> str:
    """Lower case without diacritics, single spaces: "Tehnična  pomoč" == "tehnicna pomoc"."""
    t = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).split())


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    obcina: str = ""
    kind: str = ""
    settlement: str | None = None   # naselje of the nearest address; None = not looked up / lookup failed
    address: str = ""


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


_GEO_LOCK = asyncio.Lock()
_GEO_LAST = 0.0
GEO_SPACING_S = 1.1     # Nominatim's usage policy: at most one request per second


def settlement_of(addr: dict) -> str:
    """The naselje of a Nominatim address: city/town/village (a hamlet or quarter is a part of one)."""
    for k in ("city", "town", "village", "hamlet", "isolated_dwelling"):
        if addr.get(k):
            return str(addr[k])
    return ""


def matching_rule(rules, title: str):
    t = _norm(title)
    return next((r for r in rules if any(_norm(x) in t for x in r.title_contains)), None)


def rule_verdict(rule, title: str, description: str, loc: Location | None) -> str | None:
    """None = notify. Otherwise the reason it is held back (fail closed: an unknown place is not "Idrija")."""
    if rule is None:
        return None
    where = "no location"
    if rule.place and loc is not None and loc.settlement is not None:
        if _norm(loc.settlement) == _norm(rule.place):
            return None
        where = f"nearest address in {loc.settlement or 'no settlement'}" + (f" ({loc.address})" if loc.address else "")
    elif rule.near_lat is not None and loc is not None:
        km = distance_km(rule.near_lat, rule.near_lon, loc.lat, loc.lon)
        if km <= rule.radius_km:
            return None
        where = f"{km:.1f} km away" + (f" (občina {loc.obcina})" if loc.obcina else "")
    if rule.require_any:
        d = _norm(description)
        for ph in rule.ignore_phrases:
            d = d.replace(_norm(ph), " ")
        if any(_norm(x) in d for x in rule.require_any):
            return None
    return f"'{title}': {where}" + (f", and no {' / '.join(rule.require_any)} in the description" if rule.require_any else "")


def pushover_from_env() -> tuple[str, str] | None:
    token = os.environ.get("SHERPA_PUSHOVER_TOKEN", "").strip()
    user = os.environ.get("SHERPA_PUSHOVER_USER", "").strip()
    return (token, user) if token and user else None


class PushoverNotifier:
    def __init__(self, token: str, user: str, url: str = PUSHOVER_URL):
        self._token, self._user, self._url = token, user, url
        self._client = httpx.AsyncClient(timeout=10, trust_env=False)
        self.last_error: str | None = None

    def _redact(self, s: str) -> str:
        return s.replace(self._token, "***").replace(self._user, "***")

    async def send(self, title: str, message: str, url: str = "", url_title: str = "", priority: int = 1,
                   sound: str = "", timestamp: int | None = None) -> bool:
        data = {"token": self._token, "user": self._user, "title": title[:250], "message": (message or title)[:1024],
                "priority": str(priority)}
        if url:
            data["url"], data["url_title"] = url[:512], url_title[:100]
        if sound:
            data["sound"] = sound
        if timestamp:
            data["timestamp"] = str(timestamp)
        try:
            r = await self._client.post(self._url, data=data)
            try:
                body = r.json()
            except ValueError:
                body = None
            if r.status_code == 200 and isinstance(body, dict) and body.get("status") == 1:
                self.last_error = None
                return True
            self.last_error = self._redact(f"HTTP {r.status_code} {r.text[:160]}")
        except Exception as e:
            self.last_error = self._redact(f"{type(e).__name__}: {e}")[:200]
        log.error("Pushover send failed: %s", self.last_error)
        return False

    async def aclose(self) -> None:
        await self._client.aclose()


class PushFeed:
    def __init__(self, cfg: PushFeedCfg, store, alerts, notifier: PushoverNotifier | None):
        self.cfg, self.store, self.alerts, self.notifier = cfg, store, alerts, notifier
        fc = FeedCfg(url=cfg.url, poll_interval_s=cfg.poll_interval_s, timeout_s=cfg.timeout_s,
                     max_age_min=cfg.max_age_min, expected_link_contains=cfg.expected_link_contains,
                     user_agent=cfg.user_agent)
        self.fetcher = FeedFetcher(fc)
        self._first = True
        self._bad_polls = 0
        self._bad_sends = 0
        self.ok: bool | None = None
        self.last_ok_at: float | None = None
        self.notified = 0
        self.last_error = ""
        self.filtered = 0
        self.last_filtered = ""
        self._loc_cache: dict[str, Location] = {}
        self._waiting_since: dict[str, float] = {}   # guid -> monotonic time its location was first missing
        self._bad_locs = 0
        self._bad_geo = 0

    @property
    def _key(self) -> str:
        return f"pushfeed:{self.cfg.name}"

    def snapshot(self) -> dict:
        return {"url": self.cfg.url, "ok": self.ok, "last_ok_at": self.last_ok_at, "notified": self.notified,
                "error": self.last_error, "pushover": self.notifier is not None,
                "filtered": self.filtered, "last_filtered": self.last_filtered}

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # nothing may end this loop
                log.exception("push feed %s: poll crashed", self.cfg.name)
            await asyncio.sleep(self.cfg.poll_interval_s)

    async def poll_once(self) -> None:
        try:
            parsed = parse_feed(await self.fetcher.fetch())
        except (FetchError, FeedFormatError) as e:
            self.ok, self.last_error = False, f"{type(e).__name__}: {e}"
            self._bad_polls += 1
            if self._bad_polls >= FAIL_AFTER:
                self.alerts.raise_(self._key, f"push feed '{self.cfg.name}' cannot be read: {self.last_error}. "
                                              "No phone notifications for it until this is fixed.")
            return
        self.ok, self.last_error, self.last_ok_at = True, "", time.time()
        self._bad_polls = 0
        self.alerts.clear(self._key, f"push feed '{self.cfg.name}' is readable again")
        exp = self.cfg.expected_link_contains
        if exp and (not parsed.self_link or exp not in parsed.self_link):
            self.alerts.raise_(f"{self._key}:link", f"push feed '{self.cfg.name}': self-link {parsed.self_link!r} no "
                                                    f"longer contains {exp!r}; the server-side filter may have changed")
        else:
            self.alerts.clear(f"{self._key}:link", f"push feed '{self.cfg.name}' self-link is as expected again")
        await self._handle(parsed.items)

    async def _location(self, it: FeedItem) -> Location | None:
        """The event's approximate map location, or None (never raises; repeated failures alert)."""
        if it.guid in self._loc_cache:
            return self._loc_cache[it.guid]
        m = re.search(r"/(\d+)/?$", it.link or it.guid) or re.search(r"/(\d+)/?$", it.guid)
        if not m:
            return None
        try:
            r = await asyncio.wait_for(self.fetcher.client.get(self.cfg.location_url.format(id=m[1])), self.cfg.timeout_s)
            v = r.json().get("value") if r.status_code == 200 else None
            loc = Location(float(v["wgsLatApprox"]), float(v["wgsLonApprox"]), str(v.get("obcinaNaziv") or ""),
                           str(v.get("intervencijaVrstaNaziv") or ""))
        except Exception as e:
            self._bad_locs += 1
            log.warning("push feed %s: no location for %s: %s", self.cfg.name, it.guid, f"{type(e).__name__}: {e}"[:200])
            if self._bad_locs >= FAIL_AFTER:
                self.alerts.raise_(f"{self._key}:location", f"push feed '{self.cfg.name}': the event location service "
                                   "does not answer; restricted types (rules) are NOT notified until it does")
            return None
        self._bad_locs = 0
        self.alerts.clear(f"{self._key}:location", f"push feed '{self.cfg.name}': event locations are readable again")
        self._loc_cache[it.guid] = loc
        return loc

    async def _geocode(self, it: FeedItem, loc: Location) -> Location:
        """loc with the nearest address filled in; settlement stays None if the lookup failed (retried later)."""
        global _GEO_LAST
        try:
            async with _GEO_LOCK:
                wait = _GEO_LAST + GEO_SPACING_S - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await asyncio.wait_for(self.fetcher.client.get(self.cfg.geocode_url, params={
                        "format": "jsonv2", "lat": f"{loc.lat}", "lon": f"{loc.lon}", "zoom": "18",
                        "addressdetails": "1", "accept-language": "sl"}, headers={"Accept": "application/json"}),
                        self.cfg.timeout_s)
                finally:
                    _GEO_LAST = time.monotonic()
            if r.status_code != 200:
                raise FetchError(f"HTTP {r.status_code}")
            d = r.json()
            addr = d.get("address") or {}  # {"error": "Unable to geocode"} = nothing near: no settlement, a real answer
            road = " ".join(x for x in (addr.get("road", ""), addr.get("house_number", "")) if x)
            settlement = settlement_of(addr)
            done = replace(loc, settlement=settlement, address=", ".join(x for x in (road, settlement) if x))
        except Exception as e:
            self._bad_geo += 1
            log.warning("push feed %s: no address for %s: %s", self.cfg.name, it.guid, f"{type(e).__name__}: {e}"[:200])
            if self._bad_geo >= FAIL_AFTER:
                self.alerts.raise_(f"{self._key}:geocode", f"push feed '{self.cfg.name}': the address lookup "
                                   f"({self.cfg.geocode_url}) does not answer; restricted types are NOT notified")
            return loc
        self._bad_geo = 0
        self.alerts.clear(f"{self._key}:geocode", f"push feed '{self.cfg.name}': address lookup works again")
        self._loc_cache[it.guid] = done
        return done

    def _forget(self, guid: str) -> None:
        self._loc_cache.pop(guid, None)
        self._waiting_since.pop(guid, None)

    async def _handle(self, items: list[FeedItem]) -> None:
        name, now = self.cfg.name, datetime.now(UTC)
        max_age = timedelta(minutes=self.cfg.max_age_min)
        unseen = sorted((it for it in items if not self.store.push_seen(name, it.guid)),
                        key=lambda it: it.pub_date or now)
        sent_any = failed_any = False
        for it in unseen:
            age = (now - it.pub_date) if it.pub_date else None
            if self._first and (age is None or age > max_age):
                self.store.push_record(name, it, now, "ignored_old")
                continue
            rule = matching_rule(self.cfg.rules, it.title)
            loc = await self._location(it)
            if loc is not None and loc.settlement is None and any(r.place for r in self.cfg.rules):
                loc = await self._geocode(it, loc)  # also names the place in every notification
            needs = rule is not None and (rule.near_lat is not None or bool(rule.place))
            if needs and (loc is None or (rule.place and loc.settlement is None)):
                # the location may simply not be readable yet: look again on the next polls, then give up (fail closed)
                since = self._waiting_since.setdefault(it.guid, time.monotonic())
                if time.monotonic() - since < LOCATION_WAIT_S:
                    continue
            why = rule_verdict(rule, it.title, it.description, loc)
            if why:  # a deliberate "no": remembered, so it is neither retried nor sent later
                self._forget(it.guid)
                self.store.push_record(name, it, now, "filtered")
                self.filtered += 1
                self.last_filtered = why
                log.warning("PUSH_FILTERED feed=%s guid=%s: %s", name, it.guid, why)
                continue
            if self.notifier is None:
                self.alerts.raise_("pushover", "a push feed has news but Pushover is not configured "
                                               "(SHERPA_PUSHOVER_TOKEN / SHERPA_PUSHOVER_USER in .env)")
                continue  # not recorded: it is delivered once Pushover works, if still young
            if age is not None and age > GIVE_UP_AFTER:
                self._forget(it.guid)
                self.store.push_record(name, it, now, "dropped_old")
                self.alerts.info(f"push feed '{name}': dropped '{it.title}' after failing to deliver it for "
                                 f"{int(age.total_seconds() // 60)} min")
                continue
            local = it.pub_date.astimezone().strftime("%d.%m.%Y %H:%M") if it.pub_date else ""
            place = (f"~{loc.address}" if loc and loc.address else (f"občina {loc.obcina}" if loc and loc.obcina else ""))
            body = " | ".join(x for x in (place, it.description, local) if x) or it.title
            ok = await self.notifier.send(it.title or name, body, it.link, "Zemljevid", self.cfg.priority,
                                          self.cfg.sound, int(it.pub_date.timestamp()) if it.pub_date else None)
            if ok:
                self._forget(it.guid)
                self.store.push_record(name, it, now, "notified")
                self.notified += 1
                sent_any = True
                log.warning("PUSH_SENT feed=%s guid=%s title=%r", name, it.guid, it.title)
            else:
                failed_any = True
        self._first = False
        if self.notifier is not None:
            if failed_any:
                self._bad_sends += 1
                if self._bad_sends >= FAIL_AFTER:
                    self.alerts.raise_("pushover", f"Pushover is rejecting notifications: {self.notifier.last_error}")
            elif sent_any or not unseen:
                self._bad_sends = 0
                self.alerts.clear("pushover", "Pushover delivers again")
