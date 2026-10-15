"""設定の読み込み（specs/001-config.md）のテスト。"""

import logging
import re
import textwrap
from pathlib import Path

import pytest

from aidigest.config import (
    ArxivSource,
    ConfigError,
    GithubReleaseSource,
    HnSearchSource,
    QiitaSearchSource,
    RssSource,
    Settings,
    load_settings,
    load_sources,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENV_NAMES = [
    "ANTHROPIC_API_KEY",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "MAIL_FROM",
    "MAIL_TO",
    "SOURCES_PATH",
    "DB_PATH",
    "USER_AGENT",
    "HTTP_TIMEOUT_SECONDS",
    "HTTP_RETRIES",
    "HTTP_BACKOFF_SECONDS",
    "HOST_INTERVAL_SECONDS",
    "TIME_WINDOW_HOURS",
]

BASE = "category: official\n    priority: 1\n    max_items: 10"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """実環境の環境変数やカレントディレクトリの .env を読まないようにする。"""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def write_sources(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------- sources.yaml


def test_loads_all_source_types_in_order_with_defaults(tmp_path: Path) -> None:
    """AC-001-1"""
    path = write_sources(
        tmp_path,
        """\
        sources:
          - id: simonw
            type: rss
            url: https://simonwillison.net/atom/everything/
            category: official
            priority: 1
            max_items: 10
          - id: cc-releases
            type: github_release
            repo: anthropics/claude-code
            category: official
            priority: 1
            max_items: 10
          - id: hn
            type: hn_search
            query: Claude Code
            category: official
            priority: 1
            max_items: 10
          - id: arxiv
            type: arxiv
            query: LLM agents
            category: official
            priority: 1
            max_items: 10
        """,
    )

    loaded = load_sources(path)

    assert loaded.errors == []
    assert [s.id for s in loaded.sources] == ["simonw", "cc-releases", "hn", "arxiv"]
    rss, gh, hn, arxiv = loaded.sources
    assert isinstance(rss, RssSource)
    assert str(rss.url) == "https://simonwillison.net/atom/everything/"
    assert isinstance(gh, GithubReleaseSource)
    assert gh.repo == "anthropics/claude-code"
    assert isinstance(hn, HnSearchSource)
    assert hn.min_score is None
    assert isinstance(arxiv, ArxivSource)
    for source in loaded.sources:
        assert source.category == "official"
        assert source.priority == 1
        assert source.max_items == 10
        assert source.require_keywords == []
        assert source.hide_keywords == []
        assert source.highlight_keywords == []
        assert source.recheck_changed is False


def test_loads_explicit_optional_fields(tmp_path: Path) -> None:
    """AC-001-1: 省略しなかった任意項目はその値で読み込まれる"""
    path = write_sources(
        tmp_path,
        """\
        sources:
          - id: hn
            type: hn_search
            query: Claude Code
            category: community
            priority: 3
            max_items: 5
            min_score: 50
            require_keywords: [claude]
            hide_keywords: [hiring]
            highlight_keywords: [hooks, MCP]
            recheck_changed: true
        """,
    )

    (hn,) = load_sources(path).sources

    assert isinstance(hn, HnSearchSource)
    assert hn.category == "community"
    assert hn.priority == 3
    assert hn.max_items == 5
    assert hn.min_score == 50
    assert hn.require_keywords == ["claude"]
    assert hn.hide_keywords == ["hiring"]
    assert hn.highlight_keywords == ["hooks", "MCP"]
    assert hn.recheck_changed is True


@pytest.mark.parametrize(
    ("source_type", "missing"),
    [("rss", "url"), ("github_release", "repo"), ("hn_search", "query"), ("arxiv", "query")],
)
def test_entry_without_required_target_is_excluded(
    tmp_path: Path, source_type: str, missing: str
) -> None:
    """AC-001-2"""
    path = write_sources(
        tmp_path,
        f"""\
        sources:
          - id: broken
            type: {source_type}
            category: official
            priority: 1
            max_items: 10
          - id: ok
            type: rss
            url: https://example.com/feed.xml
            category: official
            priority: 1
            max_items: 10
        """,
    )

    loaded = load_sources(path)

    assert [s.id for s in loaded.sources] == ["ok"]
    assert len(loaded.errors) == 1
    error = loaded.errors[0]
    assert error.index == 0
    assert error.source_id == "broken"
    assert missing in error.reason


@pytest.mark.parametrize(
    "entry",
    [
        "type: rss\n    url: https://example.com/f\n    category: official\n"
        "    priority: 0\n    max_items: 10",
        "type: rss\n    url: https://example.com/f\n    category: official\n"
        "    priority: 4\n    max_items: 10",
        "type: rss\n    url: https://example.com/f\n    category: official\n"
        "    priority: 1\n    max_items: 0",
        f"type: rss\n    url: ftp://example.com/f\n    {BASE}",
        f"type: rss\n    url: not-a-url\n    {BASE}",
        f"type: github_release\n    repo: anthropics\n    {BASE}",
        f"type: github_release\n    repo: a/b/c\n    {BASE}",
        "type: rss\n    url: https://example.com/f\n    category: blog\n"
        "    priority: 1\n    max_items: 10",
        f"type: podcast\n    url: https://example.com/f\n    {BASE}",
        "type: rss\n    url: https://example.com/f",
    ],
    ids=[
        "priority-0",
        "priority-4",
        "max_items-0",
        "url-ftp",
        "url-invalid",
        "repo-no-slash",
        "repo-too-many-slashes",
        "category-unknown",
        "type-unknown",
        "missing-common-fields",
    ],
)
def test_entry_violating_constraints_is_excluded(tmp_path: Path, entry: str) -> None:
    """AC-001-3"""
    path = write_sources(tmp_path, f"sources:\n  - id: bad\n    {entry}\n")

    loaded = load_sources(path)

    assert loaded.sources == []
    assert [(e.index, e.source_id) for e in loaded.errors] == [(0, "bad")]


@pytest.mark.parametrize(
    "extra",
    ["titel: typo", "repo: anthropics/claude-code", "query: Claude"],
    ids=["unknown-key", "repo-on-rss", "query-on-rss"],
)
def test_entry_with_unknown_or_unused_key_is_excluded(tmp_path: Path, extra: str) -> None:
    """AC-001-4"""
    path = write_sources(
        tmp_path,
        f"sources:\n  - id: bad\n    type: rss\n    url: https://example.com/f\n"
        f"    {BASE}\n    {extra}\n",
    )

    loaded = load_sources(path)

    assert loaded.sources == []
    assert [e.source_id for e in loaded.errors] == ["bad"]


@pytest.mark.parametrize(
    "target",
    ["type: rss\n    url: https://example.com/f", "type: arxiv\n    query: LLM"],
    ids=["rss", "arxiv"],
)
def test_min_score_outside_hn_search_is_excluded(tmp_path: Path, target: str) -> None:
    """AC-001-5"""
    path = write_sources(
        tmp_path, f"sources:\n  - id: bad\n    {target}\n    {BASE}\n    min_score: 10\n"
    )

    loaded = load_sources(path)

    assert loaded.sources == []
    assert [e.source_id for e in loaded.errors] == ["bad"]


def test_duplicate_id_keeps_first_entry(tmp_path: Path) -> None:
    """AC-001-6"""
    path = write_sources(
        tmp_path,
        """\
        sources:
          - id: dup
            type: rss
            url: https://example.com/first
            category: official
            priority: 1
            max_items: 10
          - id: dup
            type: rss
            url: https://example.com/second
            category: official
            priority: 1
            max_items: 10
        """,
    )

    loaded = load_sources(path)

    assert len(loaded.sources) == 1
    first = loaded.sources[0]
    assert isinstance(first, RssSource)
    assert str(first.url) == "https://example.com/first"
    assert [(e.index, e.source_id) for e in loaded.errors] == [(1, "dup")]


def test_non_mapping_entry_is_excluded_without_id(tmp_path: Path) -> None:
    """AC-001-2 / AC-001-3: マッピングでないエントリも、そのエントリだけ除外される"""
    path = write_sources(
        tmp_path,
        """\
        sources:
          - just-a-string
          - id: ok
            type: rss
            url: https://example.com/f
            category: official
            priority: 1
            max_items: 10
        """,
    )

    loaded = load_sources(path)

    assert [s.id for s in loaded.sources] == ["ok"]
    assert [(e.index, e.source_id) for e in loaded.errors] == [(0, None)]


def test_excluded_entry_is_logged_as_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-001-7"""
    path = write_sources(
        tmp_path,
        """\
        sources:
          - id: ok
            type: rss
            url: https://example.com/f
            category: official
            priority: 1
            max_items: 10
          - id: broken-feed
            type: rss
            category: official
            priority: 1
            max_items: 10
        """,
    )

    with caplog.at_level(logging.ERROR, logger="aidigest.config"):
        load_sources(path)

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(messages) == 1
    assert "broken-feed" in messages[0]
    assert "1" in messages[0]


@pytest.mark.parametrize(
    "content",
    [
        None,
        "sources: [unclosed\n",
        "- id: a\n",
        "other: []\n",
        "sources: not-a-list\n",
        "",
    ],
    ids=["missing-file", "invalid-yaml", "top-level-list", "no-sources-key", "not-list", "empty"],
)
def test_unreadable_sources_file_raises_config_error(tmp_path: Path, content: str | None) -> None:
    """AC-001-8"""
    path = tmp_path / "sources.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_sources(path)


def test_empty_sources_list_is_valid(tmp_path: Path) -> None:
    """AC-001-8 の境界: sources が空のリストなら ConfigError にはならない"""
    loaded = load_sources(write_sources(tmp_path, "sources: []\n"))

    assert loaded.sources == []
    assert loaded.errors == []


@pytest.mark.parametrize("source_type", ["hn_search", "arxiv"])
def test_query_is_normalized_to_list_and_operator_defaults_to_and(
    tmp_path: Path, source_type: str
) -> None:
    """AC-001-14"""
    path = write_sources(
        tmp_path,
        f"""\
        sources:
          - id: single
            type: {source_type}
            query: Claude Code
            category: official
            priority: 1
            max_items: 10
          - id: multiple
            type: {source_type}
            query: ["Claude Code", "Claude Agent SDK"]
            query_operator: or
            category: official
            priority: 1
            max_items: 10
        """,
    )

    loaded = load_sources(path)

    assert loaded.errors == []
    single, multiple = loaded.sources
    assert isinstance(single, HnSearchSource | ArxivSource)
    assert isinstance(multiple, HnSearchSource | ArxivSource)
    assert single.query == ["Claude Code"]
    assert single.query_operator == "and"
    assert multiple.query == ["Claude Code", "Claude Agent SDK"]
    assert multiple.query_operator == "or"


@pytest.mark.parametrize(
    "query_lines",
    [
        "query: Claude\n    query_operator: xor",
        "query: []",
        'query: ["Claude", ""]',
        'query: ""',
    ],
    ids=["operator-invalid", "empty-list", "list-with-empty", "empty-string"],
)
def test_invalid_query_is_excluded(tmp_path: Path, query_lines: str) -> None:
    """AC-001-15"""
    path = write_sources(
        tmp_path, f"sources:\n  - id: bad\n    type: hn_search\n    {query_lines}\n    {BASE}\n"
    )

    loaded = load_sources(path)

    assert loaded.sources == []
    assert [e.source_id for e in loaded.errors] == ["bad"]


@pytest.mark.parametrize(
    "target",
    [
        "type: rss\n    url: https://example.com/f",
        "type: github_release\n    repo: anthropics/claude-code",
    ],
    ids=["rss", "github_release"],
)
def test_query_operator_outside_query_sources_is_excluded(tmp_path: Path, target: str) -> None:
    """AC-001-16"""
    path = write_sources(
        tmp_path, f"sources:\n  - id: bad\n    {target}\n    {BASE}\n    query_operator: or\n"
    )

    loaded = load_sources(path)

    assert loaded.sources == []
    assert [e.source_id for e in loaded.errors] == ["bad"]


QIITA_BASE = "category: japanese\n    priority: 1\n    max_items: 20"


def test_qiita_search_is_loaded_with_defaults(tmp_path: Path) -> None:
    """AC-001-18"""
    path = write_sources(
        tmp_path,
        """\
        sources:
          - id: qiita-claudecode
            type: qiita_search
            query: "tag:ClaudeCode"
            category: japanese
            priority: 1
            max_items: 20
          - id: qiita-llm
            type: qiita_search
            query: "tag:LLM"
            category: japanese
            priority: 2
            max_items: 20
            min_stocks: 10
            lookback_days: 3
        """,
    )

    loaded = load_sources(path)

    assert loaded.errors == []
    default, custom = loaded.sources
    assert isinstance(default, QiitaSearchSource)
    assert isinstance(custom, QiitaSearchSource)
    assert default.query == "tag:ClaudeCode"
    assert default.min_stocks == 3
    assert default.lookback_days == 7
    assert custom.min_stocks == 10
    assert custom.lookback_days == 3


@pytest.mark.parametrize("query_line", ["", 'query: ""', 'query: ["tag:LLM"]'])
def test_qiita_search_without_valid_query_is_excluded(tmp_path: Path, query_line: str) -> None:
    """AC-001-19: query が無い、空、または文字列でない"""
    path = write_sources(
        tmp_path,
        f"sources:\n  - id: bad\n    type: qiita_search\n    {query_line}\n    {QIITA_BASE}\n",
    )

    loaded = load_sources(path)

    assert loaded.sources == []
    assert [e.source_id for e in loaded.errors] == ["bad"]


@pytest.mark.parametrize(
    "extra", ["min_stocks: -1", "lookback_days: 0"], ids=["negative-stocks", "zero-days"]
)
def test_qiita_search_with_invalid_limits_is_excluded(tmp_path: Path, extra: str) -> None:
    """AC-001-20"""
    path = write_sources(
        tmp_path,
        f"sources:\n  - id: bad\n    type: qiita_search\n    query: tag:LLM\n"
        f"    {QIITA_BASE}\n    {extra}\n",
    )

    loaded = load_sources(path)

    assert loaded.sources == []
    assert [e.source_id for e in loaded.errors] == ["bad"]


@pytest.mark.parametrize(
    "entry",
    [
        f"type: rss\n    url: https://example.com/f\n    {BASE}\n    min_stocks: 3",
        f"type: hn_search\n    query: LLM\n    {BASE}\n    lookback_days: 7",
        f"type: qiita_search\n    query: tag:LLM\n    {QIITA_BASE}\n    query_operator: or",
        f"type: qiita_search\n    query: tag:LLM\n    {QIITA_BASE}\n    min_score: 10",
    ],
    ids=[
        "min_stocks-on-rss",
        "lookback_days-on-hn",
        "query_operator-on-qiita",
        "min_score-on-qiita",
    ],
)
def test_qiita_only_fields_are_rejected_elsewhere(tmp_path: Path, entry: str) -> None:
    """AC-001-21"""
    path = write_sources(tmp_path, f"sources:\n  - id: bad\n    {entry}\n")

    loaded = load_sources(path)

    assert loaded.sources == []
    assert [e.source_id for e in loaded.errors] == ["bad"]


# ------------------------------------------------------------------------ .env


def test_settings_reads_env_file(tmp_path: Path) -> None:
    """AC-001-9"""
    env_file = write_env(
        tmp_path,
        """\
        ANTHROPIC_API_KEY=sk-test-key
        SMTP_HOST=smtp.example.com
        SMTP_PORT=587
        SMTP_USER=user
        SMTP_PASSWORD=smtp-pass
        MAIL_FROM=from@example.com
        MAIL_TO=to@example.com
        SOURCES_PATH=config/sources.yaml
        """,
    )

    settings = load_settings(env_file)

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-test-key"
    assert settings.smtp_host == "smtp.example.com"
    assert settings.smtp_port == 587
    assert settings.smtp_user == "user"
    assert settings.smtp_password is not None
    assert settings.smtp_password.get_secret_value() == "smtp-pass"
    assert settings.mail_from == "from@example.com"
    assert settings.mail_to == "to@example.com"
    assert settings.sources_path == Path("config/sources.yaml")


def test_environment_variable_overrides_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-001-9: 環境変数が .env より優先される"""
    env_file = write_env(tmp_path, "SMTP_HOST=from-env-file\n")
    monkeypatch.setenv("SMTP_HOST", "from-environment")

    assert load_settings(env_file).smtp_host == "from-environment"


def test_settings_without_secrets_loads_with_none(tmp_path: Path) -> None:
    """AC-001-10"""
    settings = load_settings(tmp_path / "no-such.env")

    assert settings.anthropic_api_key is None
    assert settings.smtp_host is None
    assert settings.smtp_port is None
    assert settings.smtp_user is None
    assert settings.smtp_password is None
    assert settings.mail_from is None
    assert settings.mail_to is None
    assert settings.sources_path == Path("sources.yaml")


def test_empty_env_values_are_treated_as_unset(tmp_path: Path) -> None:
    """AC-001-17"""
    env_file = write_env(tmp_path, "".join(f"{name}=\n" for name in ENV_NAMES))

    settings = load_settings(env_file)

    assert settings.anthropic_api_key is None
    assert settings.smtp_host is None
    assert settings.smtp_port is None
    assert settings.smtp_password is None
    assert settings.sources_path == Path("sources.yaml")


@pytest.mark.parametrize("port", ["abc", "0", "65536"])
def test_invalid_smtp_port_raises_config_error_without_secrets(tmp_path: Path, port: str) -> None:
    """AC-001-11"""
    env_file = write_env(
        tmp_path,
        f"ANTHROPIC_API_KEY=sk-very-secret\nSMTP_PASSWORD=pw-very-secret\nSMTP_PORT={port}\n",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env_file)

    message = str(exc_info.value)
    assert "SMTP_PORT" in message.upper()
    assert "sk-very-secret" not in message
    assert "pw-very-secret" not in message


def test_secrets_are_not_exposed_in_str_repr_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-001-12"""
    env_file = write_env(
        tmp_path, "ANTHROPIC_API_KEY=sk-very-secret\nSMTP_PASSWORD=pw-very-secret\n"
    )
    settings = load_settings(env_file)

    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("settings: %s", settings)

    for text in (str(settings), repr(settings), caplog.text):
        assert "sk-very-secret" not in text
        assert "pw-very-secret" not in text


def test_fetcher_settings_have_defaults(tmp_path: Path) -> None:
    """AC-004-21"""
    settings = load_settings(tmp_path / "no-such.env")

    assert settings.user_agent == "aidigest/0.1 (+https://github.com/dreammind/ai-digest)"
    assert settings.http_timeout_seconds == 20
    assert settings.http_retries == 2
    assert settings.http_backoff_seconds == 1.0
    assert settings.host_interval_seconds == 1.0
    assert settings.time_window_hours == 36


@pytest.mark.parametrize(
    "line",
    [
        "HTTP_TIMEOUT_SECONDS=0",
        "HTTP_RETRIES=-1",
        "HTTP_BACKOFF_SECONDS=-1",
        "HOST_INTERVAL_SECONDS=-0.5",
        "TIME_WINDOW_HOURS=0",
    ],
)
def test_invalid_fetcher_settings_raise_config_error(tmp_path: Path, line: str) -> None:
    """AC-004-21: 表の制約（正の数、0 以上）を満たさなければ ConfigError"""
    with pytest.raises(ConfigError):
        load_settings(write_env(tmp_path, line + "\n"))


def test_env_example_lists_all_settings_variables() -> None:
    """AC-001-13"""
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))

    expected = {name.upper() for name in Settings.model_fields}
    assert expected == set(ENV_NAMES)
    assert expected <= listed
