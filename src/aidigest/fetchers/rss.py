"""RSS 2.0 / Atom のフェッチャー。"""

import logging
import time
from datetime import UTC, datetime

import feedparser
from pydantic import TypeAdapter, ValidationError

from aidigest.config import RssSource
from aidigest.fetchers.http import FetchError, HttpClient
from aidigest.models import FetchedItem

logger = logging.getLogger(__name__)

_ENTRIES = TypeAdapter(list[dict[str, object]])


def fetch_rss(client: HttpClient, source: RssSource) -> list[FetchedItem]:
    """フィードの先頭から `max_items` 件のエントリを `FetchedItem` にする。"""
    response = client.get(str(source.url))
    parsed = feedparser.parse(response.content)
    if not parsed.get("version"):
        raise FetchError(f"フィードとして解析できません: {source.url}")
    try:
        entries = _ENTRIES.validate_python(parsed.get("entries", []))
    except ValidationError as e:
        raise FetchError(f"フィードのエントリを読めません: {source.url}") from e

    items: list[FetchedItem] = []
    for entry in entries[: source.max_items]:
        item = _to_item(source.id, entry)
        if item is not None:
            items.append(item)
    return items


def _to_item(source_id: str, entry: dict[str, object]) -> FetchedItem | None:
    link = entry.get("link")
    title = entry.get("title")
    if not isinstance(link, str) or not link or not isinstance(title, str) or not title:
        logger.warning(
            "リンクかタイトルが無いエントリをスキップします（source_id=%s, link=%s, title=%s）",
            source_id,
            link,
            title,
        )
        return None
    return FetchedItem(
        source_id=source_id,
        url=link,
        title=title,
        published_at=_to_datetime(entry.get("published_parsed"))
        or _to_datetime(entry.get("updated_parsed")),
        content=_content(entry),
    )


def _to_datetime(value: object) -> datetime | None:
    """feedparser が UTC に変換した `struct_time` を、タイムゾーン付きの日時にする。"""
    if not isinstance(value, time.struct_time):
        return None
    return datetime(
        value.tm_year,
        value.tm_mon,
        value.tm_mday,
        value.tm_hour,
        value.tm_min,
        value.tm_sec,
        tzinfo=UTC,
    )


def _content(entry: dict[str, object]) -> str:
    try:
        contents = _ENTRIES.validate_python(entry.get("content", []))
    except ValidationError:
        contents = []
    if contents and isinstance(value := contents[0].get("value"), str):
        return value
    summary = entry.get("summary")
    return summary if isinstance(summary, str) else ""
