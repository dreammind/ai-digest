"""本文の取得と日本語の要約（specs/010-summarize.md）のテスト。

本文の取得は httpx.MockTransport、LLM は偽物に差し替え、外部にはアクセスしない。
"""

import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from anthropic.types import Message
from pydantic import TypeAdapter

from aidigest.config import Source, load_settings
from aidigest.db import open_db
from aidigest.fetchers.http import HttpClient
from aidigest.llm import BudgetExceeded, LlmError
from aidigest.models import FetchedItem
from aidigest.pipeline.allocation import SelectedArticle
from aidigest.pipeline.dedupe import register_items
from aidigest.pipeline.rerank import RankedArticle
from aidigest.pipeline.scoring import ScoredArticle
from aidigest.pipeline.summarize import (
    TOOL_DEFINITION,
    SummarizedArticle,
    SummaryParams,
    summarize_articles,
)
from tests.fetchers.helpers import make_client
from tests.llm_fakes import MODEL, FakeLlm, text_reply, tool_reply

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 16, 21, 30, tzinfo=UTC)
PARAMS = SummaryParams(body_chars=4000, max_tokens=4000)
PARAGRAPH = (
    "Claude Code の hooks を使うと、ツールの実行前後に任意のコマンドを走らせることができる。" * 4
)
ARTICLE_HTML = (
    "<html><body><nav>メニュー</nav><article><h1>見出し</h1>"
    f"<p>{PARAGRAPH}</p><p>二つ目の段落です。{PARAGRAPH}</p></article></body></html>"
)
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

VALID_SUMMARY: dict[str, object] = {
    "title_ja": "Claude Code の hooks 入門",
    "tldr": "hooks でツールの前後に処理を差し込める。",
    "key_takeaways": ["PreToolUse で止められる", "PostToolUse で検査できる"],
    "summary": "hooks の使い方を解説した記事。実行前後にコマンドを走らせる例を示す。",
    "topics": ["hooks", "Claude Code"],
    "novelty": 3,
    "apply_to_claude_code": "編集後に ruff を自動で走らせる。",
}


def summary_reply(**overrides: object) -> Message:
    return tool_reply("submit_summary", {**VALID_SUMMARY, **overrides})


def without(key: str) -> Message:
    return tool_reply("submit_summary", {k: v for k, v in VALID_SUMMARY.items() if k != key})


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


class FakeSites:
    """URL のパスごとに決まった HTML（または状態コード）を返すサイト。"""

    def __init__(self, pages: Mapping[str, str | int]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        page = self.pages.get(request.url.path, ARTICLE_HTML)
        if isinstance(page, int):
            return httpx.Response(page)
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})


def client(sites: FakeSites | None = None) -> HttpClient:
    return make_client(sites or FakeSites({}), user_agent="aidigest-test/1.0")


def selected(
    conn: sqlite3.Connection, count: int, contents: Sequence[str] = ()
) -> list[SelectedArticle]:
    items = [
        FetchedItem(
            source_id="blog",
            url=f"https://example.com/a{i}",
            title=f"Original title {i}",
            published_at=NOW,
            content=contents[i] if i < len(contents) else "",
        )
        for i in range(count)
    ]
    articles = register_items(conn, items, SOURCES, NOW)
    conn.execute("UPDATE articles SET status = 'selected'")
    return [
        SelectedArticle(
            RankedArticle(ScoredArticle(a, 1.0, ["claude_code"]), 0.5, 0.5), "claude_code"
        )
        for a in articles
    ]


def row(conn: sqlite3.Connection, article: SummarizedArticle) -> tuple[object, ...]:
    return tuple(
        conn.execute(
            "SELECT summary_json, summary_model, status FROM articles WHERE id = ?",
            (article.selected.ranked.scored.article.id,),
        ).fetchone()
    )


def body_of(user_text: str) -> str:
    return user_text.split("本文:\n", 1)[1]


# ------------------------------------------------------------ 設定と本文


