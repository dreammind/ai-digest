"""設定の読み込み（specs/001-config.md）。

- `.env` / 環境変数 → `Settings`
- `sources.yaml` → `LoadedSources`（検証を通ったソースと、除外したエントリ）
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """設定を読み込めず、処理を続行できないときに送出する。"""


# ------------------------------------------------------------------- .env


def _settings_config(env_file: Path | None) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_file=env_file,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )


class Settings(BaseSettings):
    """`.env` / 環境変数から読み込む設定。秘密情報は `SecretStr` で持つ。"""

    model_config = _settings_config(Path(".env"))

    anthropic_api_key: SecretStr | None = None
    smtp_host: str | None = None
    smtp_port: Annotated[int, Field(ge=1, le=65535)] | None = None
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    mail_from: str | None = None
    mail_to: str | None = None
    sources_path: Path = Path("sources.yaml")
    db_path: Path = Path("data/aidigest.db")
    user_agent: str = "aidigest/0.1 (+https://github.com/dreammind/ai-digest)"
    http_timeout_seconds: Annotated[float, Field(gt=0)] = 20
    http_retries: Annotated[int, Field(ge=0)] = 2
    http_backoff_seconds: Annotated[float, Field(ge=0)] = 1.0
    host_interval_seconds: Annotated[float, Field(ge=0)] = 1.0
    time_window_hours: Annotated[int, Field(gt=0)] = 36
    topics_path: Path = Path("topics.yaml")
    llm_model: str = "claude-haiku-5-5"
    llm_timeout_seconds: Annotated[float, Field(gt=0)] = 60
    llm_max_retries: Annotated[int, Field(ge=0)] = 2
    budget_daily_usd: Annotated[float, Field(ge=0)] = 0.15
    budget_monthly_usd: Annotated[float, Field(ge=0)] = 2.0
    usd_jpy_rate: Annotated[float, Field(gt=0)] = 160
    pricing_path: Path = Path("pricing.yaml")


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    """`Settings` を作る。値の型が不正なら、入力値を含めずに `ConfigError` を送出する。"""

    # `Settings(_env_file=...)` は実行時には使えるが、pyright が生成する __init__ の型に
    # 含まれないため、.env のパスだけを差し替えたサブクラスで読み込む
    class _SettingsFromFile(Settings):
        model_config = _settings_config(env_file)

    try:
        return _SettingsFromFile()
    except ValidationError as e:
        raise ConfigError(f"設定値が不正です: {_summarize(e)}") from None


# ----------------------------------------------------------- sources.yaml

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
SourceCategory = Literal["official", "community", "paper", "japanese"]
QueryOperator = Literal["and", "or"]


def _str_to_list(value: object) -> object:
    """`query` に文字列 1 つが書かれていたら、要素 1 つのリストにそろえる。"""
    return [value] if isinstance(value, str) else value


Query = Annotated[list[NonEmptyStr], Field(min_length=1), BeforeValidator(_str_to_list)]


class _SourceBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyStr
    category: SourceCategory
    priority: Annotated[int, Field(ge=1, le=3)]
    max_items: Annotated[int, Field(ge=1)]
    require_keywords: list[str] = Field(default_factory=list[str])
    hide_keywords: list[str] = Field(default_factory=list[str])
    highlight_keywords: list[str] = Field(default_factory=list[str])
    recheck_changed: bool = False


class RssSource(_SourceBase):
    type: Literal["rss"]
    url: HttpUrl


class GithubReleaseSource(_SourceBase):
    type: Literal["github_release"]
    repo: Annotated[str, StringConstraints(pattern=r"^[\w.-]+/[\w.-]+$")]


class HnSearchSource(_SourceBase):
    type: Literal["hn_search"]
    query: Query
    query_operator: QueryOperator = "and"
    min_score: Annotated[int, Field(ge=0)] | None = None


class ArxivSource(_SourceBase):
    type: Literal["arxiv"]
    query: Query
    query_operator: QueryOperator = "and"


class QiitaSearchSource(_SourceBase):
    type: Literal["qiita_search"]
    query: NonEmptyStr  # Qiita の検索構文をそのまま書く（例: tag:ClaudeCode）
    min_stocks: Annotated[int, Field(ge=0)] = 3
    lookback_days: Annotated[int, Field(ge=1)] = 7


class ZennTopicSource(_SourceBase):
    type: Literal["zenn_topic"]
    topic: NonEmptyStr  # Zenn のトピック名（例: claudecode）
    min_likes: Annotated[int, Field(ge=0)] = 5
    lookback_days: Annotated[int, Field(ge=1)] = 7
    pages: Annotated[int, Field(ge=1)] = 2


Source = Annotated[
    RssSource
    | GithubReleaseSource
    | HnSearchSource
    | ArxivSource
    | QiitaSearchSource
    | ZennTopicSource,
    Field(discriminator="type"),
]

_SOURCE_ADAPTER = TypeAdapter[Source](Source)
_ENTRY_ADAPTER = TypeAdapter(dict[str, object])


class _SourcesFile(BaseModel):
    sources: list[object]
    exclude_keywords: list[NonEmptyStr] = Field(default_factory=list[str])


@dataclass(frozen=True)
class SourceError:
    """除外したエントリ。`index` は `sources` リスト内の位置（0 始まり）。"""

    index: int
    source_id: str | None
    reason: str


@dataclass(frozen=True)
class LoadedSources:
    sources: list[Source]
    errors: list[SourceError]
    exclude_keywords: list[str] = field(default_factory=list[str])  # 全ソース共通の除外キーワード


def load_sources(path: Path) -> LoadedSources:
    """`sources.yaml` を読み、エントリごとに検証する。不正なエントリだけを除外する。"""
    parsed = _load_yaml_as(path, _SourcesFile)

    sources: list[Source] = []
    errors: list[SourceError] = []
    seen_ids: set[str] = set()

    for index, entry in enumerate(parsed.sources):
        source_id: str | None = None
        try:
            mapping = _ENTRY_ADAPTER.validate_python(entry)
            raw_id = mapping.get("id")
            source_id = raw_id if isinstance(raw_id, str) else None
            source = _SOURCE_ADAPTER.validate_python(mapping)
        except ValidationError as e:
            errors.append(SourceError(index, source_id, _summarize(e)))
            continue

        if source.id in seen_ids:
            errors.append(SourceError(index, source_id, f"id '{source.id}' が重複しています"))
            continue
        seen_ids.add(source.id)
        sources.append(source)

    for error in errors:
        logger.error(
            "sources.yaml のエントリを除外しました（index=%d, id=%s）: %s",
            error.index,
            error.source_id,
            error.reason,
        )
    return LoadedSources(sources, errors, list(parsed.exclude_keywords))


def _load_yaml_as[M: BaseModel](path: Path, model: type[M]) -> M:
    """YAML ファイルを読んで `model` で検証する。読めない・不正なら `ConfigError` を送出する。"""
    try:
        data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        return model.model_validate(data)
    except OSError as e:
        raise ConfigError(f"{path} を読み込めません: {e}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} は YAML として不正です: {e}") from None
    except ValidationError as e:
        raise ConfigError(f"{path} の形式が不正です: {_summarize(e)}") from None


def _summarize(error: ValidationError) -> str:
    """検証エラーを、入力値を含めずに要約する（秘密情報を漏らさないため）。"""
    return "; ".join(
        f"{'.'.join(str(loc) for loc in detail['loc'])}: {detail['msg']}"
        for detail in error.errors(include_input=False, include_url=False)
    )


# ------------------------------------------------------------ topics.yaml

NonNegativeFloat = Annotated[float, Field(ge=0)]


class ScoringParams(BaseModel):
    """採点のパラメータ（specs/006-local-score.md）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    priority_bonus: dict[int, float]
    high_points: NonNegativeFloat
    medium_points: NonNegativeFloat
    negative_points: NonNegativeFloat
    highlight_points: NonNegativeFloat
    content_factor: Annotated[float, Field(ge=0, le=1)]
    max_hits: Annotated[int, Field(ge=1)]
    min_score: float
    similarity_threshold: Annotated[float, Field(gt=0, le=1)]
    ngram: Annotated[int, Field(ge=1)]

    @field_validator("priority_bonus")
    @classmethod
    def _covers_all_priorities(cls, value: dict[int, float]) -> dict[int, float]:
        if set(value) != {1, 2, 3}:
            raise ValueError("priority 1・2・3 のすべてに値が必要です")
        return value


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NonEmptyStr
    weight: Annotated[float, Field(gt=0)]
    high: list[NonEmptyStr]
    medium: list[NonEmptyStr] = Field(default_factory=list[str])


