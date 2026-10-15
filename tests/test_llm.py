"""LLM ラッパーと予算ガード（specs/007-llm-client.md）のテスト。

SDK は本物を使い、その下の HTTP 通信だけを httpx2.MockTransport で差し替える。
実際の API には出ない。
"""

import json
import sqlite3
import textwrap
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import anthropic
import httpx2
import pytest
from anthropic.types import MessageParam, Usage

from aidigest.config import ConfigError, PricingConfig, Settings, load_pricing, load_settings
from aidigest.db import open_db
from aidigest.llm import (
    BudgetExceeded,
    CreateOptions,
    LlmClient,
    LlmError,
    LlmUnavailable,
    compute_cost,
    spent_usd,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 16, 21, 30, tzinfo=UTC)  # 日本時間 10/17 06:30
SECRET = "sk-ant-test-very-secret"

PRICING_YAML = """\
models:
  claude-haiku-5-5:
    input: 0.10
    output: 0.50
    cache_write_5m: 0.125
    cache_write_1h: 0.20
    cache_read: 0.01
    long_context:
      threshold: 100000
      input: 0.50
      output: 2.50
      cache_write_5m: 0.625
      cache_write_1h: 1.00
      cache_read: 0.05
"""

Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ["ANTHROPIC_API_KEY", "LLM_MODEL", "LLM_TIMEOUT_SECONDS", "LLM_MAX_RETRIES"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def make_settings(tmp_path: Path, **values: object) -> Settings:
    lines = {"ANTHROPIC_API_KEY": SECRET, "LLM_MAX_RETRIES": 0, **values}
    env = tmp_path / "test.env"
    env.write_text("".join(f"{k}={v}\n" for k, v in lines.items() if v is not None))
    return load_settings(env)


def pricing(tmp_path: Path, body: str = PRICING_YAML) -> PricingConfig:
    path = tmp_path / "pricing.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return load_pricing(path)


def usage(**fields: object) -> Usage:
    return Usage.model_validate({"input_tokens": 0, "output_tokens": 0, **fields})


def message_json(**usage_fields: object) -> dict[str, object]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 500, **usage_fields},
    }


class FakeApi:
    """Messages API の偽物。受けたリクエストを記録し、決まった応答を返す。"""

    def __init__(self, responses: list[httpx2.Response] | None = None) -> None:
        self.responses = responses
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return httpx2.Response(200, json=message_json())

    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "INSERT INTO runs (started_at) VALUES ('2026-10-16T21:30:00+00:00') RETURNING id"
    ).fetchone()
    conn.commit()
    return int(row[0])


def make_client(
    conn: sqlite3.Connection,
    run_id: int,
    tmp_path: Path,
    handler: Handler,
    **settings: object,
) -> LlmClient:
    return LlmClient(
        conn,
        run_id,
        make_settings(tmp_path, **settings),
        pricing(tmp_path),
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        clock=lambda: NOW,
    )


def add_call(conn: sqlite3.Connection, run_id: int, created_at: str, cost: float) -> None:
    conn.execute(
        "INSERT INTO llm_calls (run_id, purpose, model, input_tokens, output_tokens, cost_usd,"
        " created_at) VALUES (?, 'summarize', 'claude-haiku-5-5', 0, 0, ?, ?)",
        (run_id, cost, created_at),
    )


def llm_rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT purpose, model, input_tokens, output_tokens, cost_usd, created_at"
            " FROM llm_calls ORDER BY id"
        )
    ]


# ------------------------------------------------------------ 単価表


def test_pricing_is_loaded(tmp_path: Path) -> None:
    """AC-007-2"""
    loaded = pricing(tmp_path)

    haiku = loaded.models["claude-haiku-5-5"]
    assert (haiku.input, haiku.output, haiku.cache_read) == (0.10, 0.50, 0.01)
    assert (haiku.cache_write_5m, haiku.cache_write_1h) == (0.125, 0.20)
    assert haiku.long_context is not None
    assert haiku.long_context.threshold == 100000
    assert haiku.long_context.input == 0.50


def test_long_context_is_optional(tmp_path: Path) -> None:
    """AC-007-2"""
    body = PRICING_YAML.split("    long_context:")[0]

    assert pricing(tmp_path, body).models["claude-haiku-5-5"].long_context is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    input: 0.10", "    input: -0.10"),
        ("threshold: 100000", "threshold: 0"),
        ("    cache_read: 0.01\n", ""),
    ],
    ids=["negative-price", "zero-threshold", "missing-price"],
)
def test_invalid_pricing_raises_config_error(tmp_path: Path, old: str, new: str) -> None:
    """AC-007-3"""
    with pytest.raises(ConfigError):
        pricing(tmp_path, PRICING_YAML.replace(old, new, 1))