def test_params_from_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-010-1"""
    for name in ["SUMMARY_BODY_CHARS", "SUMMARY_MAX_TOKENS"]:
        monkeypatch.delenv(name, raising=False)
    settings = load_settings(tmp_path / "no-such.env")

    assert (settings.summary_body_chars, settings.summary_max_tokens) == (4000, 4000)
    assert SummaryParams.from_settings(settings) == SummaryParams(4000, 4000)


def test_body_is_fetched_extracted_and_truncated(conn: sqlite3.Connection) -> None:
    """AC-010-2"""
    sites = FakeSites({})
    llm = FakeLlm([summary_reply()])

    summarize_articles(conn, selected(conn, 1), llm, client(sites), SummaryParams(50, 4000))

    (request,) = sites.requests
    assert str(request.url) == "https://example.com/a0"
    assert request.headers["user-agent"] == "aidigest-test/1.0"
    body = body_of(llm.user_text())
    assert len(body) == 50
    assert body.startswith("見出し\nClaude Code の hooks")
    assert "メニュー" not in body


@pytest.mark.parametrize("page", [404, "<html><body></body></html>"], ids=["404", "no-body"])
def test_feed_content_is_used_when_body_unavailable(
    conn: sqlite3.Connection, page: str | int
) -> None:
    """AC-010-3"""
    llm = FakeLlm([summary_reply()])
    articles = selected(conn, 1, ["<p>フィードの  本文</p>\n<p>です</p>"])

    summarize_articles(conn, articles, llm, client(FakeSites({"/a0": page})), PARAMS)

    assert body_of(llm.user_text()) == "フィードの 本文 です"


def test_no_body_at_all_skips_llm(conn: sqlite3.Connection) -> None:
    """AC-010-3"""
    llm = FakeLlm([])

    (result,) = summarize_articles(
        conn, selected(conn, 1), llm, client(FakeSites({"/a0": 404})), PARAMS
    )

    assert llm.calls == []
    assert result.summary is None


# ------------------------------------------------------------ リクエストと結果


def test_request(conn: sqlite3.Connection) -> None:
    """AC-010-4"""
    llm = FakeLlm([summary_reply()])

    summarize_articles(conn, selected(conn, 1), llm, client(), PARAMS)

    (call,) = llm.calls
    prompt = (PROJECT_ROOT / "src/aidigest/prompts/summarize_system.md").read_text()
    assert call.options.get("system") == [
        {"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}
    ]
    assert list(call.options.get("tools", [])) == [TOOL_DEFINITION]
    assert TOOL_DEFINITION["name"] == "submit_summary"
    assert TOOL_DEFINITION.get("strict") is True
    assert call.options.get("tool_choice") == {"type": "auto"}
    assert call.options.get("output_config") == {"effort": "low"}
    assert call.max_tokens == 4000
    assert call.purpose == "summarize"
    text = llm.user_text()
    assert "出典: blog\n" in text
    assert "原題: Original title 0\n" in text
    assert "URL: https://example.com/a0\n" in text
    assert "本文:\n見出し" in text


def test_valid_summary_is_recorded(conn: sqlite3.Connection) -> None:
    """AC-010-5"""
    llm = FakeLlm([summary_reply(title_ja="一つ目"), summary_reply(title_ja="二つ目")])

    results = summarize_articles(conn, selected(conn, 2), llm, client(), PARAMS)

    assert [r.summary.title_ja if r.summary else None for r in results] == ["一つ目", "二つ目"]
    first = results[0]
    assert first.summary is not None
    assert first.summary.model_dump() == {**VALID_SUMMARY, "title_ja": "一つ目"}
    summary_json, model, status = row(conn, first)
    assert isinstance(summary_json, str)
    assert json.loads(summary_json) == {**VALID_SUMMARY, "title_ja": "一つ目"}
    assert (model, status) == (MODEL, "summarized")


INVALID = {
    "no-tool": text_reply(),
    "missing-title": without("title_ja"),
    "novelty-6": summary_reply(novelty=6),
    "empty-tldr": summary_reply(tldr=""),
    "empty-takeaways": summary_reply(key_takeaways=[]),
    "takeaways-not-list": summary_reply(key_takeaways="要点"),
    "max-tokens": tool_reply("submit_summary", VALID_SUMMARY, "max_tokens"),
    "refusal": text_reply("", "refusal"),
}


@pytest.mark.parametrize("bad", list(INVALID.values()), ids=list(INVALID))
def test_invalid_response_is_retried_once(conn: sqlite3.Connection, bad: Message) -> None:
    """AC-010-6"""
    llm = FakeLlm([bad, summary_reply(title_ja="再試行")])

    (result,) = summarize_articles(conn, selected(conn, 1), llm, client(), PARAMS)

    assert len(llm.calls) == 2
    assert result.summary is not None
    assert result.summary.title_ja == "再試行"


def test_two_invalid_responses_give_no_summary(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-010-7"""
    llm = FakeLlm([text_reply(), text_reply(), summary_reply()])

    with caplog.at_level(logging.WARNING, logger="aidigest.pipeline.summarize"):
        first, second = summarize_articles(conn, selected(conn, 2), llm, client(), PARAMS)

    assert first.summary is None
    assert row(conn, first) == (None, None, "selected")
    assert second.summary is not None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_budget_exceeded_stops_remaining(conn: sqlite3.Connection) -> None:
    """AC-010-8"""
    sites = FakeSites({})
    llm = FakeLlm([summary_reply(), BudgetExceeded("予算")])

    results = summarize_articles(conn, selected(conn, 3), llm, client(sites), PARAMS)

    assert [r.summary is not None for r in results] == [True, False, False]
    assert len(llm.calls) == 2
    assert [r.url.path for r in sites.requests] == ["/a0", "/a1"]


