"""LLM の再ランク（specs/008-rerank.md）のテスト。LLM は偽物に差し替え、実際の API には出ない。"""

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Unpack

import pytest
from anthropic.types import Message, MessageParam
from pydantic import TypeAdapter

from aidigest.config import Source
from aidigest.db import open_db
from aidigest.llm import BudgetExceeded, CreateOptions, LlmError, Purpose
from aidigest.models import FetchedItem
from aidigest.pipeline.dedupe import register_items
from aidigest.pipeline.rerank import RankedArticle, RerankParams, rerank
from aidigest.pipeline.scoring import ScoredArticle

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 16, 21, 30, tzinfo=UTC)
MODEL = "claude-haiku-5-5"
PARAMS = RerankParams(top_n=30, excerpt_chars=200, llm_weight=0.5, max_tokens=4000)
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


class FakeLlm:
    """`LlmClient` の偽物。決まった順に応答を返すか、例外を送出する。"""

    model = MODEL

    def __init__(self, outcomes: Sequence[Message | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def create(
        self,
        purpose: Purpose,
        *,
        messages: Iterable[MessageParam],
        max_tokens: int,
        **options: Unpack[CreateOptions],
    ) -> Message:
        self.calls.append(
            {"purpose": purpose, "messages": list(messages), "max_tokens": max_tokens, **options}
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def message(content: Sequence[Mapping[str, object]], stop_reason: str = "tool_use") -> Message:
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
    )


def tool_reply(scores: list[dict[str, object]], stop_reason: str = "tool_use") -> Message:
    block = {
        "type": "tool_use",
        "id": "toolu_test",
        "name": "submit_relevance",
        "input": {"scores": scores},
    }
    return message([block], stop_reason)


def scores_for(relevances: Sequence[float]) -> list[dict[str, object]]:
    return [{"id": i, "relevance": r, "reason": "理由"} for i, r in enumerate(relevances, start=1)]


def valid(relevances: Sequence[float]) -> Message:
    return tool_reply(scores_for(relevances))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


def scored(
    conn: sqlite3.Connection, local_scores: Sequence[float], contents: Sequence[str] = ()
) -> list[ScoredArticle]:
    """ローカルスコアの高い順（入力の順）の ScoredArticle を作る。"""
    items = [
        FetchedItem(
            source_id="blog",
            url=f"https://example.com/{i}",
            title=f"Title {i}",
            published_at=NOW,
            content=contents[i] if i < len(contents) else "",
        )
        for i in range(len(local_scores))
    ]
    articles = register_items(conn, items, SOURCES, NOW)
    return [ScoredArticle(a, s, []) for a, s in zip(articles, local_scores, strict=True)]


def titles(ranked: Sequence[RankedArticle]) -> list[str]:
    return [r.scored.article.item.title for r in ranked]


def user_text(llm: FakeLlm, call: int = 0) -> str:
    messages = llm.calls[call]["messages"]
    assert isinstance(messages, list)
    (first,) = messages
    assert isinstance(first, dict)
    content = first["content"]
    assert isinstance(content, str)
    return content


# ------------------------------------------------------------ リクエスト


def test_request_contains_top_n_with_settings(conn: sqlite3.Connection) -> None:
    """AC-008-2"""
    articles = scored(conn, [100 - i for i in range(35)])
    llm = FakeLlm([valid([0.5] * 30)])

    rerank(conn, articles, llm, PARAMS)

    (call,) = llm.calls
    text = user_text(llm)
    assert "[1] 出典: blog" in text
    assert "タイトル: Title 0" in text
    assert "[30]" in text
    assert "[31]" not in text
    assert "Title 30" not in text
    system_prompt = (PROJECT_ROOT / "src/aidigest/prompts/rerank_system.md").read_text()
    assert call["system"] == system_prompt
    tools = call["tools"]
    assert isinstance(tools, list)
    (tool,) = tools
    assert isinstance(tool, dict)
    assert tool["name"] == "submit_relevance"
    assert tool["strict"] is True
    assert call["tool_choice"] == {"type": "auto"}
    assert call["output_config"] == {"effort": "low"}
    assert call["max_tokens"] == 4000
    assert call["purpose"] == "rerank"


def test_excerpt_is_cleaned_and_truncated(conn: sqlite3.Connection) -> None:
    """AC-008-3"""
    articles = scored(
        conn,
        [3, 2, 1],
        ["<p>Hello</p>\n\n   world  ", "0123456789ABCDEF", ""],
    )
    llm = FakeLlm([valid([0.5] * 3)])

    rerank(
        conn,
        articles,
        llm,
        RerankParams(top_n=30, excerpt_chars=10, llm_weight=0.5, max_tokens=4000),
    )

    text = user_text(llm)
    assert "抜粋: Hello world\n" in text
    assert "抜粋: 0123456789\n" in text
    assert "抜粋: なし" in text


# ------------------------------------------------------------ 結果


def test_valid_response_is_mixed_and_sorted(conn: sqlite3.Connection) -> None:
    """AC-008-4: final は 0.75・0.75・0.7・0.85。同じ final はローカルスコアの順（入力の順）"""
    articles = scored(conn, [10, 10, 4, 8])
    llm = FakeLlm([valid([0.5, 0.5, 1.0, 0.9])])

    ranked = rerank(conn, articles, llm, PARAMS)

    assert titles(ranked) == ["Title 3", "Title 0", "Title 1", "Title 2"]
    assert [r.llm_score for r in ranked] == [0.9, 0.5, 0.5, 1.0]
    assert [r.final_score for r in ranked] == pytest.approx([0.85, 0.75, 0.75, 0.7])


def test_formula_example(conn: sqlite3.Connection) -> None:
    """AC-008-5"""
    articles = scored(conn, [10, 5, -2])
    llm = FakeLlm([valid([0.2, 0.9, 1.0])])

    ranked = rerank(conn, articles, llm, PARAMS)

    assert titles(ranked) == ["Title 1", "Title 0", "Title 2"]
    assert [r.final_score for r in ranked] == pytest.approx([0.7, 0.6, 0.5])


INVALID_FIRST = {
    "no-tool": message([{"type": "text", "text": "すみません"}], "end_turn"),
    "out-of-range": tool_reply(scores_for([1.5, 0.5])),
    "missing-id": tool_reply(scores_for([0.5])),
    "duplicate-id": tool_reply(
        [{"id": 1, "relevance": 0.5, "reason": "a"}, {"id": 1, "relevance": 0.5, "reason": "b"}]
    ),
    "extra-id": tool_reply(scores_for([0.5, 0.5, 0.5])),
    "max-tokens": tool_reply(scores_for([0.5, 0.5]), "max_tokens"),
    "refusal": message([{"type": "text", "text": ""}], "refusal"),
    "not-a-number": tool_reply([{"id": 1, "relevance": "high", "reason": "a"}]),
}


@pytest.mark.parametrize("bad", list(INVALID_FIRST.values()), ids=list(INVALID_FIRST))
def test_invalid_response_is_retried_once(conn: sqlite3.Connection, bad: Message) -> None:
    """AC-008-6"""
    articles = scored(conn, [2, 1])
    llm = FakeLlm([bad, valid([0.1, 0.9])])

    ranked = rerank(conn, articles, llm, PARAMS)

    assert len(llm.calls) == 2
    assert [r.llm_score for r in ranked] == [0.9, 0.1]


def test_two_invalid_responses_fall_back(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-008-7"""
    articles = scored(conn, [4, 2])
    bad = INVALID_FIRST["no-tool"]
    llm = FakeLlm([bad, bad])

    with caplog.at_level(logging.WARNING, logger="aidigest.pipeline.rerank"):
        ranked = rerank(conn, articles, llm, PARAMS)

    assert len(llm.calls) == 2
    assert titles(ranked) == ["Title 0", "Title 1"]
    assert [r.llm_score for r in ranked] == [None, None]
    assert [r.final_score for r in ranked] == pytest.approx([1.0, 0.5])
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize(
    "error", [BudgetExceeded("予算"), LlmError("失敗")], ids=["budget", "llm-error"]
)
def test_budget_and_api_errors_fall_back_without_retry(
    conn: sqlite3.Connection, error: Exception
) -> None:
    """AC-008-8"""
    articles = scored(conn, [4, 2])
    llm = FakeLlm([error])

    ranked = rerank(conn, articles, llm, PARAMS)

    assert len(llm.calls) == 1
    assert [r.llm_score for r in ranked] == [None, None]
    assert titles(ranked) == ["Title 0", "Title 1"]


def test_no_llm_falls_back(conn: sqlite3.Connection) -> None:
    """AC-008-9"""
    ranked = rerank(conn, scored(conn, [4, 2]), None, PARAMS)

    assert [r.final_score for r in ranked] == pytest.approx([1.0, 0.5])


def test_empty_input_does_not_call(conn: sqlite3.Connection) -> None:
    """AC-008-10"""
    llm = FakeLlm([])

    assert rerank(conn, [], llm, PARAMS) == []
    assert llm.calls == []


# ------------------------------------------------------------ 記録


def row(conn: sqlite3.Connection, ranked: RankedArticle) -> tuple[object, ...]:
    return tuple(
        conn.execute(
            "SELECT llm_score, final_score, score_model, status FROM articles WHERE id = ?",
            (ranked.scored.article.id,),
        ).fetchone()
    )


def test_scores_are_recorded_without_commit(conn: sqlite3.Connection) -> None:
    """AC-008-11"""
    articles = scored(conn, [10, 5])
    conn.execute("UPDATE articles SET status = 'scored'")
    conn.commit()
    llm = FakeLlm([valid([0.2, 0.9])])

    ranked = rerank(conn, articles, llm, PARAMS)

    first, second = ranked
    assert row(conn, first) == (0.9, pytest.approx(0.7), MODEL, "scored")
    assert row(conn, second) == (0.2, pytest.approx(0.6), MODEL, "scored")
    conn.rollback()
    assert row(conn, first) == (None, None, None, "scored")


def test_fallback_records_nulls(conn: sqlite3.Connection) -> None:
    """AC-008-11: 縮退時は llm_score と score_model が NULL"""
    (ranked,) = rerank(conn, scored(conn, [3]), None, PARAMS)

    assert row(conn, ranked) == (None, 1.0, None, "new")


def test_non_positive_local_scores_normalize_to_zero(conn: sqlite3.Connection) -> None:
    """AC-008-12"""
    ranked = rerank(conn, scored(conn, [0, -1]), None, PARAMS)

    assert [r.final_score for r in ranked] == [0.0, 0.0]
