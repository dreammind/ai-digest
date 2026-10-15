"""ローカルスコアと重複話題の除去（specs/006-local-score.md）のテスト。"""

import json
import logging
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from aidigest.config import Source, TopicsConfig
from aidigest.db import open_db
from aidigest.models import FetchedItem
from aidigest.pipeline.dedupe import NewArticle, register_items
from aidigest.pipeline.scoring import local_score, score_articles, title_similarity

NOW = datetime(2026, 10, 16, 21, 30, tzinfo=UTC)
SOURCE = TypeAdapter[Source](Source)


def make_source(source_id: str, priority: int, highlight: Sequence[str] = ()) -> Source:
    return SOURCE.validate_python(
        {
            "id": source_id,
            "type": "rss",
            "url": f"https://example.com/{source_id}.xml",
            "category": "community",
            "priority": priority,
            "max_items": 10,
            "highlight_keywords": list(highlight),
        }
    )


SOURCES: dict[str, Source] = {
    "p1": make_source("p1", 1, ["tips"]),
    "p2": make_source("p2", 2),
    "p3": make_source("p3", 3),
}


def make_topics(**scoring: object) -> TopicsConfig:
    """AC-006-7 の設定（scoring の値は引数で上書きできる）。"""
    params: dict[str, object] = {
        "priority_bonus": {1: 2.0, 2: 1.0, 3: 0.0},
        "high_points": 2.0,
        "medium_points": 1.0,
        "negative_points": 3.0,
        "highlight_points": 1.0,
        "content_factor": 0.25,
        "max_hits": 3,
        "min_score": -100.0,
        "similarity_threshold": 0.45,
        "ngram": 2,
        **scoring,
    }
    return TopicsConfig.model_validate(
        {
            "scoring": params,
            "topics": [
                {"id": "A", "weight": 3.0, "high": ["hooks"], "medium": ["MCP"]},
                {"id": "B", "weight": 2.0, "high": ["LLM"]},
            ],
            "negative": ["hiring"],
        }
    )


TOPICS = make_topics()
_counter = iter(range(1_000_000))


def item(
    title: str,
    content: str = "",
    *,
    source_id: str = "p3",
    published_at: datetime | None = NOW,
) -> FetchedItem:
    return FetchedItem(
        source_id=source_id,
        url=f"https://example.com/{next(_counter)}",
        title=title,
        published_at=published_at,
        content=content,
    )


def score_of(it: FetchedItem, topics: TopicsConfig = TOPICS) -> float:
    return local_score(it, SOURCES.get(it.source_id), topics)[0]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


def register(conn: sqlite3.Connection, items: Sequence[FetchedItem]) -> list[NewArticle]:
    return register_items(conn, items, SOURCES, NOW)


def row(conn: sqlite3.Connection, article: NewArticle) -> tuple[float, list[str], str]:
    local, topics, status = conn.execute(
        "SELECT local_score, matched_topics, status FROM articles WHERE id = ?", (article.id,)
    ).fetchone()
    return float(local), json.loads(topics), str(status)


# ------------------------------------------------------------ 採点


def test_title_and_content_factors() -> None:
    """AC-006-5: テーマ A の high（hooks）だけを見る。A の点 = 3 * 2 * 倍率"""
    in_title = score_of(item("hooks"))
    in_content = score_of(item("x", "hooks"))
    in_both = score_of(item("hooks", "hooks hooks"))

    assert in_title == 6.0
    assert in_content == 6.0 * 0.25
    assert in_both == 6.0


def test_max_hits_counts_largest_factors_first() -> None:
    """AC-006-6"""
    topics = TopicsConfig.model_validate(
        {
            "scoring": TOPICS.scoring.model_dump(),
            "topics": [{"id": "T", "weight": 1.0, "high": ["a1", "a2", "b1", "b2", "b3"]}],
        }
    )

    score = score_of(item("a1 a2", "b1 b2 b3"), topics)

    assert score == 2.0 * (1.0 + 1.0 + 0.25)


def test_formula_example() -> None:
    """AC-006-7"""
    score, _ = local_score(item("hooks and MCP tips", "LLM hiring"), SOURCES["p1"], TOPICS)

    assert score == pytest.approx(12.25)


def test_matched_topics() -> None:
    """AC-006-8"""
    _, matched = local_score(item("hooks and MCP tips", "LLM hiring"), SOURCES["p1"], TOPICS)
    _, none = local_score(item("unrelated"), SOURCES["p1"], TOPICS)

    assert matched == ["A", "B"]
    assert none == []


FULLWIDTH_A_SPACE_B = "\uff41 \uff22"  # 全角の「a B」
FULLWIDTH_HOOKS = "\uff48\uff4f\uff4f\uff4b\uff53"  # 全角の「hooks」


@pytest.mark.parametrize(
    ("title", "content", "expected"),
    [
        (FULLWIDTH_HOOKS, "", 6.0),
        ("x", '<a href="hooks">link</a>', 0.0),
        ("x", "a" * 1000 + " hooks", 0.0),
    ],
    ids=["fullwidth", "inside-tag", "after-limit"],
)
def test_matching_is_same_as_prefilter(title: str, content: str, expected: float) -> None:
    """AC-006-9"""
    assert score_of(item(title, content)) == expected


