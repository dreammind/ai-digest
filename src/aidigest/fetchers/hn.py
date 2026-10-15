"""Algolia HN Search API のフェッチャー。"""

import logging
from datetime import datetime, timedelta
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, ValidationError

from aidigest.config import HnSearchSource
from aidigest.fetchers.http import FetchError, HttpClient, json_body
from aidigest.models import FetchedItem

logger = logging.getLogger(__name__)

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={}"


class _Hit(BaseModel):
    object_id: str = Field(alias="objectID")
    title: Annotated[str, StringConstraints(min_length=1)]
    url: str | None = None
    created_at: AwareDatetime
    story_text: str | None = None


class _SearchResponse(BaseModel):
    hits: list[object]


def fetch_hn(
    client: HttpClient, source: HnSearchSource, now: datetime, time_window_hours: int
) -> list[FetchedItem]:
    """タイトル検索で時間窓内の記事を取得する。OR は検索語ごとに呼んで結果を合わせる。"""
    since = int((now - timedelta(hours=time_window_hours)).timestamp())
    filters = [f"created_at_i>{since}"]
    if source.min_score is not None:
        filters.append(f"points>={source.min_score}")

    phrases = [f'"{term}"' for term in source.query]
    queries = [" ".join(phrases)] if source.query_operator == "and" else phrases

    hits: dict[str, _Hit] = {}
    for query in queries:
        for hit in _search(client, source.id, query, ",".join(filters), source.max_items):
            hits.setdefault(hit.object_id, hit)

    newest_first = sorted(hits.values(), key=lambda h: h.created_at, reverse=True)
    return [_to_item(source.id, hit) for hit in newest_first[: source.max_items]]


def _search(
    client: HttpClient, source_id: str, query: str, numeric_filters: str, hits_per_page: int
) -> list[_Hit]:
    response = client.get(
        HN_SEARCH_URL,
        params={
            "query": query,
            "tags": "story",
            "restrictSearchableAttributes": "title",
            "advancedSyntax": "true",
            "numericFilters": numeric_filters,
            "hitsPerPage": str(hits_per_page),
        },
    )
    try:
        raw_hits = _SearchResponse.model_validate(json_body(response)).hits
    except ValidationError as e:
        raise FetchError(f"HN の応答の形式が想定と違います: {e.error_count()} 件のエラー") from e

    hits: list[_Hit] = []
    for raw in raw_hits:
        try:
            hits.append(_Hit.model_validate(raw))
        except ValidationError as e:
            logger.warning("不正なヒットをスキップします（source_id=%s）: %s", source_id, e)
    return hits


def _to_item(source_id: str, hit: _Hit) -> FetchedItem:
    return FetchedItem(
        source_id=source_id,
        url=hit.url or HN_ITEM_URL.format(hit.object_id),
        title=hit.title,
        published_at=hit.created_at,
        content=hit.story_text or "",
    )
