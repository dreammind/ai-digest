"""LLM の再ランク（specs/008-rerank.md）。

ローカルスコアの上位の記事を 1 回の LLM 呼び出しで判定させ、ローカルスコアと混ぜて並べ直す。
LLM が使えない・失敗したときは、ローカルスコアの順のまま続ける。コミットは呼び出し側が行う。
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Annotated, Self

from anthropic.types import Message, ToolParam
from pydantic import BaseModel, Field, ValidationError

from aidigest.config import Settings
from aidigest.llm import BudgetExceeded, LlmCaller, LlmError
from aidigest.pipeline.scoring import ScoredArticle
from aidigest.pipeline.text import content_excerpt
from aidigest.pipeline.tool_response import describe_validation_error, single_tool_input

logger = logging.getLogger(__name__)

TOOL_NAME = "submit_relevance"
NO_EXCERPT = "なし"
ATTEMPTS = 2  # 最初の 1 回 + 不正な応答のときの再試行 1 回

TOOL_DEFINITION: ToolParam = {
    "name": TOOL_NAME,
    "description": "各記事の読者にとっての関連度（0.0〜1.0）と、5 語以内の理由を提出する。",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "記事の番号"},
                        "relevance": {"type": "number", "description": "0.0〜1.0"},
                        "reason": {"type": "string", "description": "5 語以内の日本語"},
                    },
                    "required": ["id", "relevance", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["scores"],
        "additionalProperties": False,
    },
}


class _Score(BaseModel):
    id: int
    relevance: Annotated[float, Field(ge=0, le=1)]
    reason: str


class _Submission(BaseModel):
    scores: list[_Score]


@dataclass(frozen=True)
class RerankParams:
    top_n: int
    excerpt_chars: int
    llm_weight: float
    max_tokens: int

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            top_n=settings.rerank_top_n,
            excerpt_chars=settings.rerank_excerpt_chars,
            llm_weight=settings.rerank_llm_weight,
            max_tokens=settings.rerank_max_tokens,
        )


@dataclass(frozen=True)
class RankedArticle:
    scored: ScoredArticle
    llm_score: float | None
    final_score: float


def rerank(
    conn: sqlite3.Connection,
    articles: Sequence[ScoredArticle],
    llm: LlmCaller | None,
    params: RerankParams,
) -> list[RankedArticle]:
    """上位 `top_n` 件を再ランクし、`final_score` の高い順に返す。"""
    top = list(articles[: params.top_n])
    if not top:
        return []
    normalized = _normalize(top)

    relevance = None
    if llm is None:
        logger.warning("LLM が使えないため、再ランクせずにローカルスコアの順で続けます")
    else:
        relevance = _ask(llm, top, params)

    if relevance is None:
        ranked = [RankedArticle(a, None, n) for a, n in zip(top, normalized, strict=True)]
        model = None
    else:
        weight = params.llm_weight
        ranked = [
            RankedArticle(a, relevance[i], (1 - weight) * n + weight * relevance[i])
            for i, (a, n) in enumerate(zip(top, normalized, strict=True), start=1)
        ]
        ranked.sort(key=lambda r: -r.final_score)  # 安定ソートなので、同点はローカルスコアの順
        model = llm.model if llm is not None else None

    conn.executemany(
        "UPDATE articles SET llm_score = ?, final_score = ?, score_model = ? WHERE id = ?",
        [(r.llm_score, r.final_score, model, r.scored.article.id) for r in ranked],
    )
    return ranked


def _normalize(articles: Sequence[ScoredArticle]) -> list[float]:
    """ローカルスコアを 0〜1 にそろえる（負は 0、最大値が 0 以下ならすべて 0）。"""
    top = max(a.local_score for a in articles)
    if top <= 0:
        return [0.0 for _ in articles]
    return [max(a.local_score, 0.0) / top for a in articles]


def _ask(
    llm: LlmCaller, articles: Sequence[ScoredArticle], params: RerankParams
) -> dict[int, float] | None:
    """LLM に関連度を判定させる。不正な応答なら 1 回だけ再試行し、だめなら None を返す。"""
    content = _user_message(articles, params.excerpt_chars)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = llm.create(
                "rerank",
                messages=[{"role": "user", "content": content}],
                max_tokens=params.max_tokens,
                system=_system_prompt(),
                tools=[TOOL_DEFINITION],
                tool_choice={"type": "auto"},
                output_config={"effort": "low"},
            )
        except (BudgetExceeded, LlmError) as e:
            logger.warning("再ランクを中止し、ローカルスコアの順で続けます: %s", e)
            return None
        result, problem = _parse(response, len(articles))
        if result is not None:
            return result
        logger.warning("再ランクの応答が不正です（%d 回目）: %s", attempt, problem)
    logger.warning("再ランクの応答が %d 回とも不正なため、ローカルスコアの順で続けます", ATTEMPTS)
    return None


def _parse(response: Message, count: int) -> tuple[dict[int, float] | None, str]:
    """応答を検証し、番号 → 関連度を返す。不正なら (None, 理由)。"""
    tool_input, problem = single_tool_input(response, TOOL_NAME)
    if tool_input is None:
        return None, problem
    try:
        submission = _Submission.model_validate(tool_input)
    except ValidationError as e:
        return None, f"スキーマ違反: {describe_validation_error(e)}"
    ids = [s.id for s in submission.scores]
    if sorted(ids) != list(range(1, count + 1)):
        return None, f"記事の番号が一致しない: {sorted(ids)}"
    return {s.id: s.relevance for s in submission.scores}, ""


def _user_message(articles: Sequence[ScoredArticle], excerpt_chars: int) -> str:
    entries = [
        f"[{i}] 出典: {a.article.item.source_id}\n"
        f"タイトル: {a.article.item.title}\n"
        f"抜粋: {_excerpt(a.article.item.content, excerpt_chars)}\n"
        for i, a in enumerate(articles, start=1)
    ]
    return f"次の {len(articles)} 件の記事を評価してください。\n\n" + "\n".join(entries)


def _excerpt(content: str, chars: int) -> str:
    """HTML タグを除き、空白をまとめた先頭 `chars` 文字。本文が無ければ「なし」。"""
    text = " ".join(content_excerpt(content).split())[:chars]
    return text or NO_EXCERPT


def _system_prompt() -> str:
    return (
        resources.files("aidigest.prompts").joinpath("rerank_system.md").read_text(encoding="utf-8")
    )
