"""Zenn のトピックのフェッチャー（非公式 API `/api/articles`）。

RSS は 20 件しか返さず取りこぼすため、過去 1 週間の人気順を取得し、
投稿から `lookback_days` 日以内で、いいねが `min_likes` 以上の記事だけを残す（SPEC.md §5）。
"""

import logging
from datetime import datetime, timedelta
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, StringConstraints, ValidationError

from aidigest.config import ZennTopicSource
from aidigest.fetchers.http import FetchError, HttpClient, json_body
from aidigest.models import FetchedItem

logger = logging.getLogger(__name__)

ZENN_ARTICLES_URL = "https://zenn.dev/api/articles"
ZENN_BASE_URL = "https://zenn.dev"
ZENN_PER_PAGE = 48

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class _Article(BaseModel):
    id: int
    title: NonEmptyStr
    path: NonEmptyStr
    published_at: AwareDatetime
    liked_count: int


class _Page(BaseModel):
    articles: list[object]
    next_page: int | None = None


def fetch_zenn(client: HttpClient, source: ZennTopicSource, now: datetime) -> list[FetchedItem]:
    """人気順の記事を `pages` ページまで取得し、期間といいね数で絞る。"""
    since = now - timedelta(days=source.lookback_days)
    found: dict[int, _Article] = {}

    for page in range(1, source.pages + 1):
        response = client.get(
            ZENN_ARTICLES_URL,
            params={
                "topicname": source.topic,
                "order": "weekly",
                "count": str(ZENN_PER_PAGE),
                "page": str(page),
            },
        )
        try:
            data = _Page.model_validate(json_body(response))
        except ValidationError as e:
            raise FetchError(
                f"Zenn の応答の形式が想定と違います: {e.error_count()} 件のエラー"
            ) from e

        for raw in data.articles:
            try:
                article = _Article.model_validate(raw)
            except ValidationError as e:
                logger.warning("不正な記事をスキップします（source_id=%s）: %s", source.id, e)
                continue
            if article.published_at >= since and article.liked_count >= source.min_likes:
                found.setdefault(article.id, article)

        if data.next_page is None:
            break

    newest_first = sorted(found.values(), key=lambda a: a.published_at, reverse=True)
    return [
        FetchedItem(
            source_id=source.id,
            url=ZENN_BASE_URL + article.path,
            title=article.title,
            published_at=article.published_at,
        )
        for article in newest_first[: source.max_items]
    ]
