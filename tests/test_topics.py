"""topics.yaml の読み込み（specs/006-local-score.md）のテスト。"""

import textwrap
from pathlib import Path

import pytest

from aidigest.config import ConfigError, load_topics

PROJECT_ROOT = Path(__file__).resolve().parent.parent

VALID = """\
scoring:
  priority_bonus: {1: 2.0, 2: 1.0, 3: 0.0}
  high_points: 2.0
  medium_points: 1.0
  negative_points: 3.0
  highlight_points: 1.0
  content_factor: 0.25
  max_hits: 3
  min_score: 1.0
  similarity_threshold: 0.45
  ngram: 2
topics:
  - id: claude_code
    weight: 3.0
    high: [Claude Code, hooks]
    medium: [MCP]
  - id: llm
    weight: 2.0
    high: [LLM]
negative: [hiring]
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "topics.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_valid_topics_are_loaded(tmp_path: Path) -> None:
    """AC-006-1"""
    config = load_topics(write(tmp_path, VALID))

    assert config.scoring.priority_bonus == {1: 2.0, 2: 1.0, 3: 0.0}
    assert config.scoring.content_factor == 0.25
    assert config.scoring.max_hits == 3
    assert config.scoring.min_score == 1.0
    assert config.scoring.similarity_threshold == 0.45
    assert config.scoring.ngram == 2
    claude_code, llm = config.topics
    assert (claude_code.id, claude_code.weight) == ("claude_code", 3.0)
    assert claude_code.high == ["Claude Code", "hooks"]
    assert claude_code.medium == ["MCP"]
    assert llm.medium == [], "medium は省略できる"
    assert config.negative == ["hiring"]


def test_negative_can_be_omitted(tmp_path: Path) -> None:
    """AC-006-1"""
    body = VALID.replace("negative: [hiring]\n", "")

    assert load_topics(write(tmp_path, body)).negative == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("  - id: llm\n", "  - id: claude_code\n"),
        ("    weight: 2.0\n", "    weight: 0\n"),
        ("high: [LLM]", 'high: [""]'),
        ("{1: 2.0, 2: 1.0, 3: 0.0}", "{1: 2.0, 2: 1.0}"),
        ("similarity_threshold: 0.45", "similarity_threshold: 0"),
        ("similarity_threshold: 0.45", "similarity_threshold: 1.5"),
        ("ngram: 2", "ngram: 0"),
        ("content_factor: 0.25", "content_factor: 1.5"),
        ("  max_hits: 3\n", ""),
        ("high_points: 2.0", "high_points: -1"),
        ("    high: [LLM]\n", ""),
    ],
    ids=[
        "duplicate-id",
        "zero-weight",
        "empty-keyword",
        "missing-priority-3",
        "zero-threshold",
        "threshold-over-1",
        "zero-ngram",
        "content-factor-over-1",
        "missing-scoring-item",
        "negative-points",
        "missing-high",
    ],
)
def test_invalid_topics_raise_config_error(tmp_path: Path, old: str, new: str) -> None:
    """AC-006-2"""
    assert old in VALID
    with pytest.raises(ConfigError):
        load_topics(write(tmp_path, VALID.replace(old, new, 1)))


@pytest.mark.parametrize("body", [None, "scoring: [unclosed\n", "- a\n", ""])
def test_unreadable_topics_raise_config_error(tmp_path: Path, body: str | None) -> None:
    """AC-006-2: ファイルが無い、YAML として不正、形式が違う"""
    path = tmp_path / "topics.yaml"
    if body is not None:
        path.write_text(body, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_topics(path)


def test_repository_topics_yaml_is_valid() -> None:
    """AC-006-4"""
    config = load_topics(PROJECT_ROOT / "topics.yaml")

    assert [t.id for t in config.topics] == ["claude_code", "llm"]


# ------------------------------------------------------------ digest（specs/009）

DIGEST = """\
digest:
  size: 6
  limits: {claude_code: 3, llm: 2, other: 1}
  update_keywords: [新機能, release]
"""


def test_digest_is_loaded(tmp_path: Path) -> None:
    """AC-009-1"""
    digest = load_topics(write(tmp_path, VALID + DIGEST)).digest

    assert digest.size == 6
    assert digest.limits == {"claude_code": 3, "llm": 2, "other": 1}
    assert digest.update_keywords == ["新機能", "release"]


def test_digest_defaults(tmp_path: Path) -> None:
    """AC-009-1"""
    digest = load_topics(write(tmp_path, VALID)).digest

    assert digest.size == 8
    assert digest.limits == {"claude_code": 4, "llm": 3, "other": 2}
    assert digest.update_keywords == ["リリース", "新機能", "アップデート", "release", "changelog"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("other: 1}", "x: 1}"),
        ("other: 1}", "other: 1, paper: 1}"),
        ("size: 6", "size: 0"),
        ("llm: 2", "llm: 0"),
        ("[新機能, release]", '[新機能, ""]'),
    ],
    ids=["missing-other", "unknown-group", "zero-size", "zero-limit", "empty-keyword"],
)
def test_invalid_digest_raises_config_error(tmp_path: Path, old: str, new: str) -> None:
    """AC-009-2"""
    assert old in DIGEST
    with pytest.raises(ConfigError):
        load_topics(write(tmp_path, VALID + DIGEST.replace(old, new, 1)))


def test_repository_topics_yaml_digest() -> None:
    """AC-009-3"""
    digest = load_topics(PROJECT_ROOT / "topics.yaml").digest

    assert digest.size == 8
    assert digest.limits == {"claude_code": 4, "llm": 3, "other": 2}
    assert digest.update_keywords == ["リリース", "新機能", "アップデート", "release", "changelog"]
