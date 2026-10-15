"""Qiita 検索のフェッチャー（specs/004-fetchers.md）のテスト。"""

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from aidigest.fetchers.http import FetchError
from aidigest.fetchers.qiita import fetch_qiita
from tests.fetchers.helpers import make_client, qiita_source

NOW = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)  # 日本時間では 10/16 06:30


def article(n: int, **fields: object) -> dict[str, object]:
    return {
        "url": f"https://qiita.com/user/items/{n}",
        "title": f"Article {n}",
        "created_at": "2026-10-12T15:26:48+09:00",
        "body": f"Body {n}",
        **fields,
    }


class FakeQiita:
    def __init__(self, body: object) -> None:
        self.body = body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.body)


@pytest.mark.parametrize(("max_items", "per_page"), [(20, "20"), (150, "100")])
def test_query_has_created_and_stocks_conditions(max_items: int, per_page: str) -> None:
    """AC-004-16"""
    fake = FakeQiita([])

    fetch_qiita(
        make_client(fake),
        qiita_source(query="tag:LLM", min_stocks=3, lookback_days=7, max_items=max_items),
        NOW,
    )

    (request,) = fake.requests
    assert request.url.host == "qiita.com"
    assert request.url.path == "/api/v2/items"
    assert request.url.params["query"] == "tag:LLM created:>=2026-10-09 stocks:>=3"
    assert request.url.params["per_page"] == per_page
    assert request.url.params["page"] == "1"


def test_articles_become_fetched_items() -> None:
    """AC-004-17"""
    fake = FakeQiita([article(1), article(2, body="")])

    first, second = fetch_qiita(make_client(fake), qiita_source(id="qiita-llm"), NOW)

    assert first.source_id == "qiita-llm"
    assert first.url == "https://qiita.com/user/items/1"
    assert first.title == "Article 1"
    assert first.published_at == datetime(2026, 10, 12, 6, 26, 48, tzinfo=UTC)
    assert first.content == "Body 1"
    assert second.content == ""


def test_invalid_article_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """AC-004-17（AC-004-15 と同じ扱い）"""
    fake = FakeQiita([{"url": "https://qiita.com/x", "created_at": "bad"}, article(2)])

    with caplog.at_level(logging.WARNING, logger="aidigest.fetchers.qiita"):
        items = fetch_qiita(make_client(fake), qiita_source(), NOW)

    assert [i.title for i in items] == ["Article 2"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.parametrize("body", [json.dumps({"message": "x"}), "not json"])
def test_invalid_response_fails(body: str) -> None:
    """AC-004-17（AC-004-15 と同じ扱い）"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    with pytest.raises(FetchError):
        fetch_qiita(make_client(handler), qiita_source(), NOW)
