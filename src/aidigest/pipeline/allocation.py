"""セクションへの振り分けと枠配分（specs/009-slot-allocation.md）。

`final_score` の高い順に見ていき、グループごとの上限を超えない範囲で最大 `size` 件を選ぶ。
選んだ記事の `status` を `selected` にする。コミットは呼び出し側が行う。
"""

import logging
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from aidigest.config import DIGEST_GROUPS, DigestConfig
from aidigest.pipeline.rerank import RankedArticle
from aidigest.pipeline.text import normalize

logger = logging.getLogger(__name__)

Section = Literal["claude_code", "claude_code_update", "llm", "other"]
SECTION_ORDER: tuple[Section, ...] = ("claude_code", "claude_code_update", "llm", "other")


@dataclass(frozen=True)
class SelectedArticle:
    ranked: RankedArticle
    section: Section


def group_of(matched_topics: Sequence[str], digest: DigestConfig) -> str:
    """いちばん点の高いテーマのグループ。上限の無いテーマやテーマ無しは other。"""
    top = matched_topics[0] if matched_topics else None
    return top if top is not None and top in digest.limits else "other"


def section_of(matched_topics: Sequence[str], title: str, digest: DigestConfig) -> Section:
    group = group_of(matched_topics, digest)
    if group == "claude_code":
        text = normalize(title)
        if any(normalize(k) in text for k in digest.update_keywords):
            return "claude_code_update"
        return "claude_code"
    if group == "llm":
        return "llm"
    return "other"


def allocate(
    conn: sqlite3.Connection, ranked: Sequence[RankedArticle], digest: DigestConfig
) -> list[SelectedArticle]:
    """上限を守って記事を選び、セクションの順・スコアの高い順に返す。"""
    counts: Counter[str] = Counter()
    chosen: list[SelectedArticle] = []
    for article in ranked:
        if len(chosen) >= digest.size:
            break
        scored = article.scored
        group = group_of(scored.matched_topics, digest)
        if counts[group] >= digest.limits[group]:
            continue
        counts[group] += 1
        section = section_of(scored.matched_topics, scored.article.item.title, digest)
        chosen.append(SelectedArticle(article, section))

    conn.executemany(
        "UPDATE articles SET status = 'selected' WHERE id = ?",
        [(c.ranked.scored.article.id,) for c in chosen],
    )
    logger.info(
        "枠配分: selected=%d %s",
        len(chosen),
        " ".join(
            f"{g}={counts[g]}/{digest.limits[g]}" for g in DIGEST_GROUPS if g in digest.limits
        ),
    )
    # 入力は final_score の高い順なので、安定ソートでセクションの中もスコアの順になる
    return sorted(chosen, key=lambda c: SECTION_ORDER.index(c.section))