def test_unknown_source_gets_no_priority_or_highlight(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-006-10"""
    with caplog.at_level(logging.WARNING, logger="aidigest.pipeline.scoring"):
        score, _ = local_score(item("tips", source_id="unknown"), None, TOPICS)

    assert score == 0.0
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


# ------------------------------------------------------------ 足切りと重複


def test_below_min_score_is_cut(conn: sqlite3.Connection) -> None:
    """AC-006-11: hooks がタイトル = 6 点、本文 = 1.5 点"""
    articles = register(conn, [item("hooks"), item("x", "hooks"), item("y")])

    kept = score_articles(conn, articles, SOURCES, make_topics(min_score=1.5))

    assert sorted(s.local_score for s in kept) == [1.5, 6.0]


def test_ordering(conn: sqlite3.Connection) -> None:
    """AC-006-12"""
    old = NOW - timedelta(days=1)
    articles = register(
        conn,
        [
            item("first same", published_at=old),
            item("no date", published_at=None),
            item("newer", published_at=NOW),
            item("second same", published_at=old),
            item("hooks top"),
        ],
    )

    kept = score_articles(conn, articles, SOURCES, make_topics(similarity_threshold=1.0))

    assert [s.article.item.title for s in kept] == [
        "hooks top",
        "newer",
        "first same",
        "second same",
        "no date",
    ]


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("Claude Code hooks", "Claude Code hooks", 1.0),
        ("abcd", "wxyz", 0.0),
        ("ab", "abc", 0.5),
        ("A-B!", FULLWIDTH_A_SPACE_B, 1.0),
    ],
    ids=["identical", "disjoint", "partial", "normalized"],
)
def test_title_similarity(a: str, b: str, expected: float) -> None:
    """AC-006-13: 4 つ目は記号・空白を除き、全角・大文字を同一視すると、どちらも ab になる"""
    assert title_similarity(a, b, 2) == pytest.approx(expected)


def test_similar_titles_keep_higher_score(conn: sqlite3.Connection) -> None:
    """AC-006-14"""
    articles = register(
        conn,
        [
            item("Claude Code hooks の使い方", source_id="p3"),
            item("Claude Code hooks の使い方を解説", source_id="p1"),
            item("まったく別の話題です", source_id="p2"),
            item("全然ちがうテーマの記事", source_id="p3"),
        ],
    )

    kept = score_articles(conn, articles, SOURCES, TOPICS)

    titles = [s.article.item.title for s in kept]
    assert "Claude Code hooks の使い方を解説" in titles
    assert "Claude Code hooks の使い方" not in titles
    assert "まったく別の話題です" in titles
    assert "全然ちがうテーマの記事" in titles


def test_symbol_only_titles_are_not_duplicates(conn: sqlite3.Connection) -> None:
    """AC-006-15"""
    articles = register(conn, [item("!!!"), item("???")])

    kept = score_articles(conn, articles, SOURCES, TOPICS)

    assert len(kept) == 2
    assert title_similarity("!!!", "???", 2) == 0.0


# ------------------------------------------------------------ 記録と出力


def test_scores_and_statuses_are_recorded_without_commit(conn: sqlite3.Connection) -> None:
    """AC-006-16"""
    kept_item = item("hooks and MCP tips", "LLM hiring", source_id="p1")
    dup_item = item("hooks and MCP tips!", source_id="p3")
    low_item = item("nothing", source_id="p3")
    articles = register(conn, [kept_item, dup_item, low_item])
    conn.commit()

    score_articles(conn, articles, SOURCES, make_topics(min_score=1.0))

    kept, dup, low = articles
    assert row(conn, kept) == (pytest.approx(12.25), ["A", "B"], "scored")
    assert row(conn, dup)[2] == "skipped"
    assert row(conn, dup)[1] == ["A"]
    assert row(conn, low) == (0.0, [], "skipped")
    conn.rollback()
    status = conn.execute("SELECT status FROM articles WHERE id = ?", (kept.id,)).fetchone()[0]
    assert status == "new"


def test_output_and_log(conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture) -> None:
    """AC-006-17"""
    articles = register(
        conn,
        [
            item("LLM news", source_id="p2"),
            item("hooks deep dive", source_id="p2"),
            item("hooks deep dive!", source_id="p3"),
            item("nothing", source_id="p3"),
        ],
    )

    with caplog.at_level(logging.INFO, logger="aidigest.pipeline.scoring"):
        kept = score_articles(conn, articles, SOURCES, make_topics(min_score=1.0))

    assert [(s.article.item.title, s.local_score, s.matched_topics) for s in kept] == [
        ("hooks deep dive", 7.0, ["A"]),
        ("LLM news", 5.0, ["B"]),
    ]
    (summary,) = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert "kept=2" in summary
    assert "below_min=1" in summary
    assert "duplicates=1" in summary
