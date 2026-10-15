"""RSS / Atom のフェッチャー（specs/004-fetchers.md）のテスト。"""

import logging
from datetime import UTC, datetime

import httpx
import pytest

from aidigest.fetchers.http import FetchError
from aidigest.fetchers.rss import fetch_rss
from aidigest.models import FetchedItem
from tests.fetchers.helpers import make_client, rss_source

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <title>First</title>
    <link href="https://example.com/1"/>
    <published>2026-10-15T09:00:00+09:00</published>
    <updated>2026-10-15T10:00:00+09:00</updated>
    <content type="text">Body one</content>
  </entry>
  <entry>
    <title>Second</title>
    <link href="https://example.com/2"/>
    <updated>2026-10-14T12:00:00Z</updated>
    <summary>Summary two</summary>
  </entry>
</feed>
"""

RSS2 = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Example</title>
  <item>
    <title>A</title>
    <link>https://example.com/a</link>
    <pubDate>Wed, 14 Oct 2026 21:23:01 +0000</pubDate>
    <description>Description A</description>
  </item>
  <item>
    <title>No date</title>
    <link>https://example.com/b</link>
    <description>Description B</description>
  </item>
</channel></rss>
"""


def fetch(body: str, **overrides: object) -> list[FetchedItem]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/feed.xml"
        return httpx.Response(200, text=body, headers={"content-type": "application/xml"})

    return fetch_rss(make_client(handler), rss_source(id="feed", **overrides))


def rss_with_items(count: int) -> str:
    items = "".join(
        f"<item><title>Item {i}</title><link>https://example.com/{i}</link></item>"
        for i in range(count)
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{items}</channel></rss>'
    )


def test_atom_entries_become_fetched_items() -> None:
    """AC-004-6"""
    first, second = fetch(ATOM)

    assert first.source_id == "feed"
    assert first.url == "https://example.com/1"
    assert first.title == "First"
    assert first.published_at == datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
    assert first.content == "Body one"
    assert second.published_at == datetime(2026, 10, 14, 12, 0, tzinfo=UTC), (
        "公開日時が無ければ更新日時"
    )
    assert second.content == "Summary two"


def test_rss2_items_become_fetched_items() -> None:
    """AC-004-6"""
    first, _ = fetch(RSS2)

    assert first.url == "https://example.com/a"
    assert first.title == "A"
    assert first.published_at == datetime(2026, 10, 14, 21, 23, 1, tzinfo=UTC)
    assert first.published_at is not None
    assert first.published_at.utcoffset() == UTC.utcoffset(None)


def test_only_first_max_items_are_returned() -> None:
    """AC-004-7"""
    items = fetch(rss_with_items(5), max_items=3)

    assert [i.title for i in items] == ["Item 0", "Item 1", "Item 2"]


def test_summary_is_used_and_missing_date_is_none() -> None:
    """AC-004-8"""
    _, no_date = fetch(RSS2)

    assert no_date.content == "Description B"
    assert no_date.published_at is None


def test_entries_without_link_or_title_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """AC-004-9"""
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        "<item><title>No link</title></item>"
        "<item><link>https://example.com/no-title</link></item>"
        "<item><title>Good</title><link>https://example.com/good</link></item>"
        "</channel></rss>"
    )

    with caplog.at_level(logging.WARNING, logger="aidigest.fetchers.rss"):
        items = fetch(body)

    assert [i.title for i in items] == ["Good"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_empty_but_valid_feed_returns_no_items() -> None:
    """AC-004-10 の境界: 正しいフィードでエントリが 0 件なら失敗ではない"""
    assert fetch(rss_with_items(0)) == []


@pytest.mark.parametrize(
    "body",
    ["<html><body><p>Not a feed</p></body></html>", "plain text", ""],
    ids=["html", "text", "empty"],
)
def test_non_feed_response_fails(body: str) -> None:
    """AC-004-10"""
    with pytest.raises(FetchError):
        fetch(body)
