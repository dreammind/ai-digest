"""Zenn のトピック（非公式 API）のフェッチャー（specs/004-fetchers.md）のテスト。"""

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from aidigest.fetchers.http import FetchError
from aidigest.fetchers.zenn import fetch_zenn
from aidigest.models import FetchedItem
from tests.fetchers.helpers import make_client, zenn_source

NOW = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)  # 7 日前は 2026-10-08T21:30Z


def article(n: int, published_at: str, likes: int, **fields: object) -> dict[str, object]:
    return {
        "id": n,
        "title": f"Article {n}",
        "path": f"/user/articles/a{n}",
        "published_at": published_at,
        "liked_count": likes,
        **fields,
    }


class FakeZenn:
    """ページごとに決まった記事を返す Zenn の API。"""

    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        page = int(request.url.params["page"])
        next_page = page + 1 if page < len(self.pages) else None
        return httpx.Response(200, json={"articles": self.pages[page - 1], "next_page": next_page})


def fetch(fake: FakeZenn, **overrides: object) -> list[FetchedItem]:
    return fetch_zenn(make_client(fake), zenn_source(**overrides), NOW)


def test_pages_are_requested_with_weekly_order() -> None:
    """AC-004-23"""
    fake = FakeZenn([[], [], []])

    fetch(fake, topic="claudecode", pages=2)

    assert [r.url.params["page"] for r in fake.requests] == ["1", "2"]
    for request in fake.requests:
        assert request.url.host == "zenn.dev"
        assert request.url.path == "/api/articles"
        assert request.url.params["topicname"] == "claudecode"
        assert request.url.params["order"] == "weekly"
        assert request.url.params["count"] == "48"


def test_stops_when_there_is_no_next_page() -> None:
    """AC-004-23"""
    fake = FakeZenn([[]])

    fetch(fake, pages=3)

    assert len(fake.requests) == 1


def test_only_recent_and_liked_articles_are_returned() -> None:
    """AC-004-24"""
    fake = FakeZenn(
        [
            [
                article(1, "2026-10-14T10:00:00.000+09:00", 74),
                article(2, "2026-10-09T08:00:00.000+09:00", 5),  # 10/8 23:00Z: 範囲内・ちょうど 5
                article(3, "2026-10-15T12:00:00.000+09:00", 4),  # いいね不足
                article(4, "2026-10-09T06:00:00.000+09:00", 30),  # 10/8 21:00Z: 7 日より前
            ],
            [
                article(1, "2026-10-14T10:00:00.000+09:00", 74),  # ページをまたいだ重複
                article(5, "2026-10-15T20:00:00.000+09:00", 9),
            ],
        ]
    )

    items = fetch(fake, min_likes=5, lookback_days=7, pages=2, max_items=10)

    assert [i.title for i in items] == ["Article 5", "Article 1", "Article 2"]


def test_max_items_limits_newest_first() -> None:
    """AC-004-24"""
    fake = FakeZenn([[article(n, f"2026-10-1{n}T10:00:00.000+09:00", 10) for n in range(1, 5)]])

    items = fetch(fake, max_items=2)

    assert [i.title for i in items] == ["Article 4", "Article 3"]


def test_articles_become_fetched_items() -> None:
    """AC-004-25"""
    fake = FakeZenn([[article(7, "2026-10-14T10:00:00.000+09:00", 8)]])

    (item,) = fetch(fake, id="zenn-claudecode")

    assert item.source_id == "zenn-claudecode"
    assert item.url == "https://zenn.dev/user/articles/a7"
    assert item.title == "Article 7"
    assert item.published_at == datetime(2026, 10, 14, 1, 0, tzinfo=UTC)
    assert item.content == ""


def test_invalid_article_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """AC-004-26"""
    fake = FakeZenn(
        [
            [
                {"id": 1, "title": "No path", "published_at": "2026-10-14T10:00:00+09:00"},
                article(2, "2026-10-14T10:00:00.000+09:00", 8),
            ]
        ]
    )

    with caplog.at_level(logging.WARNING, logger="aidigest.fetchers.zenn"):
        items = fetch(fake)

    assert [i.title for i in items] == ["Article 2"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.parametrize(
    "body", [json.dumps({"message": "x"}), json.dumps({"articles": "x"}), "not json"]
)
def test_invalid_response_fails(body: str) -> None:
    """AC-004-26"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    with pytest.raises(FetchError):
        fetch_zenn(make_client(handler), zenn_source(), NOW)
