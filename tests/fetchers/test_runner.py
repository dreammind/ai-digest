"""全ソースの取得と source_health への記録（specs/004-fetchers.md）のテスト。"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter

from aidigest.config import Source, load_sources
from aidigest.db import open_db
from aidigest.fetchers.runner import SourceHealth, fetch_all, record_health
from tests.fetchers.helpers import hn_source, make_client, qiita_source, rss_source

NOW = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ADAPTER = TypeAdapter[Source](Source)


def feed(*titles: str) -> str:
    items = "".join(
        f"<item><title>{t}</title><link>https://example.com/{t}</link></item>" for t in titles
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{items}</channel></rss>'
    )


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [("raise", "RuntimeError: "), ("404", "FetchError: ")],
    ids=["unexpected-exception", "http-error"],
)
def test_failing_source_does_not_stop_others(failure: str, expected_error: str) -> None:
    """AC-004-18"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "b.example.com":
            if failure == "raise":
                raise RuntimeError("boom" * 300)
            return httpx.Response(404)
        name = request.url.host.split(".")[0]
        return httpx.Response(200, text=feed(f"{name}1", f"{name}2"))

    sources: list[Source] = [
        rss_source(id="a", url="https://a.example.com/feed"),
        rss_source(id="b", url="https://b.example.com/feed"),
        rss_source(id="c", url="https://c.example.com/feed"),
    ]

    result = fetch_all(make_client(handler), sources, NOW, time_window_hours=36)

    assert [i.title for i in result.items] == ["a1", "a2", "c1", "c2"]
    a, b, c = result.health
    assert (a.source_id, a.status, a.error, a.item_count) == ("a", "ok", None, 2)
    assert (c.source_id, c.status, c.error, c.item_count) == ("c", "ok", None, 2)
    assert (b.source_id, b.status, b.item_count) == ("b", "error", 0)
    assert b.error is not None
    assert b.error.startswith(expected_error)
    assert len(b.error) <= 500


def test_each_source_type_is_dispatched_to_its_fetcher() -> None:
    """AC-004-18: rss / hn_search / qiita_search がそれぞれのフェッチャーで取得される"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hn.algolia.com":
            hit = {
                "objectID": "1",
                "title": "HN story",
                "url": "https://example.com/hn",
                "created_at": "2026-10-15T10:00:00Z",
            }
            return httpx.Response(200, json={"hits": [hit]})
        if request.url.host == "qiita.com":
            article = {
                "url": "https://qiita.com/u/items/1",
                "title": "Qiita article",
                "created_at": "2026-10-12T10:00:00+09:00",
            }
            return httpx.Response(200, json=[article])
        return httpx.Response(200, text=feed("rss1"))

    sources: list[Source] = [rss_source(id="r"), hn_source(id="h"), qiita_source(id="q")]

    result = fetch_all(make_client(handler), sources, NOW, time_window_hours=36)

    assert [(i.source_id, i.title) for i in result.items] == [
        ("r", "rss1"),
        ("h", "HN story"),
        ("q", "Qiita article"),
    ]
    assert [h.status for h in result.health] == ["ok", "ok", "ok"]


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "github_release", "repo": "anthropics/claude-code"},
        {"type": "arxiv", "query": "LLM"},
    ],
    ids=["github_release", "arxiv"],
)
def test_unsupported_type_is_reported_without_request(entry: dict[str, object]) -> None:
    """AC-004-19"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"リクエストしてはいけない: {request.url}")

    source = SOURCE_ADAPTER.validate_python(
        {"id": "x", "category": "official", "priority": 1, "max_items": 5, **entry}
    )

    result = fetch_all(make_client(handler), [source], NOW, time_window_hours=36)

    (health,) = result.health
    assert health.status == "error"
    assert health.error == f"未対応の type: {entry['type']}"
    assert result.items == []


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


def test_health_is_recorded_without_commit(conn: sqlite3.Connection) -> None:
    """AC-004-20"""
    run_id = conn.execute(
        "INSERT INTO runs (started_at) VALUES ('2026-10-15T21:30:00+00:00') RETURNING id"
    ).fetchone()[0]
    conn.commit()
    health = [
        SourceHealth("a", "ok", None, 3),
        SourceHealth("b", "error", "FetchError: 404", 0),
    ]

    record_health(conn, run_id, health)

    rows = conn.execute(
        "SELECT source_id, status, error, item_count FROM source_health"
        " WHERE run_id = ? ORDER BY source_id",
        (run_id,),
    ).fetchall()
    assert [tuple(r) for r in rows] == [("a", "ok", None, 3), ("b", "error", "FetchError: 404", 0)]
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0] == 0


def test_repository_sources_yaml_matches_spec() -> None:
    """AC-004-22"""
    loaded = load_sources(PROJECT_ROOT / "sources.yaml")

    assert loaded.errors == []
    actual = [
        (s.id, s.type, s.category, s.priority, s.max_items, getattr(s, "min_score", None))
        for s in loaded.sources
    ]
    assert actual == [
        ("zenn-claudecode", "rss", "japanese", 1, 20, None),
        ("zenn-anthropic", "rss", "japanese", 1, 20, None),
        ("zenn-llm", "rss", "japanese", 2, 20, None),
        ("qiita-claudecode", "qiita_search", "japanese", 1, 20, None),
        ("qiita-llm", "qiita_search", "japanese", 2, 20, None),
        ("qiita-aiagent", "qiita_search", "japanese", 2, 20, None),
        ("hn-claude-code", "hn_search", "community", 2, 20, 20),
        ("hn-llm", "hn_search", "community", 2, 20, 100),
        ("hn-models", "hn_search", "community", 2, 20, 300),
        ("simonw", "rss", "community", 2, 30, None),
    ]
    targets = {
        s.id: (str(getattr(s, "url", "")), getattr(s, "query", None)) for s in loaded.sources
    }
    assert targets["zenn-claudecode"][0] == "https://zenn.dev/topics/claudecode/feed"
    assert targets["zenn-anthropic"][0] == "https://zenn.dev/topics/anthropic/feed"
    assert targets["zenn-llm"][0] == "https://zenn.dev/topics/llm/feed"
    assert targets["simonw"][0] == "https://simonwillison.net/atom/everything/"
    assert targets["qiita-claudecode"][1] == "tag:ClaudeCode"
    assert targets["qiita-llm"][1] == "tag:LLM"
    assert targets["qiita-aiagent"][1] == "tag:AIエージェント"
    assert targets["hn-claude-code"][1] == ["Claude Code"]
    assert targets["hn-llm"][1] == ["LLM"]
    assert targets["hn-models"][1] == ["Gemini", "OpenAI", "Anthropic", "GPT"]
    models = next(s for s in loaded.sources if s.id == "hn-models")
    assert getattr(models, "query_operator", None) == "or"
