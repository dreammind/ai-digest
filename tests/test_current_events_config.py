"""current-events の設定ファイル（specs/016-current-events.md）のテスト。"""

from pathlib import Path

import pytest

from aidigest.config import ConfigError, RssSource, load_current_events_topics, load_sources

PROJECT_ROOT = Path(__file__).resolve().parent.parent

REQUIRE = [
    "AI",
    "人工知能",
    "ChatGPT",
    "Gemini",
    "Claude",
    "Copilot",
    "OpenAI",
    "Anthropic",
    "LLM",
    "チャットボット",
]
# id, url, category, priority, max_items, AI の記事だけに絞るか
SOURCES = [
    ("itmedia-aiplus", "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml", "japanese", 1, 30, False),
    ("itmedia-news", "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml", "japanese", 2, 60, True),
    ("gigazine", "https://gigazine.net/news/rss_2.0/", "japanese", 2, 40, True),
    (
        "forest-watch",
        "https://forest.watch.impress.co.jp/data/rss/1.0/wf/feed.rdf",
        "japanese",
        2,
        60,
        True,
    ),
    ("bi-japan", "https://www.businessinsider.jp/feed/index.xml", "japanese", 3, 30, True),
    (
        "pc-watch",
        "https://pc.watch.impress.co.jp/data/rss/1.0/pcw/feed.rdf",
        "japanese",
        3,
        30,
        True,
    ),
    (
        "verge-ai",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "community",
        2,
        30,
        False,
    ),
    ("cnet-ai", "https://www.cnet.com/rss/ai/", "community", 2, 30, False),
    (
        "google-blog-ai",
        "https://blog.google/innovation-and-ai/technology/ai/rss/",
        "official",
        2,
        30,
        False,
    ),
    ("wired-ai", "https://www.wired.com/feed/tag/ai/latest/rss", "community", 3, 30, False),
    (
        "guardian-ai",
        "https://www.theguardian.com/technology/artificialintelligenceai/rss",
        "community",
        3,
        30,
        False,
    ),
    ("ars-ai", "https://arstechnica.com/ai/feed/", "community", 3, 30, False),
]
EXCLUDE = ["お買い得", "セール中", "【PR】", "値引き", "% OFF", "\N{FULLWIDTH PERCENT SIGN}オフ"]
# id, weight, high, medium
TOPICS = [
    (
        "news",
        2.0,
        [
            "ChatGPT",
            "Gemini",
            "Claude",
            "Copilot",
            "Siri",
            "Alexa",
            "OpenAI",
            "Anthropic",
            "新機能",
            "発表",
            "launch",
            "announce",
        ],
        ["Google", "Apple", "Microsoft", "規制", "法律", "regulation"],
    ),
    (
        "caution",
        2.0,
        [
            "詐欺",
            "フィッシング",
            "ディープフェイク",
            "deepfake",
            "なりすまし",
            "scam",
            "著作権",
            "copyright",
        ],
        ["個人情報", "プライバシー", "privacy", "漏えい", "偽", "fake", "安全", "safety"],
    ),
    (
        "howto",
        1.5,
        ["使い方", "使ってみた", "試してみた", "how to", "I used", "I tried"],
        ["活用", "便利", "機能を追加", "tips", "guide"],
    ),
    (
        "fun",
        1.0,
        ["話題", "発見", "証明", "ロボット", "robot"],
        ["研究", "research", "画像生成", "動画生成", "会話"],
    ),
]
NEGATIVE = [
    "開発者向け",
    "developer",
    "SDK",
    "ベンチマーク",
    "benchmark",
    "論文",
    "資金調達",
    "IPO",
    "株価",
    "決算",
    "売上",
    "revenue",
    "求人",
    "hiring",
    "スポンサー",
    "sponsored",
]


def test_repository_sources_file() -> None:
    """AC-016-1"""
    loaded = load_sources(PROJECT_ROOT / "sources-current-events.yaml")

    assert loaded.errors == []
    actual = [
        (
            s.id,
            str(s.url),
            s.category,
            s.priority,
            s.max_items,
            s.require_keywords,
        )
        for s in loaded.sources
        if isinstance(s, RssSource)
    ]
    expected = [
        (i, url, category, priority, max_items, REQUIRE if require else [])
        for i, url, category, priority, max_items, require in SOURCES
    ]
    assert len(loaded.sources) == len(SOURCES)
    assert actual == expected
    assert loaded.exclude_keywords == EXCLUDE


def test_repository_topics_file() -> None:
    """AC-016-1"""
    config = load_current_events_topics(PROJECT_ROOT / "topics-current-events.yaml")

    assert [(t.id, t.weight, t.high, t.medium) for t in config.topics] == TOPICS
    assert config.negative == NEGATIVE
    assert config.digest.size == 10


def test_digest_limits_are_rejected(tmp_path: Path) -> None:
    """AC-016-2"""
    text = (PROJECT_ROOT / "topics-current-events.yaml").read_text(encoding="utf-8")
    body = text.replace("  size: 10", "  size: 10\n  limits: {other: 2}")
    assert body != text
    path = tmp_path / "topics.yaml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_current_events_topics(path)
