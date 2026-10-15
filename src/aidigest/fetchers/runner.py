"""全ソースの取得と、source_health への記録。"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from aidigest.config import HnSearchSource, QiitaSearchSource, RssSource, Source
from aidigest.fetchers.hn import fetch_hn
from aidigest.fetchers.http import HttpClient
from aidigest.fetchers.qiita import fetch_qiita
from aidigest.fetchers.rss import fetch_rss
from aidigest.models import FetchedItem

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class SourceHealth:
    source_id: str
    status: Literal["ok", "error"]
    error: str | None
    item_count: int


@dataclass(frozen=True)
class FetchResult:
    items: list[FetchedItem]
    health: list[SourceHealth]


def fetch_all(
    client: HttpClient, sources: Sequence[Source], now: datetime, time_window_hours: int
) -> FetchResult:
    """ソースを順に取得する。1 つのソースの失敗で全体を止めない。"""
    items: list[FetchedItem] = []
    health: list[SourceHealth] = []
    for source in sources:
        try:
            fetched = _fetch_one(client, source, now, time_window_hours)
        except Exception as e:  # ソース単位で例外を隔離する（CLAUDE.md）ため、すべて受け止める
            error = f"{type(e).__name__}: {e}"[:MAX_ERROR_LENGTH]
            logger.exception("ソースの取得に失敗しました（source_id=%s）", source.id)
            health.append(SourceHealth(source.id, "error", error, 0))
            continue
        if fetched is None:
            message = f"未対応の type: {source.type}"
            logger.error("%s（source_id=%s）", message, source.id)
            health.append(SourceHealth(source.id, "error", message, 0))
            continue
        items.extend(fetched)
        health.append(SourceHealth(source.id, "ok", None, len(fetched)))
    return FetchResult(items, health)


def _fetch_one(
    client: HttpClient, source: Source, now: datetime, time_window_hours: int
) -> list[FetchedItem] | None:
    """対応していない type なら None を返す（リクエストは送らない）。"""
    if isinstance(source, RssSource):
        return fetch_rss(client, source)
    if isinstance(source, HnSearchSource):
        return fetch_hn(client, source, now, time_window_hours)
    if isinstance(source, QiitaSearchSource):
        return fetch_qiita(client, source, now)
    return None


def record_health(conn: sqlite3.Connection, run_id: int, health: Sequence[SourceHealth]) -> None:
    """ソースごとの取得結果を `source_health` に登録する。コミットはしない。"""
    conn.executemany(
        "INSERT INTO source_health (run_id, source_id, status, error, item_count)"
        " VALUES (?, ?, ?, ?, ?)",
        [(run_id, h.source_id, h.status, h.error, h.item_count) for h in health],
    )
