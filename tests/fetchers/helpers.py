"""フェッチャーのテストで共通に使う偽の時計・クライアント・ソース。"""

from collections.abc import Callable

import httpx

from aidigest.config import HnSearchSource, QiitaSearchSource, RssSource
from aidigest.fetchers.http import HttpClient

Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    """sleep すると時刻だけを進める時計（実際には待たない）。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(
    handler: Handler,
    *,
    clock: FakeClock | None = None,
    retries: int = 2,
    backoff_seconds: float = 1.0,
    host_interval_seconds: float = 0.0,
    user_agent: str = "test-agent/1.0",
) -> HttpClient:
    fake = clock or FakeClock()
    return HttpClient(
        user_agent=user_agent,
        timeout_seconds=5.0,
        retries=retries,
        backoff_seconds=backoff_seconds,
        host_interval_seconds=host_interval_seconds,
        transport=httpx.MockTransport(handler),
        clock=fake.time,
        sleep=fake.sleep,
    )


BASE: dict[str, object] = {"category": "community", "priority": 2, "max_items": 10}


def rss_source(**overrides: object) -> RssSource:
    data = {"id": "feed", "type": "rss", "url": "https://example.com/feed.xml", **BASE}
    return RssSource.model_validate({**data, **overrides})


def hn_source(**overrides: object) -> HnSearchSource:
    data = {"id": "hn", "type": "hn_search", "query": "Claude Code", **BASE}
    return HnSearchSource.model_validate({**data, **overrides})


def qiita_source(**overrides: object) -> QiitaSearchSource:
    data = {"id": "qiita", "type": "qiita_search", "query": "tag:LLM", **BASE}
    return QiitaSearchSource.model_validate({**data, **overrides})
