"""Qiita API v2 の記事検索のフェッチャー。"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, StringConstraints, TypeAdapter, ValidationError

from aidigest.config import QiitaSearchSource
from aidigest.fetchers.http import FetchError, HttpClient, json_body
from aidigest.models import FetchedItem

logger = logging.getLogger(__name__)

QIITA_ITEMS_URL = "https://qiita.com/api/v2/items"
QIITA_MAX_PER_PAGE = 100
JST = timezone(timedelta(hours=9), "JST")  # Qiita の created: は日本時間の日付

_ARTICLES = TypeAdapter(list[object])


class _Article(BaseModel):
    url: str
    title: Annotated[str, StringConstraints(min_length=1)]
    created_at: AwareDatetime
    body: str = ""


def fetch_qiita(client: HttpClient, source: QiitaSearchSource, now: datetime) -> list[FetchedItem]:
    """投稿から `lookback_days` 日以内で、ストックが `min_stocks` 以上の記事を取得する。"""
    since = (now - timedelta(days=source.lookback_days)).astimezone(JST).date()
    response = client.get(
        QIITA_ITEMS_URL,
        params={
            "query": f"{source.query} created:>={since.isoformat()} stocks:>={source.min_stocks}",
            "per_page": str(min(source.max_items, QIITA_MAX_PER_PAGE)),
            "page": "1",
        },
    )
    try:
        raw_articles = _ARTICLES.validate_python(json_body(response))
    except ValidationError as e:
        raise FetchError(f"Qiita の応答の形式が想定と違います: {e.error_count()} 件のエラー") from e

    items: list[FetchedItem] = []
    for raw in raw_articles:
        try:
            article = _Article.model_validate(raw)
        except ValidationError as e:
            logger.warning("不正な記事をスキップします（source_id=%s）: %s", source.id, e)
            continue
        items.append(
            FetchedItem(
                source_id=source.id,
                url=article.url,
                title=article.title,
                published_at=article.created_at,
                content=article.body,
            )
        )
    return items
