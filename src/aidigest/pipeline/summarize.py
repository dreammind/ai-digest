"""本文の取得と日本語の要約（specs/010-summarize.md）。

選んだ記事ごとに本文を取得・抽出し、Haiku で日本語の構造化要約を作る。
要約できなかった記事は要約なしで返す（011 で原題と原文リンクだけで配信する）。
コミットは呼び出し側が行う。
"""

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Annotated, Self, cast

import trafilatura
from anthropic.types import TextBlockParam, ToolParam
from pydantic import BaseModel, BeforeValidator, Field, StringConstraints, ValidationError

from aidigest.config import Settings
from aidigest.fetchers.http import FetchError, HttpClient
from aidigest.llm import BudgetExceeded, LlmCaller, LlmError
from aidigest.pipeline.allocation import SelectedArticle
from aidigest.pipeline.text import strip_tags
from aidigest.pipeline.tool_response import describe_validation_error, single_tool_input

logger = logging.getLogger(__name__)

TOOL_NAME = "submit_summary"
ATTEMPTS = 2  # 最初の 1 回 + 不正な応答のときの再試行 1 回

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
MAX_LIST_ITEMS = 5  # key_takeaways と topics の最大件数


def _first_items(value: object) -> object:
    """リストが長すぎたら先頭の MAX_LIST_ITEMS 件にする（件数だけで要約を捨てないため）。"""
    if isinstance(value, list):
        return cast(list[object], value)[:MAX_LIST_ITEMS]
    return value


class Summary(BaseModel):
    """要約のスキーマ（SPEC.md §6、specs/010）。"""

    title_ja: NonEmptyStr
    tldr: NonEmptyStr
    key_takeaways: Annotated[
        list[NonEmptyStr], BeforeValidator(_first_items), Field(min_length=1, max_length=5)
    ]
    summary: NonEmptyStr
    topics: Annotated[list[str], BeforeValidator(_first_items), Field(max_length=5)]
    novelty: Annotated[int, Field(ge=1, le=5)]
    apply_to_claude_code: str


def _string(description: str) -> dict[str, object]:
    return {"type": "string", "description": description}


TOOL_DEFINITION: ToolParam = {
    "name": TOOL_NAME,
    "description": "記事の日本語の要約を提出する。",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "title_ja": _string("日本語のタイトル"),
            "tldr": _string("1 文の要約"),
            "key_takeaways": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要点（1〜5 個）",
            },
            "summary": _string("2〜3 文の要約"),
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "話題のキーワード（5 個まで）",
            },
            "novelty": {"type": "integer", "description": "新しさ（1〜5）"},
            "apply_to_claude_code": _string("Claude Code での活かし方。無ければ空"),
        },
        "required": [
            "title_ja",
            "tldr",
            "key_takeaways",
            "summary",
            "topics",
            "novelty",
            "apply_to_claude_code",
        ],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class SummaryParams:
    body_chars: int
    max_tokens: int

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(body_chars=settings.summary_body_chars, max_tokens=settings.summary_max_tokens)


@dataclass(frozen=True)
class SummarizedArticle:
    selected: SelectedArticle
    summary: Summary | None


def summarize_articles(
    conn: sqlite3.Connection,
    selected: Sequence[SelectedArticle],
    llm: LlmCaller | None,
    http: HttpClient,
    params: SummaryParams,
) -> list[SummarizedArticle]:
    """選んだ記事を 1 件ずつ要約し、入力と同じ順で返す。"""
    if llm is None and selected:
        logger.warning("LLM が使えないため、要約せずにタイトルと原文リンクだけで続けます")

    results: list[SummarizedArticle] = []
    budget_exceeded = False
    for article in selected:
        summary = None
        if llm is not None and not budget_exceeded:
            try:
                summary = _summarize_one(llm, http, article, params)
            except BudgetExceeded as e:
                logger.warning("予算の上限に達したため、残りの記事は要約しません: %s", e)
                budget_exceeded = True
            except LlmError as e:
                logger.warning("要約に失敗しました（%s）: %s", _url(article), e)
        if summary is not None:
            _record(conn, article, summary, llm.model if llm is not None else "")
        results.append(SummarizedArticle(article, summary))

    done = sum(1 for r in results if r.summary is not None)
    logger.info("要約: summarized=%d not_summarized=%d", done, len(results) - done)
    return results


def _summarize_one(
    llm: LlmCaller, http: HttpClient, article: SelectedArticle, params: SummaryParams
) -> Summary | None:
    body = _body(http, article, params.body_chars)
    if not body:
        logger.warning("本文が取れないため要約しません（%s）", _url(article))
        return None

    content = _user_message(article, body)
    for attempt in range(1, ATTEMPTS + 1):
        response = llm.create(
            "summarize",
            messages=[{"role": "user", "content": content}],
            max_tokens=params.max_tokens,
            system=[_system_block()],
            tools=[TOOL_DEFINITION],
            tool_choice={"type": "auto"},
            output_config={"effort": "low"},
        )
        tool_input, problem = single_tool_input(response, TOOL_NAME)
        if tool_input is not None:
            try:
                return Summary.model_validate(tool_input)
            except ValidationError as e:
                problem = f"スキーマ違反: {describe_validation_error(e)}"
        logger.warning("要約の応答が不正です（%d 回目、%s）: %s", attempt, _url(article), problem)
    logger.warning("要約の応答が %d 回とも不正なため要約しません（%s）", ATTEMPTS, _url(article))
    return None


def _body(http: HttpClient, article: SelectedArticle, chars: int) -> str:
    """元の記事から本文を抽出する。取れなければフィードの本文で代える。"""
    text = ""
    try:
        response = http.get(_url(article))
        text = trafilatura.extract(response.text) or ""
    except FetchError as e:
        logger.warning("本文を取得できません（%s）: %s", _url(article), e)
    if not text.strip():
        text = " ".join(strip_tags(article.ranked.scored.article.item.content).split())
        if text:
            logger.warning("本文を抽出できないため、フィードの本文を使います（%s）", _url(article))
    return text[:chars]


def _user_message(article: SelectedArticle, body: str) -> str:
    item = article.ranked.scored.article.item
    return f"出典: {item.source_id}\n原題: {item.title}\nURL: {_url(article)}\n\n本文:\n{body}"


def _system_block() -> TextBlockParam:
    prompt = (
        resources.files("aidigest.prompts")
        .joinpath("summarize_system.md")
        .read_text(encoding="utf-8")
    )
    return {"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}


def _url(article: SelectedArticle) -> str:
    return article.ranked.scored.article.url


def _record(
    conn: sqlite3.Connection, article: SelectedArticle, summary: Summary, model: str
) -> None:
    conn.execute(
        "UPDATE articles SET summary_json = ?, summary_model = ?, status = 'summarized'"
        " WHERE id = ?",
        (
            json.dumps(summary.model_dump(), ensure_ascii=False),
            model,
            article.ranked.scored.article.id,
        ),
    )
