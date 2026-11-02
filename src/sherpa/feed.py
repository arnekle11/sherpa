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


def _parse_date(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    try:
        dt = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_feed(data: bytes) -> ParsedFeed:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as e:
        raise FeedFormatError(f"invalid XML: {e}") from e
    if root is None or etree.QName(root).localname != "rss":
        raise FeedFormatError(f"root element is not <rss> (got {getattr(root, 'tag', None)!r})")
    channel = root.find("channel")
    if channel is None:
        raise FeedFormatError("no <channel> element")

    atom_self = channel.xpath(f"*[local-name()='link' and @rel='self']/@href")
    self_link = (atom_self[0] if atom_self else (channel.findtext("link") or "")).strip() or None

    problems: list[str] = []
    items: list[FeedItem] = []
    for idx, el in enumerate(channel.findall("item")):
        title = (el.findtext("title") or "").strip()
        link = (el.findtext("link") or "").strip()
        guid = (el.findtext("guid") or "").strip() or link
        raw_date = el.findtext("pubDate")
        pub = _parse_date(raw_date)
        if not guid:
            # Fail open: still dedupe-able, still triggers.
            guid = "synthetic:" + hashlib.sha1(f"{title}|{raw_date}".encode()).hexdigest()
            problems.append(f"item #{idx} has no guid/link (using synthetic id)")
        if not title:
            problems.append(f"item {guid} has no title")
        if pub is None:
            problems.append(f"item {guid} has missing/unparseable pubDate ({raw_date!r})")
        items.append(
            FeedItem(guid=guid, title=title, link=link,
                     description=(el.findtext("description") or "").strip(), pub_date=pub)
        )
    return ParsedFeed(self_link=self_link, items=items, problems=problems)


class FeedFetcher:
    """GET with a persistent keep-alive connection, hard total timeout, size cap."""

    def __init__(self, cfg: FeedCfg, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": cfg.user_agent, "Accept": "application/rss+xml, text/xml, */*"},
            timeout=httpx.Timeout(cfg.timeout_s),
            limits=httpx.Limits(max_keepalive_connections=1, keepalive_expiry=60),
            transport=httpx.AsyncHTTPTransport(retries=1),  # connect-level retry only
        )

    async def _get(self) -> bytes:
        async with self.client.stream("GET", self.cfg.url) as r:
            if r.status_code != 200:
                raise FetchError(f"HTTP {r.status_code}")
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
