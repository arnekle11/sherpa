"""Fetching and strictly validating the SOS 112 RSS feed.

An *empty* channel is the normal idle state. A *structural* surprise (not RSS, no
channel, items we cannot identify, changed self-link) is reported as a problem so
that a changed filter or format is never mistaken for "no emergencies".
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from lxml import etree

from .config import FeedCfg

ATOM = "http://www.w3.org/2005/Atom"


class FetchError(Exception):
    """Transport/HTTP-level failure: the feed is unreachable."""


class FeedFormatError(Exception):
    """Body is not a usable RSS 2.0 document."""


@dataclass(frozen=True)
class FeedItem:
    guid: str
    title: str
    link: str
    description: str
    pub_date: datetime | None


@dataclass
class ParsedFeed:
    self_link: str | None
    items: list[FeedItem]
    problems: list[str] = field(default_factory=list)


def _parse_date(raw: str | None) -> tuple[datetime | None, str | None]:
    """(UTC datetime, None) or (None, why). Never raises: one odd date must not stop the poll."""
    if not raw or not raw.strip():
        return None, "missing pubDate"
    try:
        try:
            dt = parsedate_to_datetime(raw.strip())
        except ValueError:
            dt = datetime.fromisoformat(raw.strip())  # ISO 8601 fallback, e.g. 2026-11-05T19:00:00Z
        if dt.tzinfo is None:  # unknown zone name (e.g. CET) or -0000: guessing UTC could be hours off
            return None, f"pubDate without a usable time zone ({raw.strip()!r})"
        return dt.astimezone(UTC), None
    except Exception:  # ValueError, TypeError, OverflowError (year 9999 with an offset), ...
        return None, f"unparseable pubDate ({raw.strip()!r})"


_STRICT = dict(resolve_entities=False, no_network=True, huge_tree=False)


def parse_feed(data: bytes) -> ParsedFeed:
    problems: list[str] = []
    data = data.lstrip()  # whitespace before the XML declaration is invalid but harmless
    try:
        root = etree.fromstring(data, etree.XMLParser(**_STRICT))
    except etree.XMLSyntaxError as e:
        # One bad character in one report (a raw '&', '&nbsp;', a control char from Word) must not hide every
        # other item for the ~31 h it stays in the feed: read leniently, keep triggering, and say so.
        try:
            root = etree.fromstring(data, etree.XMLParser(recover=True, **_STRICT))
        except etree.XMLSyntaxError:
            root = None
        if root is None:
            raise FeedFormatError(f"invalid XML: {e}") from e
        problems.append(f"feed XML is malformed and was read leniently ({e})")
    if root is None or etree.QName(root).localname != "rss":
        raise FeedFormatError(f"root element is not <rss> (got {getattr(root, 'tag', None)!r})")
    ns = etree.QName(root).namespace
    q = (lambda tag: f"{{{ns}}}{tag}") if ns else (lambda tag: tag)  # tolerate a default namespace on <rss>
    channel = root.find(q("channel"))
    if channel is None:
        raise FeedFormatError("no <channel> element")

    atom_self = channel.xpath("*[local-name()='link' and @rel='self']/@href")
    self_link = (atom_self[0] if atom_self else (channel.findtext(q("link")) or "")).strip() or None

    items: list[FeedItem] = []
    for idx, el in enumerate(channel.findall(q("item"))):
        title = (el.findtext(q("title")) or "").strip()
        link = (el.findtext(q("link")) or "").strip()
        guid = (el.findtext(q("guid")) or "").strip() or link
        raw_date = el.findtext(q("pubDate"))
        pub, date_problem = _parse_date(raw_date)
        if not guid:
            # Fail open: still dedupe-able, still triggers.
            guid = "synthetic:" + hashlib.sha1(f"{title}|{raw_date}".encode()).hexdigest()
            problems.append(f"item #{idx} has no guid/link (using synthetic id)")
        if not title:
            problems.append(f"item {guid} has no title")
        if date_problem:
            problems.append(f"item {guid}: {date_problem}")
        items.append(
            FeedItem(guid=guid, title=title, link=link,
                     description=(el.findtext(q("description")) or "").strip(), pub_date=pub)
        )
    return ParsedFeed(self_link=self_link, items=items, problems=problems)


class FeedFetcher:
    """GET with a persistent keep-alive connection, hard total timeout, size cap."""

    def __init__(self, cfg: FeedCfg, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.last_server_date: str | None = None  # HTTP Date header of the last 200 response (clock check)
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": cfg.user_agent, "Accept": "application/rss+xml, text/xml, */*"},
            timeout=httpx.Timeout(cfg.timeout_s),
            # limits must go to the transport: a client ignores its own limits= when transport= is given
            transport=httpx.AsyncHTTPTransport(
                retries=1,  # connect-level retry only
                limits=httpx.Limits(max_keepalive_connections=1, keepalive_expiry=60)),
        )

    async def _get(self) -> bytes:
        async with self.client.stream("GET", self.cfg.url) as r:
            if r.status_code != 200:
                raise FetchError(f"HTTP {r.status_code}")
            self.last_server_date = r.headers.get("date")
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > self.cfg.max_response_bytes:
                    raise FetchError("response too large")
            return bytes(buf)

    async def _get_retrying_stale(self) -> bytes:
        try:
            return await self._get()
        except (httpx.RemoteProtocolError, httpx.ReadError):
            # Server closed an idle keep-alive connection: one immediate retry.
            return await self._get()

    async def fetch(self) -> bytes:
        try:
            return await asyncio.wait_for(self._get_retrying_stale(), self.cfg.timeout_s)
        except TimeoutError as e:
            raise FetchError(f"timeout after {self.cfg.timeout_s}s") from e
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e

    async def aclose(self) -> None:
        await self.client.aclose()
