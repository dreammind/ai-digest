"""共通の HTTP アクセス（ユーザーエージェント、タイムアウト、再試行、同じホストへの間隔）。"""

import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import Self

import httpx

from aidigest.config import Settings

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """取得に失敗した（再試行しても成功しない、応答が期待した形式でない）。"""


def _is_transient(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


class HttpClient:
    """再試行と、同じホストへのアクセス間隔を守る HTTP クライアント。"""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float,
        retries: int,
        backoff_seconds: float,
        host_interval_seconds: float,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )
        self._retries = retries
        self._backoff_seconds = backoff_seconds
        self._host_interval_seconds = host_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: dict[str, float] = {}

    @classmethod
    def from_settings(
        cls, settings: Settings, transport: httpx.BaseTransport | None = None
    ) -> Self:
        return cls(
            user_agent=settings.user_agent,
            timeout_seconds=settings.http_timeout_seconds,
            retries=settings.http_retries,
            backoff_seconds=settings.http_backoff_seconds,
            host_interval_seconds=settings.host_interval_seconds,
            transport=transport,
        )

    def get(self, url: str, params: Mapping[str, str] | None = None) -> httpx.Response:
        """GET する。一時的な失敗は再試行し、それでも失敗すれば `FetchError` を送出する。"""
        host = httpx.URL(url).host
        last_error = ""
        for attempt in range(self._retries + 1):
            if attempt > 0:
                self._sleep(self._backoff_seconds * 2 ** (attempt - 1))
            self._wait_for_host(host)
            try:
                response = self._client.get(url, params=params)
            except httpx.TransportError as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning(
                    "取得に失敗しました（%d 回目）: %s: %s", attempt + 1, url, last_error
                )
                continue
            if _is_transient(response.status_code):
                last_error = f"HTTP {response.status_code}"
                logger.warning(
                    "取得に失敗しました（%d 回目）: %s: %s", attempt + 1, url, last_error
                )
                continue
            if response.is_error:
                raise FetchError(f"HTTP {response.status_code}: {url}")
            return response
        raise FetchError(f"{self._retries + 1} 回試みて失敗しました: {url}: {last_error}")

    def close(self) -> None:
        self._client.close()

    def _wait_for_host(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is not None:
            wait = last + self._host_interval_seconds - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request_at[host] = self._clock()


def json_body(response: httpx.Response) -> object:
    """応答を JSON として読む。読めなければ `FetchError` を送出する。"""
    try:
        body: object = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise FetchError(f"JSON として読めません: {response.url}: {e}") from e
    return body
