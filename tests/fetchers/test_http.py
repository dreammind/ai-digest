"""共通の HTTP アクセス（specs/004-fetchers.md）のテスト。"""

from pathlib import Path

import httpx
import pytest

from aidigest.config import load_settings
from aidigest.fetchers.http import FetchError, HttpClient
from tests.fetchers.helpers import FakeClock, make_client


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="ok")


def test_user_agent_is_sent() -> None:
    """AC-004-1"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return ok(request)

    make_client(handler, user_agent="aidigest-test/9.9").get("https://example.com/feed")

    assert seen == ["aidigest-test/9.9"]


def test_from_settings_uses_configured_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-004-1: 設定の USER_AGENT が使われる"""
    monkeypatch.setenv("USER_AGENT", "custom-agent/2.0")
    settings = load_settings(tmp_path / "no-such.env")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return ok(request)

    client = HttpClient.from_settings(settings, transport=httpx.MockTransport(handler))
    client.get("https://example.com/feed")
    client.close()

    assert seen == ["custom-agent/2.0"]


def _fail_once(kind: str) -> httpx.Response:
    if kind == "timeout":
        raise httpx.ReadTimeout("timed out")
    if kind == "connect":
        raise httpx.ConnectError("refused")
    return httpx.Response(int(kind))


@pytest.mark.parametrize("kind", ["503", "500", "429", "timeout", "connect"])
def test_transient_failure_is_retried(kind: str) -> None:
    """AC-004-2"""
    calls: list[int] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _fail_once(kind) if len(calls) == 1 else ok(request)

    response = make_client(handler, clock=clock, backoff_seconds=1.0).get("https://example.com/")

    assert response.status_code == 200
    assert len(calls) == 2
    assert clock.sleeps == [1.0]


def test_gives_up_after_retries_with_exponential_backoff() -> None:
    """AC-004-3"""
    calls: list[int] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    client = make_client(handler, clock=clock, retries=2, backoff_seconds=1.0)

    with pytest.raises(FetchError):
        client.get("https://example.com/")

    assert len(calls) == 3
    assert clock.sleeps == [1.0, 2.0]


@pytest.mark.parametrize("status", [400, 403, 404])
def test_client_error_is_not_retried(status: int) -> None:
    """AC-004-4"""
    calls: list[int] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status)

    with pytest.raises(FetchError):
        make_client(handler, clock=clock).get("https://example.com/")

    assert len(calls) == 1
    assert clock.sleeps == []


def test_same_host_requests_are_spaced() -> None:
    """AC-004-5"""
    clock = FakeClock()
    client = make_client(ok, clock=clock, host_interval_seconds=1.0)

    client.get("https://a.example.com/1")
    client.get("https://a.example.com/2")
    assert clock.sleeps == [1.0]

    client.get("https://b.example.com/1")
    assert clock.sleeps == [1.0], "違うホストへは待たない"

    clock.now += 5.0
    client.get("https://a.example.com/3")
    assert clock.sleeps == [1.0], "間隔が十分に空いていれば待たない"
