"""既読判定と記録（specs/003-normalize-dedupe.md）。

取得した全記事を、配信するかどうかに関係なく、この時点で `articles` に記録する。
コミットは呼び出し側が行う。
"""

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from aidigest.config import Source
from aidigest.models import FetchedItem
from aidigest.pipeline.normalize import content_hash, normalize_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewArticle:
    """今回の処理を続ける記事（新規、または再収集）。"""

    id: int
    url: str
    item: FetchedItem


def register_items(
    conn: sqlite3.Connection,
    items: Sequence[FetchedItem],
    sources: Mapping[str, Source],
    now: datetime,
) -> list[NewArticle]:
    """未読の記事を `articles` に記録し、処理を続ける記事を入力の順に返す。"""
    fetched_at = _iso(now)
    seen_urls: set[str] = set()
    result: list[NewArticle] = []

    for item in items:
        url = normalize_url(item.url)
        if url is None:
            logger.warning(
                "URL を正規化できないためスキップします（source_id=%s, url=%s）",
                item.source_id,
                item.url,
            )
            continue
        source = sources.get(item.source_id)
        if source is None:
            logger.error(
                "設定に無いソースの記事のためスキップします（source_id=%s, url=%s）",
                item.source_id,
                item.url,
            )
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        new_hash = content_hash(item.title, item.content)
        row = conn.execute("SELECT id, content_hash FROM articles WHERE url = ?", (url,)).fetchone()
        if row is None:
            article_id = _insert(conn, url, source, item, new_hash, fetched_at)
        elif source.recheck_changed and row[1] != new_hash:
            article_id = int(row[0])
            _recollect(conn, article_id, item, new_hash, fetched_at)
        else:
            continue
        result.append(NewArticle(article_id, url, item))

    return result


def _insert(
    conn: sqlite3.Connection,
    url: str,
    source: Source,
    item: FetchedItem,
    hash_: str,
    fetched_at: str,
) -> int:
    row = conn.execute(
        "INSERT INTO articles"
        " (url, source_id, category, title, published_at, fetched_at, content_hash, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'new') RETURNING id",
        (
            url,
            source.id,
            source.category,
            item.title,
            _iso_or_none(item.published_at),
            fetched_at,
            hash_,
        ),
    ).fetchone()
    return int(row[0])


def _recollect(
    conn: sqlite3.Connection, article_id: int, item: FetchedItem, hash_: str, fetched_at: str
) -> None:
    """内容が変わった記事を新規に戻し、前回の採点・要約の結果を消す。"""
    conn.execute(
        "UPDATE articles SET title = ?, published_at = ?, content_hash = ?, fetched_at = ?,"
        " status = 'new', local_score = NULL, llm_score = NULL, final_score = NULL,"
        " matched_topics = NULL, summary_json = NULL, score_model = NULL, summary_model = NULL"
        " WHERE id = ?",
        (item.title, _iso_or_none(item.published_at), hash_, fetched_at, article_id),
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)
