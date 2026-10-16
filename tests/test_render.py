"""ダイジェストの HTML の生成（specs/011-render.md）のテスト。リポジトリのテンプレートを使う。"""

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from aidigest.fetchers.runner import SourceHealth
from aidigest.llm import Spent
from aidigest.models import FetchedItem
from aidigest.pipeline.allocation import Section, SelectedArticle
from aidigest.pipeline.dedupe import NewArticle
from aidigest.pipeline.rerank import RankedArticle
from aidigest.pipeline.scoring import ScoredArticle
from aidigest.pipeline.summarize import SummarizedArticle, Summary
from aidigest.render import RenderedDigest, render_digest, write_static

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
DAY = date(2026, 10, 17)
NO_COST = Spent(today=0.0, month=0.0)
ALL_OK = [SourceHealth("zenn-claudecode", "ok", None, 10)]


def summary(**overrides: object) -> Summary:
    data: dict[str, object] = {
        "title_ja": "日本語のタイトル",
        "tldr": "一文の要約。",
        "key_takeaways": ["要点"],
        "summary": "二つか三つの文の要約です。",
        "topics": ["hooks"],
        "novelty": 3,
        "apply_to_claude_code": "hooks で ruff を走らせる。",
        **overrides,
    }
    return Summary.model_validate(data)


_ids = iter(range(1, 1_000_000))


def article(
    section: Section,
    title: str = "Original title",
    summary_: Summary | None = None,
    *,
    source_id: str = "zenn-claudecode",
) -> SummarizedArticle:
    n = next(_ids)
    url = f"https://example.com/{n}"
    item = FetchedItem(
        source_id=source_id, url=url, title=title, published_at=datetime(2026, 10, 16, tzinfo=UTC)
    )
    ranked = RankedArticle(ScoredArticle(NewArticle(n, url, item), 1.0, []), 0.5, 0.5)
    return SummarizedArticle(SelectedArticle(ranked, section), summary_)


def render(
    articles: Sequence[SummarizedArticle],
    *,
    health: Sequence[SourceHealth] = ALL_OK,
    spent: Spent = NO_COST,
    notices: Sequence[str] = (),
) -> RenderedDigest:
    return render_digest(articles, DAY, health, spent, 160, notices, TEMPLATES)


