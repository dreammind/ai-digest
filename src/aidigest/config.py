"""設定の読み込み（specs/001-config.md）。

- `.env` / 環境変数 → `Settings`
- `sources.yaml` → `LoadedSources`（検証を通ったソースと、除外したエントリ）
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

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


Source = Annotated[
    RssSource | GithubReleaseSource | HnSearchSource | ArxivSource | QiitaSearchSource,
    Field(discriminator="type"),
]

_SOURCE_ADAPTER = TypeAdapter[Source](Source)
_ENTRY_ADAPTER = TypeAdapter(dict[str, object])


class _SourcesFile(BaseModel):
    sources: list[object]


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


def load_sources(path: Path) -> LoadedSources:
    """`sources.yaml` を読み、エントリごとに検証する。不正なエントリだけを除外する。"""
    try:
        data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        entries = _SourcesFile.model_validate(data).sources
    except OSError as e:
        raise ConfigError(f"{path} を読み込めません: {e}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} は YAML として不正です: {e}") from None
    except ValidationError as e:
        raise ConfigError(f"{path} に sources のリストがありません: {_summarize(e)}") from None

    sources: list[Source] = []
    errors: list[SourceError] = []
    seen_ids: set[str] = set()

    for index, entry in enumerate(entries):
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
    return LoadedSources(sources, errors)


def _summarize(error: ValidationError) -> str:
    """検証エラーを、入力値を含めずに要約する（秘密情報を漏らさないため）。"""
    return "; ".join(
        f"{'.'.join(str(loc) for loc in detail['loc'])}: {detail['msg']}"
        for detail in error.errors(include_input=False, include_url=False)
    )
