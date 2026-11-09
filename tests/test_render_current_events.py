"""current-events のメールの生成（specs/016-current-events.md）のテスト。"""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from aidigest.fetchers.runner import SourceHealth
from aidigest.llm import Spent
from aidigest.models import FetchedItem
from aidigest.pipeline.allocation import SelectedArticle
from aidigest.pipeline.dedupe import NewArticle
from aidigest.pipeline.rerank import RankedArticle
from aidigest.pipeline.scoring import ScoredArticle
from aidigest.pipeline.summarize import CurrentEventsSummary, SummarizedArticle
from aidigest.render import TextDigest, render_current_events

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
DAY = date(2026, 11, 9)
SPENT = Spent(today=0.0025, month=0.0771)
ALL_OK = [SourceHealth("forest-watch", "ok", None, 60)]
EQ = "\N{FULLWIDTH EQUALS SIGN}"  # 管理用の情報で、タイトルとソースの間に置く記号


def article(
    n: int,
    summary: CurrentEventsSummary | None,
    *,
    title: str = "Original title",
    source_id: str = "forest-watch",
) -> SummarizedArticle[CurrentEventsSummary]:
    url = f"https://example.com/{n}"
    item = FetchedItem(
        source_id=source_id, url=url, title=title, published_at=datetime(2026, 11, 9, tzinfo=UTC)
    )
    ranked = RankedArticle(ScoredArticle(NewArticle(n, url, item), 1.0, []), 0.5, 0.5)
    return SummarizedArticle(SelectedArticle(ranked, "other"), summary)


def ce_summary(title: str, summary: str, why: str) -> CurrentEventsSummary:
    return CurrentEventsSummary(title_ja=title, summary=summary, why_it_matters=why)


def render(
    articles: Sequence[SummarizedArticle[CurrentEventsSummary]],
    *,
    health: Sequence[SourceHealth] = ALL_OK,
    notices: Sequence[str] = (),
) -> TextDigest:
    return render_current_events(articles, DAY, health, SPENT, 160, notices, TEMPLATES)


def paste_part(text: str) -> str:
    start = text.index("----- ここから LINE に貼る -----")
    end = text.index("----- ここまで -----")
    return text[start:end]


def test_text_digest() -> None:
    """AC-016-7"""
    digest = render(
        [
            article(1, ce_summary("タイトル1", "要約1です。", "関係1です。")),
            article(
                2,
                ce_summary("タイトル2", "要約2です。", "関係2です。"),
                source_id="itmedia-aiplus",
            ),
        ]
    )

    assert digest.subject == "[AI 時事ネタ] 2026-11-09 2 件"
    assert isinstance(digest, TextDigest)  # HTML の本文は持たない
    assert digest.text == (
        "----- ここから LINE に貼る -----\n"
        "🤖 今日の AI ニュース（11/9）\n"
        "\n"
        "🔹 タイトル1\n"
        "要約1です。\n"
        "👉 関係1です。\n"
        "https://example.com/1\n"
        "\n"
        "🔹 タイトル2\n"
        "要約2です。\n"
        "👉 関係2です。\n"
        "https://example.com/2\n"
        "----- ここまで -----\n"
        "\n"
        "■ 管理用の情報\n"
        "ソース：\n"
        f"- タイトル1{EQ}forest-watch\n"
        f"- タイトル2{EQ}itmedia-aiplus\n"
        "取得に失敗したソース：なし\n"
        "LLM の費用：本日 0.40 円（$0.0025）／ 今月 12.34 円（$0.0771）\n"
    )


def test_unsummarized_article_has_two_lines() -> None:
    """AC-016-8"""
    digest = render([article(1, None, title="Original title")])

    assert "🔹 Original title\nhttps://example.com/1\n" in paste_part(digest.text)
    assert "👉" not in digest.text
    assert f"- Original title{EQ}forest-watch" in digest.text


def test_newlines_in_summary_become_spaces() -> None:
    """AC-016-9"""
    digest = render([article(1, ce_summary("タイトル", "一文目。\n二文目。", "関係\nです。"))])

    part = paste_part(digest.text)
    assert "🔹 タイトル\n一文目。 二文目。\n👉 関係 です。\nhttps://example.com/1\n" in part


def test_failures_and_notices_only_in_admin_part() -> None:
    """AC-016-10"""
    digest = render(
        [article(1, None)],
        health=[SourceHealth("gigazine", "error", "HTTPStatusError: 503", 0)],
        notices=["LLM を使わずに作成しました（--no-llm）。要約はありません。"],
    )

    part = paste_part(digest.text)
    admin = digest.text[digest.text.index("----- ここまで -----") :]
    for text in ["gigazine", "LLM を使わずに作成しました"]:
        assert text not in part
        assert text in admin
    assert "取得に失敗したソース：\n- gigazine：HTTPStatusError: 503\n" in admin
    assert "お知らせ：\n- LLM を使わずに作成しました（--no-llm）。要約はありません。\n" in admin
