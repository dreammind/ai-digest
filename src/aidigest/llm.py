"""LLM ラッパーと予算ガード（specs/007-llm-client.md）。

LLM の呼び出しはすべて `LlmClient.create` を通す。呼び出しの前に日次・月次の予算を確認し、
呼び出しの後にトークン数と概算の費用を `llm_calls` に記録する。コミットは呼び出し側が行う。
"""

import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal, TypedDict, Unpack

import anthropic
import httpx2
from anthropic.types import (
    CacheControlEphemeralParam,
    Message,
    MessageParam,
    MetadataParam,
    OutputConfigParam,
    TextBlockParam,
    ThinkingConfigParam,
    ToolChoiceParam,
    ToolUnionParam,
    Usage,
)

from aidigest.config import ConfigError, ModelPricing, Prices, PricingConfig, Settings

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9), "JST")  # 予算の「日」「月」の区切りは日本時間
TOKENS_PER_UNIT = 1_000_000  # 単価は 100 万トークンあたり

Purpose = Literal["rerank", "summarize"]


class LlmUnavailable(Exception):
    """API キーが無いなど、LLM 全体が使えない。"""


class LlmError(Exception):
    """API の呼び出しに失敗した（SDK の再試行の後も失敗、または恒久エラー）。"""


class BudgetExceeded(Exception):
    """日次または月次の予算の上限に達している。"""


class CreateOptions(TypedDict, total=False):
    """`LlmClient.create` に渡せる Messages API の引数（`model` は設定で決めるので含めない）。"""

    system: str | Iterable[TextBlockParam]
    tools: Iterable[ToolUnionParam]
    tool_choice: ToolChoiceParam
    thinking: ThinkingConfigParam
    output_config: OutputConfigParam
    cache_control: CacheControlEphemeralParam
    metadata: MetadataParam
    stop_sequences: list[str]


@dataclass(frozen=True)
class Spent:
    """当日と当月の費用の累計（USD）。"""

    today: float
    month: float


def compute_cost(usage: Usage, pricing: ModelPricing) -> float:
    """usage と単価から、1 回の呼び出しの費用（USD）を計算する。"""
    cache_write = usage.cache_creation_input_tokens or 0
    cache_read = usage.cache_read_input_tokens or 0
    if usage.cache_creation is not None:
        write_5m = usage.cache_creation.ephemeral_5m_input_tokens
        write_1h = usage.cache_creation.ephemeral_1h_input_tokens
    else:
        write_5m, write_1h = cache_write, 0

    prices: Prices = pricing
    prompt_tokens = usage.input_tokens + cache_write + cache_read
    long_context = pricing.long_context
    if long_context is not None and prompt_tokens > long_context.threshold:
        prices = long_context

    return (
        usage.input_tokens * prices.input
        + write_5m * prices.cache_write_5m
        + write_1h * prices.cache_write_1h
        + cache_read * prices.cache_read
        + usage.output_tokens * prices.output
    ) / TOKENS_PER_UNIT


def spent_usd(conn: sqlite3.Connection, now: datetime) -> Spent:
    """`llm_calls` から、日本時間の当日と当月の費用の累計を求める。"""
    local = now.astimezone(JST)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    return Spent(
        today=_sum_since(conn, day_start),
        month=_sum_since(conn, month_start),
    )


def _sum_since(conn: sqlite3.Connection, start: datetime) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE created_at >= ?",
        (_iso(start),),
    ).fetchone()
    return float(row[0])


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


class LlmClient:
    """予算ガードと記録付きの、Anthropic Messages API の入口。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        run_id: int,
        settings: Settings,
        pricing: PricingConfig,
        *,
        http_client: httpx2.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        model_pricing = pricing.models.get(settings.llm_model)
        if model_pricing is None:
            raise ConfigError(f"単価表にモデル {settings.llm_model} がありません")
        if settings.anthropic_api_key is None:
            raise LlmUnavailable("ANTHROPIC_API_KEY が設定されていません")

        self._conn = conn
        self._run_id = run_id
        self._model = settings.llm_model
        self._pricing = model_pricing
        self._daily_limit = settings.budget_daily_usd
        self._monthly_limit = settings.budget_monthly_usd
        self._clock = clock
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            http_client=http_client,
        )

    def create(
        self,
        purpose: Purpose,
        *,
        messages: Iterable[MessageParam],
        max_tokens: int,
        **options: Unpack[CreateOptions],
    ) -> Message:
        """予算を確認してから API を呼び、費用を `llm_calls` に記録して応答を返す。"""
        if "model" in options:
            raise ValueError("model は渡せません（設定の LLM_MODEL を使います）")

        spent = spent_usd(self._conn, self._clock())
        if spent.today >= self._daily_limit or spent.month >= self._monthly_limit:
            raise BudgetExceeded(
                f"予算の上限に達しています（当日 ${spent.today:.4f} / ${self._daily_limit}、"
                f"当月 ${spent.month:.4f} / ${self._monthly_limit}）"
            )

        try:
            response = self._client.messages.create(
                model=self._model, messages=messages, max_tokens=max_tokens, **options
            )
        except anthropic.APIError as e:
            raise LlmError(
                f"LLM の呼び出しに失敗しました: {type(e).__name__}: {e.message}"
            ) from None

        self._record(purpose, response)
        return response

    def _record(self, purpose: Purpose, response: Message) -> None:
        usage = response.usage
        cost = compute_cost(usage, self._pricing)
        input_tokens = (
            usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
        )
        self._conn.execute(
            "INSERT INTO llm_calls"
            " (run_id, purpose, model, input_tokens, output_tokens, cost_usd, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._run_id,
                purpose,
                self._model,
                input_tokens,
                usage.output_tokens,
                cost,
                _iso(self._clock()),
            ),
        )
        logger.info(
            "LLM を呼び出しました（purpose=%s, model=%s, input=%d, output=%d, cost=$%.6f）",
            purpose,
            self._model,
            input_tokens,
            usage.output_tokens,
            cost,
        )