class TopicsConfig(BaseModel):
    """`topics.yaml` の内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scoring: ScoringParams
    topics: list[Topic]
    negative: list[NonEmptyStr] = Field(default_factory=list[str])

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [topic.id for topic in self.topics]
        if len(ids) != len(set(ids)):
            raise ValueError(f"テーマの id が重複しています: {ids}")
        return self


def load_topics(path: Path) -> TopicsConfig:
    """`topics.yaml` を読み、検証する。不正なら `ConfigError` を送出する。"""
    return _load_yaml_as(path, TopicsConfig)


# ------------------------------------------------------------ pricing.yaml


class Prices(BaseModel):
    """USD / 100 万トークンの単価（specs/007-llm-client.md）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: NonNegativeFloat
    output: NonNegativeFloat
    cache_write_5m: NonNegativeFloat
    cache_write_1h: NonNegativeFloat
    cache_read: NonNegativeFloat


class LongContextPrices(Prices):
    """プロンプトが `threshold` トークンを超えるリクエストの単価。"""

    threshold: Annotated[int, Field(ge=1)]


class ModelPricing(Prices):
    long_context: LongContextPrices | None = None


class PricingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    models: dict[str, ModelPricing]


def load_pricing(path: Path) -> PricingConfig:
    """`pricing.yaml` を読み、検証する。不正なら `ConfigError` を送出する。"""
    return _load_yaml_as(path, PricingConfig)
