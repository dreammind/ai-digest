"""既読判定と記録（specs/003-normalize-dedupe.md）のテスト。"""

import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aidigest.config import RssSource, Source
from aidigest.db import open_db
from aidigest.models import FetchedItem
from aidigest.pipeline.dedupe import register_items
from aidigest.pipeline.normalize import content_hash

DAY1 = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)
DAY2 = datetime(2026, 10, 16, 21, 30, tzinfo=UTC)
PUBLISHED = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)


def make_source(source_id: str, *, recheck_changed: bool = False) -> RssSource:
    return RssSource.model_validate(
        {
            "id": source_id,
            "type": "rss",
            "url": "https://example.com/feed.xml",
            "category": "official",
            "priority": 1,
            "max_items": 10,
            "recheck_changed": recheck_changed,
        }
    )


SOURCES: dict[str, Source] = {
    "blog": make_source("blog"),
    "releases": make_source("releases", recheck_changed=True),
}


def item(
    url: str = "https://example.com/post?utm_source=rss",
    *,
    source_id: str = "blog",
    title: str = "Title",
    content: str = "Body",
) -> FetchedItem:
    return FetchedItem(
        source_id=source_id, url=url, title=title, published_at=PUBLISHED, content=content
    )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


def all_articles(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute("SELECT * FROM articles ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def test_new_article_is_registered_and_returned(conn: sqlite3.Connection) -> None:
    """AC-003-10"""
    result = register_items(conn, [item()], SOURCES, DAY1)

    assert len(result) == 1
    (article,) = all_articles(conn)
    assert result[0].id == article["id"]
    assert result[0].url == "https://example.com/post"
    assert result[0].item.title == "Title"
    assert article["url"] == "https://example.com/post"
    assert article["source_id"] == "blog"
    assert article["category"] == "official"
    assert article["title"] == "Title"
    assert article["published_at"] == "2026-10-15T03:00:00+00:00"
    assert article["fetched_at"] == "2026-10-15T21:30:00+00:00"
    assert article["content_hash"] == content_hash("Title", "Body")
    assert article["status"] == "new"


def test_article_without_published_at_is_registered(conn: sqlite3.Connection) -> None:
    """AC-003-10: published_at が無い記事も登録される"""
    no_date = FetchedItem(source_id="blog", url="https://example.com/x", title="T")

    register_items(conn, [no_date], SOURCES, DAY1)

    (article,) = all_articles(conn)
    assert article["published_at"] is None
    assert article["content_hash"] == content_hash("T", "")


def test_same_article_next_day_is_not_returned(conn: sqlite3.Connection) -> None:
    """AC-003-11"""
    register_items(conn, [item()], SOURCES, DAY1)
    before = all_articles(conn)

    result = register_items(conn, [item()], SOURCES, DAY2)

    assert result == []
    assert all_articles(conn) == before


def test_changed_content_is_ignored_without_recheck(conn: sqlite3.Connection) -> None:
    """AC-003-12"""
    register_items(conn, [item()], SOURCES, DAY1)
    before = all_articles(conn)

    result = register_items(conn, [item(content="Edited body")], SOURCES, DAY2)

    assert result == []
    assert all_articles(conn) == before


def test_unchanged_content_is_skipped_with_recheck(conn: sqlite3.Connection) -> None:
    """AC-003-13"""
    release = item("https://github.com/o/r/releases/tag/v1", source_id="releases")
    register_items(conn, [release], SOURCES, DAY1)
    before = all_articles(conn)

    result = register_items(conn, [release], SOURCES, DAY2)

    assert result == []
    assert all_articles(conn) == before


def test_changed_content_is_recollected_with_recheck(conn: sqlite3.Connection) -> None:
    """AC-003-14"""
    url = "https://github.com/o/r/releases/tag/v1"
    register_items(conn, [item(url, source_id="releases")], SOURCES, DAY1)
    conn.execute(
        "UPDATE articles SET status = 'sent', local_score = 1.0, llm_score = 0.5,"
        " final_score = 0.8, matched_topics = '[]', summary_json = '{}',"
        " score_model = 'm1', summary_model = 'm2'"
    )
    (original,) = all_articles(conn)

    edited = item(url, source_id="releases", title="v1 (edited)", content="New notes")
    result = register_items(conn, [edited], SOURCES, DAY2)

    (article,) = all_articles(conn)
    assert [r.id for r in result] == [original["id"]]
    assert article["id"] == original["id"]
    assert article["title"] == "v1 (edited)"
    assert article["content_hash"] == content_hash("v1 (edited)", "New notes")
    assert article["fetched_at"] == "2026-10-16T21:30:00+00:00"
    assert article["status"] == "new"
    for column in (
        "local_score",
        "llm_score",
        "final_score",
        "matched_topics",
        "summary_json",
        "score_model",
        "summary_model",
    ):
        assert article[column] is None, column


def test_duplicate_urls_in_one_batch_keep_first(conn: sqlite3.Connection) -> None:
    """AC-003-15"""
    first = item("https://example.com/post?utm_source=a", title="First")
    second = item("https://EXAMPLE.com/post/#comments", title="Second")

    result = register_items(conn, [first, second], SOURCES, DAY1)

    assert [r.item.title for r in result] == ["First"]
    assert [a["title"] for a in all_articles(conn)] == ["First"]


def test_invalid_items_are_skipped_and_logged(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-003-16"""
    bad_url = item("ftp://example.com/file")
    unknown_source = item("https://example.com/other", source_id="no-such-source")
    good = item("https://example.com/good")

    with caplog.at_level(logging.WARNING, logger="aidigest.pipeline.dedupe"):
        result = register_items(conn, [bad_url, unknown_source, good], SOURCES, DAY1)

    assert [r.url for r in result] == ["https://example.com/good"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(warnings) == 1
    assert "ftp://example.com/file" in warnings[0].getMessage()
    assert "blog" in warnings[0].getMessage()
    assert len(errors) == 1
    assert "no-such-source" in errors[0].getMessage()


def test_registration_is_not_committed(conn: sqlite3.Connection) -> None:
    """AC-003-17"""
    register_items(conn, [item()], SOURCES, DAY1)
    conn.commit()
    before = all_articles(conn)

    register_items(conn, [item("https://example.com/new")], SOURCES, DAY2)
    conn.rollback()

    assert all_articles(conn) == before
