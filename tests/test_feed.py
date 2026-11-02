import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sherpa.config import FeedCfg
from sherpa.feed import FeedFetcher, FeedFormatError, FetchError, parse_feed
from sherpa.mock import MockFeed

FX = Path(__file__).parent / "fixtures"


def test_idle_feed_is_valid_and_empty():
    p = parse_feed((FX / "idle.xml").read_bytes())
    assert p.items == [] and p.problems == []
    assert "OD/52034" in p.self_link


def test_sample_items_parsed_in_any_order():
    p = parse_feed((FX / "sample.xml").read_bytes())
    assert [i.guid.rsplit("/", 1)[1] for i in p.items] == ["501", "499", "500"]  # order is not trusted
    assert p.items[0].pub_date == datetime(2026, 11, 2, 16, 30, tzinfo=UTC)
    assert p.problems == []


@pytest.mark.parametrize("body", [b"", b"<html/>", b"<rss version='2.0'></rss>", b"\x00garbage"])
def test_structural_breakage_raises(body):
    with pytest.raises(FeedFormatError):
        parse_feed(body)


def test_item_without_guid_fails_open_with_problem():
    xml = b"<rss><channel><item><title>x</title><pubDate>Mon, 02 Nov 2026 16:30:00 GMT</pubDate></item></channel></rss>"
    p = parse_feed(xml)
    assert len(p.items) == 1 and p.items[0].guid.startswith("synthetic:") and p.problems


def test_bad_pubdate_is_reported_not_fatal():
    xml = b"<rss><channel><item><title>x</title><guid>g</guid><pubDate>yesterday</pubDate></item></channel></rss>"
    p = parse_feed(xml)
    assert p.items[0].pub_date is None and any("pubDate" in x for x in p.problems)


def test_xxe_entities_not_resolved():
    xml = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><title>&x;</title></channel></rss>'
    p = parse_feed(xml)  # must not read the file or raise
    assert p.items == []


def test_fetch_timeout_and_http_errors():
    async def go():
        feed = MockFeed().start()
        f = FeedFetcher(FeedCfg(url=feed.url, timeout_s=0.5))
        try:
            assert b"<rss" in await f.fetch()
            feed.mode = "500"
            with pytest.raises(FetchError, match="500"):
                await f.fetch()
            feed.mode = "slow"
            with pytest.raises(FetchError, match="timeout"):
                await f.fetch()
        finally:
            await f.aclose()
            feed.stop()
    asyncio.run(go())
