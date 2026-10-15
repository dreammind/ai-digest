"""HN 検索のフェッチャー（specs/004-fetchers.md）のテスト。"""

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aidigest.fetchers.hn import fetch_hn
from aidigest.fetchers.http import FetchError
from aidigest.models import FetchedItem
from tests.fetchers.helpers import hn_source, make_client

NOW = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)
SINCE = int((NOW - timedelta(hours=36)).timestamp())


def hit(object_id: str, created_at: str, **fields: object) -> dict[str, object]:
    return {
        "objectID": object_id,
        "title": f"Story {object_id}",
        "url": f"https://example.com/{object_id}",
        "created_at": created_at,
        **fields,
    }


class FakeHn:
    """クエリごとに決まったヒットを返す Algolia HN Search API。"""

    def __init__(self, hits_by_query: dict[str, list[dict[str, object]]]) -> None:
        self.hits_by_query = hits_by_query
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        hits = self.hits_by_query.get(request.url.params["query"], [])
        return httpx.Response(200, json={"hits": hits})


def fetch(handler: FakeHn, **overrides: object) -> list[FetchedItem]:
    return fetch_hn(make_client(handler), hn_source(**overrides), NOW, time_window_hours=36)


def test_and_query_uses_single_request_with_all_conditions() -> None:
    """AC-004-11"""
    fake = FakeHn({'"Claude Code" "MCP"': [hit("1", "2026-10-15T10:00:00Z")]})

    items = fetch(fake, query=["Claude Code", "MCP"], query_operator="and", min_score=20)

    assert [i.title for i in items] == ["Story 1"]
    (request,) = fake.requests
    assert request.url.host == "hn.algolia.com"
    assert request.url.path == "/api/v1/search_by_date"
    params = request.url.params
    assert params["query"] == '"Claude Code" "MCP"'
    assert params["tags"] == "story"
    assert params["restrictSearchableAttributes"] == "title"
    assert params["advancedSyntax"] == "true"
    assert params["hitsPerPage"] == "10"
    assert set(params["numericFilters"].split(",")) == {f"created_at_i>{SINCE}", "points>=20"}


def test_points_filter_is_omitted_without_min_score() -> None:
    """AC-004-12"""
    fake = FakeHn({})

    fetch(fake, query="LLM")

    assert fake.requests[0].url.params["numericFilters"] == f"created_at_i>{SINCE}"


def test_or_query_requests_each_term_and_merges() -> None:
    """AC-004-13"""
    fake = FakeHn(
        {
            '"A"': [hit("1", "2026-10-15T10:00:00Z"), hit("2", "2026-10-15T09:00:00Z")],
            '"B"': [hit("2", "2026-10-15T09:00:00Z"), hit("3", "2026-10-15T11:00:00Z")],
            '"C"': [hit("4", "2026-10-15T08:00:00Z"), hit("5", "2026-10-15T07:00:00Z")],
        }
    )

    items = fetch(fake, query=["A", "B", "C"], query_operator="or", max_items=3)

    assert [r.url.params["query"] for r in fake.requests] == ['"A"', '"B"', '"C"']
    assert [i.title for i in items] == ["Story 3", "Story 1", "Story 2"]


def test_hits_become_fetched_items() -> None:
    """AC-004-14"""
    fake = FakeHn(
        {
            '"Claude Code"': [
                hit("10", "2026-10-15T10:00:00Z", story_text="Some text"),
                hit("11", "2026-10-15T09:00:00Z", url=None, title="Ask HN: Claude Code?"),
            ]
        }
    )

    linked, ask = fetch(fake)

    assert linked.source_id == "hn"
    assert linked.url == "https://example.com/10"
    assert linked.published_at == datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    assert linked.content == "Some text"
    assert ask.url == "https://news.ycombinator.com/item?id=11"
    assert ask.title == "Ask HN: Claude Code?"
    assert ask.content == ""


def test_invalid_hit_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """AC-004-15"""
    fake = FakeHn(
        {
            '"Claude Code"': [
                {"objectID": "bad", "created_at": "2026-10-15T10:00:00Z"},
                hit("ok", "2026-10-15T09:00:00Z"),
            ]
        }
    )

    with caplog.at_level(logging.WARNING, logger="aidigest.fetchers.hn"):
        items = fetch(fake)

    assert [i.title for i in items] == ["Story ok"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.parametrize("body", [json.dumps({"nope": 1}), json.dumps({"hits": "x"}), "not json"])
def test_invalid_response_fails(body: str) -> None:
    """AC-004-15"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    with pytest.raises(FetchError):
        fetch_hn(make_client(handler), hn_source(), NOW, time_window_hours=36)
