"""事前の絞り込み（specs/005-prefilter.md）。

LLM を使わずに、時間窓・全ソース共通の除外キーワード・ソース別ルールで記事を外す。
外した記事は `articles.status` を `skipped` にする。コミットは呼び出し側が行う。
"""

import logging
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Literal

from aidigest.config import QiitaSearchSource, Source, ZennTopicSource
from aidigest.models import FetchedItem
from aidigest.pipeline.dedupe import NewArticle

logger = logging.getLogger(__name__)

Reason = Literal["time_window", "exclude_keywords", "hide_keywords", "require_keywords"]
REASONS: tuple[Reason, ...] = (
    "time_window",
    "exclude_keywords",
    "hide_keywords",
    "require_keywords",
)

CONTENT_CHARS = 1000  # 照合に使う本文の先頭の文字数
_HTML_TAG = re.compile(r"<[^>]+>")


def prefilter(
    conn: sqlite3.Connection,
    articles: Sequence[NewArticle],
    sources: Mapping[str, Source],
    exclude_keywords: Sequence[str],
    now: datetime,
    time_window_hours: int,
) -> list[NewArticle]:
    """絞り込みを通った記事を入力の順で返し、外した記事を `skipped` にする。"""
    since = now - timedelta(hours=time_window_hours)
    excluded = [normalize(k) for k in exclude_keywords]
    counts: Counter[Reason] = Counter()
    passed: list[NewArticle] = []

    for article in articles:
        source = sources.get(article.item.source_id)
        if source is None:
            logger.warning(
                "設定に無いソースの記事のため、ソース別ルールを適用しません（source_id=%s）",
                article.item.source_id,
            )
        reason = _reason(article.item, source, excluded, since)
        if reason is None:
            passed.append(article)
            continue
        counts[reason] += 1
        conn.execute("UPDATE articles SET status = 'skipped' WHERE id = ?", (article.id,))
        logger.debug("事前の絞り込みで外しました（reason=%s, url=%s）", reason, article.url)

    logger.info(
        "事前の絞り込み: passed=%d %s",
        len(passed),
        " ".join(f"{reason}={counts[reason]}" for reason in REASONS),
    )
    return passed


def normalize(text: str) -> str:
    """全角・半角と大文字・小文字の違いを無くす（NFKC 正規化 + casefold）。"""
    return unicodedata.normalize("NFKC", text).casefold()


def _reason(
    item: FetchedItem, source: Source | None, excluded: Sequence[str], since: datetime
) -> Reason | None:
    if _outside_time_window(item, source, since):
        return "time_window"
    text = _searchable_text(item)
    if _contains_any(text, excluded):
        return "exclude_keywords"
    if source is None:
        return None
    if _contains_any(text, [normalize(k) for k in source.hide_keywords]):
        return "hide_keywords"
    required = [normalize(k) for k in source.require_keywords]
    if required and not _contains_any(text, required):
        return "require_keywords"
    return None


def _outside_time_window(item: FetchedItem, source: Source | None, since: datetime) -> bool:
    # qiita_search / zenn_topic は取得時に lookback_days で絞っているため対象外（SPEC.md §5）
    if isinstance(source, QiitaSearchSource | ZennTopicSource) or item.published_at is None:
        return False
    return item.published_at < since


def _searchable_text(item: FetchedItem) -> str:
    """タイトルと、HTML タグを除いた本文の先頭 `CONTENT_CHARS` 文字。"""
    content = _HTML_TAG.sub(" ", item.content)[:CONTENT_CHARS]
    return normalize(f"{item.title}\n{content}")


def _contains_any(text: str, keywords: Sequence[str]) -> bool:
    return any(keyword in text for keyword in keywords)