def visible(html: str) -> str:
    """HTML からタグを除いた、見える文字だけ（順序の確認用）。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_subject() -> None:
    """AC-011-2"""
    digest = render([article("llm", summary_=summary()) for _ in range(7)])

    assert digest.subject == "[AI Digest] 2026-10-17 注目 7 件"


def test_sections_in_order_and_empty_ones_hidden() -> None:
    """AC-011-3"""
    articles = [
        article("claude_code", summary_=summary(title_ja="手法1")),
        article("claude_code", summary_=summary(title_ja="手法2")),
        article("llm", summary_=summary(title_ja="動向1")),
        article("other", summary_=summary(title_ja="その他1")),
    ]

    digest = render(articles)

    for body in (visible(digest.html), digest.text):
        order = [
            body.index(word)
            for word in [
                "今日の最重要（Claude Code 手法）",
                "手法1",
                "手法2",
                "LLM 動向",
                "動向1",
                "その他",
                "その他1",
            ]
        ]
        assert order == sorted(order)
        assert "Claude Code 更新情報" not in body


def test_summarized_article() -> None:
    """AC-011-4"""
    with_apply = article(
        "claude_code", summary_=summary(title_ja="活用あり"), source_id="qiita-llm"
    )
    without_apply = article("llm", summary_=summary(title_ja="活用なし", apply_to_claude_code=""))

    digest = render([with_apply, without_apply])

    html = digest.html
    url = with_apply.selected.ranked.scored.article.url
    assert f'href="{url}"' in html
    assert "活用あり" in html
    assert "二つか三つの文の要約です。" in html
    assert "hooks で ruff を走らせる。" in html
    assert "qiita-llm" in html
    assert visible(html).count("AI活用") == 1, (
        "apply_to_claude_code が空の記事には AI活用 を出さない"
    )
    assert "AI活用" in digest.text


def test_unsummarized_article() -> None:
    """AC-011-5"""
    item = article("other", title="Raw English Title", source_id="hn-llm")

    digest = render([item])

    url = item.selected.ranked.scored.article.url
    assert f'href="{url}"' in digest.html
    assert "Raw English Title" in digest.html
    assert "hn-llm" in digest.html
    assert "要約なし" in digest.html
    assert "要約なし" in digest.text


def test_external_strings_are_escaped() -> None:
    """AC-011-6"""
    evil = "<script>alert(1)</script>"
    digest = render(
        [
            article("llm", title=evil),
            article(
                "llm", summary_=summary(title_ja=evil, summary=evil, apply_to_claude_code=evil)
            ),
        ],
        health=[SourceHealth("hn-llm", "error", evil, 0)],
    )

    assert "<script>" not in digest.html
    assert "&lt;script&gt;" in digest.html


def test_footer_failed_sources() -> None:
    """AC-011-7"""
    health = [
        SourceHealth("zenn-claudecode", "ok", None, 10),
        SourceHealth("hn-llm", "error", "FetchError: " + "x" * 300, 0),
        SourceHealth("simonw", "ok", None, 30),
    ]

    digest = render([], health=health)

    for body in (visible(digest.html), digest.text):
        assert "hn-llm" in body
        assert "FetchError: " + "x" * 188 in body
        assert "x" * 189 not in body
        assert "zenn-claudecode" not in body
        assert "simonw" not in body
        assert "すべてのソースから取得できました" not in body
    assert "すべてのソースから取得できました" in render([]).text


def test_footer_cost() -> None:
    """AC-011-8"""
    digest = render([], spent=Spent(today=0.0025, month=0.0771))

    expected = "本日 0.40 円（$0.0025）／ 今月 12.34 円（$0.0771）"
    assert expected in visible(digest.html)
    assert expected in digest.text


def test_notices() -> None:
    """AC-011-9"""
    digest = render([article("llm")], notices=["LLM を使わずに作成しました"])

    body = visible(digest.html)
    assert body.index("LLM を使わずに作成しました") < body.index("LLM 動向")
    assert "LLM を使わずに作成しました" in digest.text
    assert "お知らせ" not in visible(render([article("llm")]).html)


def test_empty_digest() -> None:
    """AC-011-10"""
    digest = render([])

    assert digest.subject == "[AI Digest] 2026-10-17 注目 0 件"
    assert "本日の注目記事はありません" in digest.html
    assert "本日の注目記事はありません" in digest.text
    assert "LLM の費用" in digest.text


def test_text_body_has_no_tags() -> None:
    """AC-011-11"""
    item = article("llm", summary_=summary(title_ja="テキスト版"))

    text = render([item]).text

    assert re.search(r"<[a-zA-Z/][^>]*>", text) is None
    assert "テキスト版" in text
    assert item.selected.ranked.scored.article.url in text
    assert "LLM の費用" in text


def test_write_static(tmp_path: Path) -> None:
    """AC-011-12"""
    output = tmp_path / "out" / "nested"

    path = write_static("<html>first</html>", DAY, output)
    path = write_static("<html>second</html>", DAY, output)

    assert path == output / "2026-10-17.html"
    assert path.read_text(encoding="utf-8") == "<html>second</html>"


@pytest.mark.parametrize("section", ["claude_code", "claude_code_update", "llm", "other"])
def test_each_section_heading(section: Section) -> None:
    """AC-011-3: 見出しの文言"""
    headings = {
        "claude_code": "今日の最重要（Claude Code 手法）",
        "claude_code_update": "Claude Code 更新情報",
        "llm": "LLM 動向",
        "other": "その他",
    }

    assert headings[section] in render([article(section)]).text
