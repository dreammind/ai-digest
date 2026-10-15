"""ローカルスコアと重複話題の除去（specs/006-local-score.md）。

LLM を使わずに、テーマ別キーワード・ソースの優先度・減点キーワードで採点し、
タイトルの文字 n-gram の類似度で別ソースの同一話題を除く。コミットは呼び出し側が行う。
"""

import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from aidigest.config import Source, TopicsConfig
from aidigest.models import FetchedItem
from aidigest.pipeline.dedupe import NewArticle
from aidigest.pipeline.text import content_excerpt, normalize

logger = logging.getLogger(__name__)

_NON_WORD = re.compile(r"[\W_]+")


@dataclass(frozen=True)
class ScoredArticle:
    article: NewArticle
    local_score: float
    matched_topics: list[str]


def local_score(
    item: FetchedItem, source: Source | None, topics: TopicsConfig
) -> tuple[float, list[str]]:
    """記事のローカルスコアと、当てはまったテーマ（点の大きい順）を返す。"""
    params = topics.scoring
    title = normalize(item.title)
    content = normalize(content_excerpt(item.content))

    def hits(keywords: Iterable[str]) -> float:
        """語の倍率（タイトル 1.0、本文だけ content_factor）を大きい順に max_hits 語まで足す。"""
        factors: list[float] = []
        for keyword in dict.fromkeys(normalize(k) for k in keywords):
            if keyword in title:
                factors.append(1.0)
            elif keyword in content:
                factors.append(params.content_factor)
        return sum(sorted(factors, reverse=True)[: params.max_hits])

    total = 0.0
    matched: list[tuple[str, float]] = []
    for topic in topics.topics:
        points = topic.weight * (
            params.high_points * hits(topic.high) + params.medium_points * hits(topic.medium)
        )
        if points > 0:
            matched.append((topic.id, points))
        total += points

    if source is None:
        logger.warning(
            "設定に無いソースの記事のため、優先度と highlight_keywords を 0 として採点します"
            "（source_id=%s）",
            item.source_id,
        )
    else:
        total += params.priority_bonus[source.priority]
        total += params.highlight_points * hits(source.highlight_keywords)
    total -= params.negative_points * hits(topics.negative)

    ordered = sorted(matched, key=lambda m: m[1], reverse=True)
    return total, [topic_id for topic_id, _ in ordered]


def title_similarity(a: str, b: str, ngram: int) -> float:
    """記号と空白を除いて正規化したタイトルの、文字 n-gram の集合の Jaccard 係数。"""
    grams_a, grams_b = _ngrams(a, ngram), _ngrams(b, ngram)
    if not grams_a or not grams_b:
        return 0.0
    return len(grams_a & grams_b) / len(grams_a | grams_b)


def _ngrams(text: str, n: int) -> set[str]:
    chars = _NON_WORD.sub("", normalize(text))
    return {chars[i : i + n] for i in range(len(chars) - n + 1)}


def score_articles(
    conn: sqlite3.Connection,
    articles: Sequence[NewArticle],
    sources: Mapping[str, Source],
    topics: TopicsConfig,
) -> list[ScoredArticle]:
    """採点して足切りと重複除去を行い、残った記事をスコアの高い順に返す。"""
    params = topics.scoring
    scored: list[ScoredArticle] = []
    for article in articles:
        score, matched = local_score(article.item, sources.get(article.item.source_id), topics)
        conn.execute(
            "UPDATE articles SET local_score = ?, matched_topics = ? WHERE id = ?",
            (score, json.dumps(matched, ensure_ascii=False), article.id),
        )
        scored.append(ScoredArticle(article, score, matched))

    below_min = [s for s in scored if s.local_score < params.min_score]
    candidates = sorted(
        (s for s in scored if s.local_score >= params.min_score),
        key=_sort_key,
    )

    kept: list[ScoredArticle] = []
    duplicates: list[ScoredArticle] = []
    for candidate in candidates:
        title = candidate.article.item.title
        original = next(
            (
                k
                for k in kept
                if title_similarity(title, k.article.item.title, params.ngram)
                >= params.similarity_threshold
            ),
            None,
        )
        if original is None:
            kept.append(candidate)
            continue
        duplicates.append(candidate)
        logger.debug(
            "重複話題として外しました: %s（残した記事: %s）", title, original.article.item.title
        )

    _set_status(conn, kept, "scored")
    _set_status(conn, [*below_min, *duplicates], "skipped")
    logger.info(
        "ローカルスコア: kept=%d below_min=%d duplicates=%d",
        len(kept),
        len(below_min),
        len(duplicates),
    )
    return kept


def _sort_key(scored: ScoredArticle) -> tuple[float, float]:
    """スコアの高い順、同点は公開日時の新しい順（無ければ最後）。

    sorted は安定なので、スコアも公開日時も同じなら入力の順になる。
    """
    published = scored.article.item.published_at
    timestamp = published.timestamp() if published is not None else float("-inf")
    return (-scored.local_score, -timestamp)


def _set_status(conn: sqlite3.Connection, scored: Sequence[ScoredArticle], status: str) -> None:
    conn.executemany(
        "UPDATE articles SET status = ? WHERE id = ?",
        [(status, s.article.id) for s in scored],
    )
