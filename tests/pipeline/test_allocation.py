"""セクションへの振り分けと枠配分（specs/009-slot-allocation.md）のテスト。"""

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from aidigest.config import DigestConfig, Source
from aidigest.db import open_db
from aidigest.models import FetchedItem
from aidigest.pipeline.allocation import SelectedArticle, allocate, section_of
from aidigest.pipeline.dedupe import register_items
from aidigest.pipeline.rerank import RankedArticle
from aidigest.pipeline.scoring import ScoredArticle

NOW = datetime(2026, 10, 16, 21, 30, tzinfo=UTC)
DIGEST = DigestConfig()  # 既定値：8 件、claude_code 4・llm 3・other 2
SOURCES: dict[str, Source] = {
    "blog": TypeAdapter[Source](Source).validate_python(
        {
            "id": "blog",
            "type": "rss",
            "url": "https://example.com/feed",
            "category": "community",
            "priority": 2,
            "max_items": 10,
        }
    )
}

# グループを表す matched_topics
CC = ["claude_code", "llm"]
LLM = ["llm"]
OTHER: list[str] = []


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


def ranked(
    conn: sqlite3.Connection, entries: Sequence[tuple[str, list[str]]]
) -> list[RankedArticle]:
    """(タイトル, matched_topics) の並びから、final_score の高い順の RankedArticle を作る。"""
    items = [
        FetchedItem(source_id="blog", url=f"https://example.com/{i}", title=title, published_at=NOW)
        for i, (title, _) in enumerate(entries)
    ]
    articles = register_items(conn, items, SOURCES, NOW)
    conn.execute("UPDATE articles SET status = 'scored'")
    count = len(entries)
    return [
        RankedArticle(ScoredArticle(a, 1.0, topics), None, 1.0 - i / (count + 1))
        for i, (a, (_, topics)) in enumerate(zip(articles, entries, strict=True))
    ]


def titles(selected: Sequence[SelectedArticle]) -> list[str]:
    return [s.ranked.scored.article.item.title for s in selected]


def status(conn: sqlite3.Connection, article: RankedArticle) -> str:
    row = conn.execute(
        "SELECT status FROM articles WHERE id = ?", (article.scored.article.id,)
    ).fetchone()
    return str(row[0])


# ------------------------------------------------------------ 振り分け


@pytest.mark.parametrize(
    ("topics", "expected"),
    [
        (["claude_code", "llm"], "claude_code"),
        (["llm"], "llm"),
        ([], "other"),
        (["paper"], "other"),
    ],
)
def test_section_by_top_topic(topics: list[str], expected: str) -> None:
    """AC-009-4"""
    assert section_of(topics, "普通のタイトル", DIGEST) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Claude Code の新機能を試した", "claude_code_update"),
        ("Claude Code 2.2 RELEASE notes", "claude_code_update"),
        ("Claude Code の hooks 活用術", "claude_code"),
    ],
)
def test_claude_code_update_by_keywords(title: str, expected: str) -> None:
    """AC-009-5"""
    assert section_of(CC, title, DIGEST) == expected


# ------------------------------------------------------------ 枠配分


def test_limits_are_never_exceeded(conn: sqlite3.Connection) -> None:
    """AC-009-6"""
    entries = (
        [(f"cc{i}", CC) for i in range(4)]
        + [(f"llm{i}", LLM) for i in range(2)]
        + [(f"cc{i}", CC) for i in range(4, 6)]
        + [(f"other{i}", OTHER) for i in range(3)]
        + [(f"llm{i}", LLM) for i in range(2, 4)]
    )

    selected = allocate(conn, ranked(conn, entries), DIGEST)

    groups = [s.ranked.scored.matched_topics[:1] for s in selected]
    assert len(selected) == 8
    assert groups.count(["claude_code"]) <= 4
    assert groups.count(["llm"]) <= 3
    assert groups.count([]) <= 2


def test_full_group_is_skipped_and_others_fill(conn: sqlite3.Connection) -> None:
    """AC-009-7: cc は 4 件で上限。cc4・cc5 は飛ばされ、スコアの低い llm・other で埋まる"""
    entries = (
        [(f"cc{i}", CC) for i in range(6)]
        + [(f"llm{i}", LLM) for i in range(4)]
        + [(f"other{i}", OTHER) for i in range(3)]
    )

    selected = allocate(conn, ranked(conn, entries), DIGEST)

    assert sorted(titles(selected)) == sorted(
        ["cc0", "cc1", "cc2", "cc3", "llm0", "llm1", "llm2", "other0"]
    )


def test_fewer_candidates_than_size(conn: sqlite3.Connection) -> None:
    """AC-009-8: other は 2 件まで"""
    entries = [("cc0", CC), ("other0", OTHER), ("other1", OTHER), ("other2", OTHER), ("llm0", LLM)]

    selected = allocate(conn, ranked(conn, entries), DIGEST)

    assert sorted(titles(selected)) == ["cc0", "llm0", "other0", "other1"]


def test_empty_input(conn: sqlite3.Connection) -> None:
    """AC-009-9"""
    assert allocate(conn, [], DIGEST) == []


# ------------------------------------------------------------ 記録と出力


def test_status_is_updated_without_commit(conn: sqlite3.Connection) -> None:
    """AC-009-10"""
    articles = ranked(conn, [("cc0", CC), ("other0", OTHER), ("other1", OTHER), ("other2", OTHER)])
    conn.commit()

    allocate(conn, articles, DIGEST)

    assert [status(conn, a) for a in articles] == ["selected", "selected", "selected", "scored"]
    conn.rollback()
    assert [status(conn, a) for a in articles] == ["scored"] * 4


def test_output_order_and_log(conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture) -> None:
    """AC-009-11"""
    entries = [
        ("other0", OTHER),
        ("llm0", LLM),
        ("Claude Code の新機能", CC),
        ("cc-method0", CC),
        ("llm1", LLM),
        ("cc-method1", CC),
    ]

    with caplog.at_level(logging.INFO, logger="aidigest.pipeline.allocation"):
        selected = allocate(conn, ranked(conn, entries), DIGEST)

    assert [(s.section, titles([s])[0]) for s in selected] == [
        ("claude_code", "cc-method0"),
        ("claude_code", "cc-method1"),
        ("claude_code_update", "Claude Code の新機能"),
        ("llm", "llm0"),
        ("llm", "llm1"),
        ("other", "other0"),
    ]
    (summary,) = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert "claude_code=3/4" in summary
    assert "llm=2/3" in summary
    assert "other=1/2" in summary