def test_missing_pricing_file_raises_config_error(tmp_path: Path) -> None:
    """AC-007-3"""
    with pytest.raises(ConfigError):
        load_pricing(tmp_path / "no-such.yaml")


def test_unknown_model_raises_config_error(
    conn: sqlite3.Connection, run_id: int, tmp_path: Path
) -> None:
    """AC-007-4"""
    with pytest.raises(ConfigError):
        make_client(conn, run_id, tmp_path, FakeApi(), LLM_MODEL="claude-unknown-9")


def test_repository_pricing_yaml() -> None:
    """AC-007-5"""
    haiku = load_pricing(PROJECT_ROOT / "pricing.yaml").models["claude-haiku-5-5"]

    assert (haiku.input, haiku.output) == (0.10, 0.50)
    assert (haiku.cache_write_5m, haiku.cache_write_1h, haiku.cache_read) == (0.125, 0.20, 0.01)
    assert haiku.long_context is not None
    assert (haiku.long_context.threshold, haiku.long_context.input) == (100000, 0.50)
    assert haiku.long_context.output == 2.50


# ------------------------------------------------------------ 費用の計算


def test_cost_without_cache(tmp_path: Path) -> None:
    """AC-007-6"""
    haiku = pricing(tmp_path).models["claude-haiku-5-5"]

    assert compute_cost(usage(input_tokens=1000, output_tokens=500), haiku) == pytest.approx(
        0.00035
    )


