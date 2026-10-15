"""URL の正規化と content_hash（specs/003-normalize-dedupe.md）のテスト。"""

import pytest

from aidigest.pipeline.normalize import content_hash, normalize_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://example.com/a?utm_source=x&b=2&utm_medium=y&a=1",
            "https://example.com/a?a=1&b=2",
        ),
        ("https://example.com/a?utm_campaign=c&utm_content=d&q=1", "https://example.com/a?q=1"),
    ],
)
def test_utm_params_are_removed_and_rest_sorted(url: str, expected: str) -> None:
    """AC-003-1"""
    assert normalize_url(url) == expected


def test_same_name_params_keep_relative_order() -> None:
    """AC-003-1: 同じ名前のパラメータどうしの順は保つ"""
    assert normalize_url("https://example.com/?b=2&a=1&a=0") == "https://example.com/?a=1&a=0&b=2"


def test_param_encoding_is_preserved() -> None:
    """AC-003-1 / AC-003-8: 残したパラメータの値はエンコードを変えない"""
    url = "https://example.com/s?q=a%20b&empty="
    assert normalize_url(url) == "https://example.com/s?empty=&q=a%20b"


def test_default_tracking_params_are_removed() -> None:
    """AC-003-2"""
    url = "https://example.com/a?fbclid=1&gclid=2&yclid=3&mc_cid=4&mc_eid=5&id=9"
    assert normalize_url(url) == "https://example.com/a?id=9"


def test_tracking_params_can_be_replaced() -> None:
    """AC-003-2: 一覧を差し替えると、差し替えた一覧に従う（* は前方一致）"""
    url = "https://example.com/a?utm_source=x&fbclid=1&ref=hn&ref_src=t&a=1"

    result = normalize_url(url, tracking_params=["ref", "ref_*"])

    assert result == "https://example.com/a?a=1&fbclid=1&utm_source=x"


def test_fragment_is_removed() -> None:
    """AC-003-3"""
    assert normalize_url("https://example.com/a#section-2") == "https://example.com/a"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Example.COM:443/Path/To?Q=Upper", "https://example.com/Path/To?Q=Upper"),
        ("http://EXAMPLE.com:80/x", "http://example.com/x"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("http://example.com:443/x", "http://example.com:443/x"),
    ],
)
def test_scheme_host_lowercased_and_default_port_removed(url: str, expected: str) -> None:
    """AC-003-4"""
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/a/b///", "https://example.com/a/b"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/?a=1", "https://example.com/?a=1"),
        ("https://example.com/a/?a=1", "https://example.com/a?a=1"),
    ],
)
def test_trailing_slash_is_removed(url: str, expected: str) -> None:
    """AC-003-5"""
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    ["https://example.com/a?utm_source=x", "https://example.com/a?utm_source=x#top"],
)
def test_no_question_mark_when_query_becomes_empty(url: str) -> None:
    """AC-003-6"""
    assert normalize_url(url) == "https://example.com/a"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "mailto:someone@example.com",
        "/relative/path",
        "https:///no-host",
        "https://example.com:notaport/x",
        "",
    ],
)
def test_unnormalizable_url_returns_none(url: str) -> None:
    """AC-003-7"""
    assert normalize_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://Example.COM:443/Path/?utm_source=x&b=2&a=1#frag",
        "https://example.com",
        "https://example.com/s?q=a%20b&empty=",
        "http://example.com:8080/a/b/?z=1&y=2",
        "https://ja.example.com/記事/タイトル",
    ],
)
def test_normalization_is_idempotent(url: str) -> None:
    """AC-003-8"""
    once = normalize_url(url)
    assert once is not None
    assert normalize_url(once) == once


def test_content_hash_ignores_whitespace_differences() -> None:
    """AC-003-9"""
    a = content_hash("Claude Code  v2.0", "Line one\nline two")
    b = content_hash("  Claude Code v2.0\n", "Line one   line two  ")

    assert a == b
    assert len(a) == 64
    assert a == a.lower()


@pytest.mark.parametrize(
    ("title", "content"),
    [("Claude Code v2.1", "Line one line two"), ("Claude Code v2.0", "Line one line three")],
)
def test_content_hash_changes_with_content(title: str, content: str) -> None:
    """AC-003-9"""
    assert content_hash(title, content) != content_hash("Claude Code v2.0", "Line one line two")


def test_content_hash_separates_title_and_content() -> None:
    """AC-003-9: title と content の境界がずれたら別の値になる"""
    assert content_hash("a b", "c") != content_hash("a", "b c")
