"""事前の絞り込み（specs/005-prefilter.md）のテスト。"""

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from aidigest.config import Source
from aidigest.db import open_db
from aidigest.models import FetchedItem
from aidigest.pipeline.dedupe import NewArticle, register_items
from aidigest.pipeline.prefilter import prefilter

NOW = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)
SOURCE = TypeAdapter[Source](Source)
BASE: dict[str, object] = {"category": "community", "priority": 2, "max_items": 10}


def make_sources() -> dict[str, Source]:
    entries: list[dict[str, object]] = [
        {"id": "blog", "type": "rss", "url": "https://example.com/feed", **BASE},
        {
            "id": "hidden",
            "type": "rss",
            "url": "https://example.com/feed2",
            "hide_keywords": ["sponsored"],
            **BASE,
        },
        {
            "id": "required",
            "type": "rss",
            "url": "https://example.com/feed3",
            "require_keywords": ["Claude", "LLM"],
            **BASE,
        },
        {"id": "qiita", "type": "qiita_search", "query": "tag:LLM", **BASE},
        {"id": "zenn", "type": "zenn_topic", "topic": "llm", **BASE},
    ]
    sources = [SOURCE.validate_python(e) for e in entries]
    return {s.id: s for s in sources}


SOURCES = make_sources()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


_counter = iter(range(1_000_000))


def item(
    *,
    source_id: str = "blog",
    title: str = "Title",
    content: str = "",
    age_hours: float | None = 1,
) -> FetchedItem:
    published = None if age_hours is None else NOW - timedelta(hours=age_hours)
    return FetchedItem(
        source_id=source_id,
        url=f"https://example.com/{next(_counter)}",
        title=title,
        published_at=published,
        content=content,
    )


def run(
    conn: sqlite3.Connection,
    items: Sequence[FetchedItem],
    exclude_keywords: Sequence[str] = (),
) -> tuple[list[NewArticle], list[NewArticle]]:
    """記事を登録して絞り込み、(通った記事, 入力した記事) を返す。"""
    articles = register_items(conn, items, SOURCES, NOW)
    passed = prefilter(conn, articles, SOURCES, exclude_keywords, NOW, time_window_hours=36)
    return passed, articles


def status(conn: sqlite3.Connection, article: NewArticle) -> str:
    return str(
        conn.execute("SELECT status FROM articles WHERE id = ?", (article.id,)).fetchone()[0]
    )


def titles(articles: Sequence[NewArticle]) -> list[str]:
    return [a.item.title for a in articles]


# ------------------------------------------------------------ 時間窓


def test_time_window(conn: sqlite3.Connection) -> None:
    """AC-005-1"""
    passed, _ = run(
        conn,
        [
            item(title="35h", age_hours=35),
            item(title="36h", age_hours=36),
            item(title="37h", age_hours=37),
        ],
    )

    assert titles(passed) == ["35h", "36h"]


@pytest.mark.parametrize("source_id", ["qiita", "zenn"])
def test_search_sources_are_exempt_from_time_window(
    conn: sqlite3.Connection, source_id: str
) -> None:
    """AC-005-2"""
    passed, _ = run(conn, [item(source_id=source_id, title="old", age_hours=24 * 5)])

    assert titles(passed) == ["old"]


def test_missing_published_at_passes_time_window(conn: sqlite3.Connection) -> None:
    """AC-005-3"""
    passed, _ = run(conn, [item(title="no date", age_hours=None)])

    assert titles(passed) == ["no date"]


# ------------------------------------------------------------ キーワード


def test_global_exclude_keywords(conn: sqlite3.Connection) -> None:
    """AC-005-4"""
    passed, _ = run(
        conn,
        [
            item(title="We are hiring engineers"),
            item(title="Plain", content="<p>求人のお知らせ</p>"),
            item(title="Keep"),
        ],
        exclude_keywords=["hiring", "求人"],
    )

    assert titles(passed) == ["Keep"]


def test_hide_keywords_apply_only_to_their_source(conn: sqlite3.Connection) -> None:
    """AC-005-5"""
    passed, _ = run(
        conn,
        [
            item(source_id="hidden", title="A sponsored post"),
            item(source_id="blog", title="B sponsored post"),
        ],
    )

    assert titles(passed) == ["B sponsored post"]


def test_require_keywords(conn: sqlite3.Connection) -> None:
    """AC-005-6"""
    passed, _ = run(
        conn,
        [
            item(source_id="required", title="Nothing relevant"),
            item(source_id="required", title="Python tips", content="Using an LLM for tests"),
            item(source_id="blog", title="Nothing relevant either"),
        ],
    )

    assert titles(passed) == ["Python tips", "Nothing relevant either"]


FULLWIDTH_CLAUDE = "\uff23\uff4c\uff41\uff55\uff44\uff45"  # 全角の「Claude」


@pytest.mark.parametrize("title", ["CLAUDE news", f"{FULLWIDTH_CLAUDE} の新機能"])
def test_matching_ignores_case_and_width(conn: sqlite3.Connection, title: str) -> None:
    """AC-005-7"""
    passed, _ = run(conn, [item(title=title)], exclude_keywords=["claude"])

    assert passed == []


def test_html_tags_and_text_after_limit_are_not_matched(conn: sqlite3.Connection) -> None:
    """AC-005-8"""
    passed, _ = run(
        conn,
        [
            item(title="tag only", content='<div class="x">hello</div>'),
            item(title="late keyword", content="a" * 1000 + " div"),
            item(title="early keyword", content="a" * 990 + " div"),
        ],
        exclude_keywords=["div"],
    )

    assert titles(passed) == ["tag only", "late keyword"]


# ------------------------------------------------------------ 記録と出力


def test_excluded_articles_are_marked_skipped_without_commit(conn: sqlite3.Connection) -> None:
    """AC-005-9"""
    articles = register_items(
        conn, [item(title="old", age_hours=48), item(title="new")], SOURCES, NOW
    )
    conn.commit()

    passed = prefilter(conn, articles, SOURCES, [], NOW, time_window_hours=36)

    old, new = articles
    assert titles(passed) == ["new"]
    assert status(conn, old) == "skipped"
    assert status(conn, new) == "new"
    conn.rollback()
    assert status(conn, old) == "new"


def test_order_is_kept_and_counts_are_logged(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-005-10"""
    with caplog.at_level(logging.INFO, logger="aidigest.pipeline.prefilter"):
        passed, _ = run(
            conn,
            [
                item(title="first"),
                item(title="old and hiring", age_hours=48),
                item(title="hiring"),
                item(source_id="hidden", title="sponsored"),
                item(source_id="required", title="unrelated"),
                item(title="last"),
            ],
            exclude_keywords=["hiring"],
        )

    assert titles(passed) == ["first", "last"]
    (summary,) = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert "time_window=1" in summary
    assert "exclude_keywords=1" in summary
    assert "hide_keywords=1" in summary
    assert "require_keywords=1" in summary
    assert "passed=2" in summary


def test_unknown_source_gets_only_common_rules(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """エラー時の挙動（設定に無いソース）: ソース別ルールは適用せず、WARNING を出す"""
    articles = register_items(
        conn, [item(source_id="required", title="unrelated"), item(title="hiring")], SOURCES, NOW
    )
    others = {k: v for k, v in SOURCES.items() if k != "required"}

    with caplog.at_level(logging.WARNING, logger="aidigest.pipeline.prefilter"):
        passed = prefilter(conn, articles, others, ["hiring"], NOW, time_window_hours=36)

    assert titles(passed) == ["unrelated"], "require_keywords は適用されない"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "required" in warnings[0]
