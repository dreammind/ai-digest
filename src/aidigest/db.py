"""SQLite の接続とマイグレーション（specs/002-db.md）。"""

import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

logger = logging.getLogger(__name__)

_MIGRATION_FILE = re.compile(r"^(\d{4})_(\w+)\.sql$")


class DbError(Exception):
    """DB を開けない、またはマイグレーションを適用できないときに送出する。"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    """同梱の `aidigest/migrations/NNNN_<name>.sql` を番号順に読む。"""
    migrations: list[Migration] = []
    for entry in resources.files("aidigest.migrations").iterdir():
        match = _MIGRATION_FILE.match(entry.name)
        if match is None:
            continue
        version, name = match.groups()
        migrations.append(Migration(int(version), name, entry.read_text(encoding="utf-8")))
    return sorted(migrations, key=lambda m: m.version)


def open_db(path: Path, migrations: Sequence[Migration] | None = None) -> sqlite3.Connection:
    """DB を開き、未適用のマイグレーションを適用した接続を返す。"""
    pending = list(load_migrations() if migrations is None else migrations)
    _check_numbering(pending)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    except (OSError, sqlite3.Error) as e:
        raise DbError(f"DB を開けません: {path}: {e}") from e

    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _migrate(conn, pending)
    except sqlite3.Error as e:
        conn.close()
        raise DbError(f"DB を開けません: {path}: {e}") from e
    except DbError:
        conn.close()
        raise
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    """適用済みのマイグレーションの最大番号（未適用なら 0）。"""
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(row[0])


def _check_numbering(migrations: Sequence[Migration]) -> None:
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise DbError(f"マイグレーションの番号が 1 からの連番になっていません: {versions}")


def _migrate(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()

    current = current_version(conn)
    latest = migrations[-1].version if migrations else 0
    if current > latest:
        raise DbError(f"DB のバージョン {current} は、このコードの最新 {latest} より新しいです")

    for migration in migrations:
        if migration.version <= current:
            continue
        _apply(conn, migration)


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    """1 つのマイグレーションを、`schema_version` への記録と同じトランザクションで適用する。"""
    applied_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        conn.executescript(f"BEGIN;\n{migration.sql}")
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (migration.version, applied_at),
        )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise DbError(
            f"マイグレーション {migration.version:04d}_{migration.name} に失敗しました: {e}"
        ) from e
    logger.info("マイグレーション %04d_%s を適用しました", migration.version, migration.name)