def test_llm_error_skips_only_that_article(conn: sqlite3.Connection) -> None:
    """AC-010-9"""
    llm = FakeLlm([summary_reply(), LlmError("失敗"), summary_reply()])

    results = summarize_articles(conn, selected(conn, 3), llm, client(), PARAMS)

    assert [r.summary is not None for r in results] == [True, False, True]


def test_no_llm_fetches_nothing(conn: sqlite3.Connection) -> None:
    """AC-010-10"""
    sites = FakeSites({})

    results = summarize_articles(conn, selected(conn, 2), None, client(sites), PARAMS)

    assert [r.summary for r in results] == [None, None]
    assert sites.requests == []


def test_not_committed(conn: sqlite3.Connection) -> None:
    """AC-010-11"""
    articles = selected(conn, 1)
    conn.commit()

    (result,) = summarize_articles(conn, articles, FakeLlm([summary_reply()]), client(), PARAMS)

    assert row(conn, result)[2] == "summarized"
    conn.rollback()
    assert row(conn, result) == (None, None, "selected")


def test_empty_input(conn: sqlite3.Connection) -> None:
    """AC-010-12"""
    llm = FakeLlm([])

    assert summarize_articles(conn, [], llm, client(), PARAMS) == []
    assert llm.calls == []


def test_long_lists_are_truncated(conn: sqlite3.Connection) -> None:
    """AC-010-13"""
    llm = FakeLlm(
        [
            summary_reply(
                key_takeaways=[f"要点{i}" for i in range(7)], topics=[f"t{i}" for i in range(6)]
            )
        ]
    )

    (result,) = summarize_articles(conn, selected(conn, 1), llm, client(), PARAMS)

    assert len(llm.calls) == 1
    assert result.summary is not None
    assert result.summary.key_takeaways == [f"要点{i}" for i in range(5)]
    assert result.summary.topics == [f"t{i}" for i in range(5)]


def test_schema_violation_log_names_the_field(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-010-14"""
    llm = FakeLlm([summary_reply(novelty=6), summary_reply(novelty=6)])

    with caplog.at_level(logging.WARNING, logger="aidigest.pipeline.summarize"):
        summarize_articles(conn, selected(conn, 1), llm, client(), PARAMS)

    violations = [r.getMessage() for r in caplog.records if "スキーマ違反" in r.getMessage()]
    assert violations
    assert all("novelty" in message for message in violations)


LITERAL_NEWLINE = "\\n"  # バックスラッシュと n の 2 文字（本物の改行ではない）


def test_literal_newlines_become_real_newlines(conn: sqlite3.Connection) -> None:
    """AC-010-15"""
    summary_text = f"一文目。{LITERAL_NEWLINE}{LITERAL_NEWLINE}二文目。"
    apply_text = f"活用一。{LITERAL_NEWLINE}活用二。"
    llm = FakeLlm([summary_reply(summary=summary_text, apply_to_claude_code=apply_text)])

    (result,) = summarize_articles(conn, selected(conn, 1), llm, client(), PARAMS)

    assert result.summary is not None
    assert result.summary.summary.splitlines() == ["一文目。", "", "二文目。"]
    assert result.summary.apply_to_claude_code == "活用一。\n活用二。"
    stored, _, _ = row(conn, result)
    assert isinstance(stored, str)
    assert json.loads(stored)["summary"] == "一文目。\n\n二文目。"