def test_cost_with_cache(tmp_path: Path) -> None:
    """AC-007-7"""
    haiku = pricing(tmp_path).models["claude-haiku-5-5"]
    with_breakdown = usage(
        input_tokens=100,
        output_tokens=10,
        cache_creation_input_tokens=3000,
        cache_creation={"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 1000},
        cache_read_input_tokens=3000,
    )
    without_breakdown = usage(input_tokens=0, cache_creation_input_tokens=2000)

    expected = (100 * 0.10 + 2000 * 0.125 + 1000 * 0.20 + 3000 * 0.01 + 10 * 0.50) / 1e6
    assert compute_cost(with_breakdown, haiku) == pytest.approx(expected)
    assert compute_cost(without_breakdown, haiku) == pytest.approx(2000 * 0.125 / 1e6)


def test_long_context_pricing(tmp_path: Path) -> None:
    """AC-007-8: プロンプトの長さ = 入力 + キャッシュ書き込み + キャッシュ読み取り"""
    haiku = pricing(tmp_path).models["claude-haiku-5-5"]
    exactly = usage(input_tokens=60000, cache_read_input_tokens=40000, output_tokens=100)
    over = usage(input_tokens=60001, cache_read_input_tokens=40000, output_tokens=100)

    assert compute_cost(exactly, haiku) == pytest.approx(
        (60000 * 0.10 + 40000 * 0.01 + 100 * 0.50) / 1e6
    )
    assert compute_cost(over, haiku) == pytest.approx(
        (60001 * 0.50 + 40000 * 0.05 + 100 * 2.50) / 1e6
    )


# ------------------------------------------------------------ 呼び出しと記録


def test_call_is_made_and_recorded_without_commit(
    conn: sqlite3.Connection, run_id: int, tmp_path: Path
) -> None:
    """AC-007-9"""
    api = FakeApi([httpx2.Response(200, json=message_json(cache_read_input_tokens=200))])
    client = make_client(conn, run_id, tmp_path, api)
    messages: list[MessageParam] = [{"role": "user", "content": "要約してください"}]

    response = client.create("summarize", messages=messages, max_tokens=256)

    assert response.content[0].type == "text"
    (body,) = api.bodies()
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 256
    assert body["messages"] == messages
    expected_cost = (1000 * 0.10 + 200 * 0.01 + 500 * 0.50) / 1e6
    (row,) = llm_rows(conn)
    assert row[:4] == ("summarize", "claude-haiku-5-5", 1200, 500)
    assert row[4] == pytest.approx(expected_cost)
    assert row[5] == "2026-10-16T21:30:00+00:00"
    conn.rollback()
    assert llm_rows(conn) == []


@pytest.mark.parametrize(("spent", "allowed"), [(0.15, False), (0.1499, True)])
def test_daily_budget(
    conn: sqlite3.Connection, run_id: int, tmp_path: Path, spent: float, allowed: bool
) -> None:
    """AC-007-10"""
    add_call(conn, run_id, "2026-10-16T20:00:00+00:00", spent)
    api = FakeApi()
    client = make_client(conn, run_id, tmp_path, api)

    if allowed:
        client.create("rerank", messages=[{"role": "user", "content": "x"}], max_tokens=10)
        assert len(api.requests) == 1
    else:
        with pytest.raises(BudgetExceeded):
            client.create("rerank", messages=[{"role": "user", "content": "x"}], max_tokens=10)
        assert api.requests == []


def test_monthly_budget(conn: sqlite3.Connection, run_id: int, tmp_path: Path) -> None:
    """AC-007-11: 当日はゼロだが、当月の別の日で上限に達している"""
    add_call(conn, run_id, "2026-10-05T00:00:00+00:00", 1.5)
    add_call(conn, run_id, "2026-10-10T00:00:00+00:00", 0.5)
    api = FakeApi()
    client = make_client(conn, run_id, tmp_path, api)

    with pytest.raises(BudgetExceeded):
        client.create("rerank", messages=[{"role": "user", "content": "x"}], max_tokens=10)
    assert api.requests == []


def test_spent_uses_japan_time_boundaries(conn: sqlite3.Connection, run_id: int) -> None:
    """AC-007-12: NOW は日本時間 10/17 06:30"""
    add_call(conn, run_id, "2026-10-16T15:00:00+00:00", 0.01)  # 日本時間 10/17 00:00 → 当日
    add_call(conn, run_id, "2026-10-16T14:59:59+00:00", 0.02)  # 日本時間 10/16 → 当月
    add_call(conn, run_id, "2026-09-30T15:00:00+00:00", 0.04)  # 日本時間 10/01 00:00 → 当月
    add_call(conn, run_id, "2026-09-30T14:59:59+00:00", 0.08)  # 日本時間 9/30 → 対象外

    spent = spent_usd(conn, NOW)

    assert spent.today == pytest.approx(0.01)
    assert spent.month == pytest.approx(0.07)


def test_missing_api_key_raises_unavailable(
    conn: sqlite3.Connection, run_id: int, tmp_path: Path
) -> None:
    """AC-007-13"""
    with pytest.raises(LlmUnavailable):
        LlmClient(conn, run_id, make_settings(tmp_path, ANTHROPIC_API_KEY=None), pricing(tmp_path))


def _raise_connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("refused", request=request)


@pytest.mark.parametrize(
    "handler",
    [
        FakeApi(
            [
                httpx2.Response(
                    429,
                    json={
                        "type": "error",
                        "error": {"type": "rate_limit_error", "message": "slow"},
                    },
                )
            ]
        ),
        FakeApi(
            [
                httpx2.Response(
                    500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}
                )
            ]
        ),
        FakeApi(
            [
                httpx2.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": "bad"},
                    },
                )
            ]
        ),
        _raise_connect_error,
    ],
    ids=["429", "500", "400", "connect"],
)
def test_api_errors_become_llm_error(
    conn: sqlite3.Connection, run_id: int, tmp_path: Path, handler: Handler
) -> None:
    """AC-007-14"""
    client = make_client(conn, run_id, tmp_path, handler)

    with pytest.raises(LlmError) as exc_info:
        client.create("summarize", messages=[{"role": "user", "content": "x"}], max_tokens=10)

    assert SECRET not in str(exc_info.value)
    assert llm_rows(conn) == []


def test_timeout_and_retries_are_passed_to_sdk(
    conn: sqlite3.Connection, run_id: int, tmp_path: Path
) -> None:
    """AC-007-15: 1 回目 500 → SDK が 1 回再試行して成功。タイムアウトはリクエストに付く"""
    error = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
    api = FakeApi([httpx2.Response(500, json=error), httpx2.Response(200, json=message_json())])
    client = make_client(conn, run_id, tmp_path, api, LLM_MAX_RETRIES=1, LLM_TIMEOUT_SECONDS=12)

    client.create("summarize", messages=[{"role": "user", "content": "x"}], max_tokens=10)

    assert len(api.requests) == 2
    assert api.requests[0].extensions["timeout"]["read"] == 12


def test_model_cannot_be_passed(conn: sqlite3.Connection, run_id: int, tmp_path: Path) -> None:
    """AC-007-16"""
    api = FakeApi()
    client = make_client(conn, run_id, tmp_path, api)

    with pytest.raises(ValueError, match="model"):
        client.create(
            "summarize",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=10,
            **cast(CreateOptions, {"model": "claude-opus-5-5"}),
        )
    assert api.requests == []


def test_invalid_yaml_pricing_raises_config_error(tmp_path: Path) -> None:
    """AC-007-3: YAML として不正"""
    with pytest.raises(ConfigError):
        pricing(tmp_path, "models: [unclosed\n")
