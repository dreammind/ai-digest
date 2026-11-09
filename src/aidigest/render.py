"""ダイジェストの HTML の生成（specs/011-render.md、specs/016-current-events.md）。

要約の結果から、メールの件名・HTML の本文・テキストの本文を作り、静的な HTML としても書き出す。
current-events は、LINE に貼る部分を先頭に置いたテキストの本文だけを作る。
外部から来た文字列（タイトル・要約・エラー）は、HTML では jinja2 の自動エスケープでエスケープする。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from aidigest.fetchers.runner import SourceHealth
from aidigest.llm import Spent
from aidigest.pipeline.allocation import SECTION_ORDER, Section
from aidigest.pipeline.summarize import CurrentEventsSummary, SummarizedArticle, Summary

HEADINGS: dict[Section, str] = {
    "claude_code": "今日の最重要（Claude Code 手法）",
    "claude_code_update": "Claude Code 更新情報",
    "llm": "LLM 動向",
    "other": "その他",
}
ERROR_CHARS = 200  # フッターに出すエラーの内容の最大文字数

# 1 文：文末の記号（。！？!?）まで。直後の閉じかっこも同じ文に含める
_SENTENCE = re.compile(r"[^。！？!?]+(?:[。！？!?]+[」』）)】]*)?|[。！？!?]+[」』）)】]*")


@dataclass(frozen=True)
class RenderedDigest:
    subject: str
    html: str
    text: str


@dataclass(frozen=True)
class TextDigest:
    """テキストの本文だけのダイジェスト（current-events。HTML は作らない）。"""

    subject: str
    text: str


@dataclass(frozen=True)
class _ArticleView:
    title: str
    url: str
    source: str
    summary: list[list[str]] | None  # 段落 → 文。None なら「要約なし」
    apply: list[list[str]]  # 空なら AI活用 は出さない


@dataclass(frozen=True)
class _SectionView:
    heading: str
    articles: list[_ArticleView]


@dataclass(frozen=True)
class _FailedSource:
    source_id: str
    error: str


def render_digest(
    articles: Sequence[SummarizedArticle[Summary]],
    day: date,
    health: Sequence[SourceHealth],
    spent: Spent,
    usd_jpy_rate: float,
    notices: Sequence[str],
    templates_dir: Path,
) -> RenderedDigest:
    subject = f"[AI Digest] {day.isoformat()} 注目 {len(articles)} 件"
    sections = [
        _SectionView(HEADINGS[s], [_view(a) for a in articles if a.selected.section == s])
        for s in SECTION_ORDER
    ]
    context: dict[str, object] = {
        "subject": subject,
        "day": day.isoformat(),
        "notices": list(notices),
        "sections": [s for s in sections if s.articles],
        "failed": _failed(health),
        "cost": _cost(spent, usd_jpy_rate),
    }
    html = _environment(templates_dir, autoescape=True).get_template("digest.html.j2")
    text = _environment(templates_dir, autoescape=False).get_template("digest.txt.j2")
    return RenderedDigest(subject, html.render(context), text.render(context))


@dataclass(frozen=True)
class _LineArticleView:
    title: str
    url: str
    source: str
    summary: str | None  # None なら要約なし（タイトルと URL の 2 行）
    why: str


def render_current_events(
    articles: Sequence[SummarizedArticle[CurrentEventsSummary]],
    day: date,
    health: Sequence[SourceHealth],
    spent: Spent,
    usd_jpy_rate: float,
    notices: Sequence[str],
    templates_dir: Path,
) -> TextDigest:
    """current-events のテキストのメールを作る（specs/016）。HTML は作らない。"""
    subject = f"[AI 時事ネタ] {day.isoformat()} {len(articles)} 件"
    context: dict[str, object] = {
        "day_md": f"{day.month}/{day.day}",
        "articles": [_line_view(a) for a in articles],
        "notices": list(notices),
        "failed": _failed(health),
        "cost": _cost(spent, usd_jpy_rate),
    }
    text = _environment(templates_dir, autoescape=False).get_template("current_events.txt.j2")
    return TextDigest(subject, text.render(context))


def _line_view(article: SummarizedArticle[CurrentEventsSummary]) -> _LineArticleView:
    new = article.selected.ranked.scored.article
    summary = article.summary
    return _LineArticleView(
        title=one_line(summary.title_ja if summary is not None else new.item.title),
        url=new.url,
        source=new.item.source_id,
        summary=one_line(summary.summary) if summary is not None else None,
        why=one_line(summary.why_it_matters) if summary is not None else "",
    )


def one_line(text: str) -> str:
    """改行（前後の空白を含む）を空白 1 つにする。LINE で 1 記事が 4 行にそろうように。"""
    return re.sub(r"\s*\n\s*", " ", text.strip())


def _failed(health: Sequence[SourceHealth]) -> list[_FailedSource]:
    return [
        _FailedSource(h.source_id, (h.error or "")[:ERROR_CHARS])
        for h in health
        if h.status == "error"
    ]


def _cost(spent: Spent, usd_jpy_rate: float) -> str:
    return (
        f"本日 {spent.today * usd_jpy_rate:.2f} 円（${spent.today:.4f}）／ "
        f"今月 {spent.month * usd_jpy_rate:.2f} 円（${spent.month:.4f}）"
    )


def write_static(html: str, day: date, output_dir: Path) -> Path:
    """`output_dir/YYYY-MM-DD.html` に書き出す（同じ日のファイルは上書き）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{day.isoformat()}.html"
    path.write_text(html, encoding="utf-8")
    return path


def _view(article: SummarizedArticle[Summary]) -> _ArticleView:
    new = article.selected.ranked.scored.article
    summary = article.summary
    return _ArticleView(
        title=summary.title_ja if summary is not None else new.item.title,
        url=new.url,
        source=new.item.source_id,
        summary=split_paragraphs(summary.summary) if summary is not None else None,
        apply=split_paragraphs(summary.apply_to_claude_code) if summary is not None else [],
    )


def split_paragraphs(text: str) -> list[list[str]]:
    """改行で段落に分け、各段落を文に分ける（空の段落・空の文は除く）。"""
    paragraphs: list[list[str]] = []
    for line in text.splitlines():
        sentences = [m.group().strip() for m in _SENTENCE.finditer(line)]
        sentences = [s for s in sentences if s]
        if sentences:
            paragraphs.append(sentences)
    return paragraphs


def _environment(templates_dir: Path, *, autoescape: bool) -> Environment:
    return Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=autoescape,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
