"""SQLite のスキーマとマイグレーション（specs/002-db.md）のテスト。"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aidigest.config import load_settings
from aidigest.db import DbError, Migration, current_version, load_migrations, open_db

SPEC_COLUMNS = {
    "runs": {
        "id",
        "started_at",
        "finished_at",
        "fetched_count",
        "selected_count",
        "sent_count",
        "error",
    },
    "articles": {
        "id",
        "url",
        "source_id",
        "category",
        "title",
        "published_at",
        "fetched_at",
        "content_hash",
        "local_score",
        "llm_score",
        "final_score",
        "matched_topics",
        "summary_json",
        "score_model",
        "summary_model",
        "status",
    },
    "digests": {
        "id",
        "run_id",
        "created_at",
        "status",
        "article_count",
        "html",
        "article_ids",
    },
    "llm_calls": {
        "id",
        "run_id",
        "purpose",
        "model",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "created_at",
    },
    "source_health": {"run_id", "source_id", "status", "error", "item_count"},
}

NOW = "2026-10-15T21:30:00+00:00"

M1 = Migration(1, "create_t1", "CREATE TABLE t1 (x INTEGER);")
M2 = Migration(2, "create_t2", "CREATE TABLE t2 (x INTEGER);")
M3 = Migration(3, "create_t3", "CREATE TABLE t3 (x INTEGER);")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(db_path)
    yield connection
    connection.close()


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def versions(path: Path) -> list[int]:
    with sqlite3.connect(path) as raw:
        return [int(r[0]) for r in raw.execute("SELECT version FROM schema_version ORDER BY 1")]


def insert_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (NOW,))
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def insert_article(conn: sqlite3.Connection, url: str, status: str = "new") -> None:
    conn.execute(
        "INSERT INTO articles (url, source_id, title, fetched_at, content_hash, status)"
        " VALUES (?, 'src', 'title', ?, 'hash', ?)",
        (url, NOW, status),
    )


# ------------------------------------------------------------ マイグレーション


def test_new_db_gets_all_tables_and_latest_version(tmp_path: Path) -> None:
    """AC-002-1"""
    path = tmp_path / "nested" / "dir" / "aidigest.db"

    conn = open_db(path)

    assert path.is_file()
    assert set(SPEC_COLUMNS) | {"schema_version"} <= table_names(conn)
    latest = max(m.version for m in load_migrations())
    assert current_version(conn) == latest
    applied_at = conn.execute("SELECT applied_at FROM schema_version").fetchone()["applied_at"]
    assert datetime.fromisoformat(applied_at).utcoffset() == UTC.utcoffset(None)
    conn.close()


def test_bundled_migrations_are_numbered_from_one() -> None:
    """AC-002-1 / AC-002-6: 同梱のマイグレーションは 1 からの連番になっている"""
    migrations = load_migrations()

    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "initial"


def test_reopening_latest_db_keeps_data(db_path: Path) -> None:
    """AC-002-2"""
    conn = open_db(db_path)
    run_id = insert_run(conn)
    conn.commit()
    conn.close()
    before = versions(db_path)

    conn = open_db(db_path)

    assert versions(db_path) == before
    row = conn.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["started_at"] == NOW
    conn.close()


def test_only_pending_migrations_are_applied(db_path: Path) -> None:
    """AC-002-3: M1 を再適用すると CREATE TABLE が失敗するので、再適用されないことも確かめられる"""
    open_db(db_path, migrations=[M1]).close()

    conn = open_db(db_path, migrations=[M1, M2])

    assert current_version(conn) == 2
    assert {"t1", "t2"} <= table_names(conn)
    conn.close()
    assert versions(db_path) == [1, 2]


def test_failed_migration_is_rolled_back(db_path: Path) -> None:
    """AC-002-4"""
    open_db(db_path, migrations=[M1]).close()
    broken = Migration(
        2, "broken", "CREATE TABLE t2 (x INTEGER);\nINSERT INTO no_such_table VALUES (1);"
    )

    with pytest.raises(DbError):
        open_db(db_path, migrations=[M1, broken])

    assert versions(db_path) == [1]
    with sqlite3.connect(db_path) as raw:
        assert table_names(raw) >= {"t1"}
        assert "t2" not in table_names(raw)


def test_db_newer_than_code_is_rejected(db_path: Path) -> None:
    """AC-002-5"""
    open_db(db_path, migrations=[M1, M2]).close()
    before = db_path.read_bytes()

    with pytest.raises(DbError):
        open_db(db_path, migrations=[M1])

    assert db_path.read_bytes() == before


@pytest.mark.parametrize(
    "migrations",
    [[M1, M3], [M1, M1], [M2, M3], [M2, M1]],
    ids=["gap", "duplicate", "not-from-one", "unordered"],
)
def test_invalid_migration_numbering_is_rejected(
    db_path: Path, migrations: list[Migration]
) -> None:
    """AC-002-6: 何も適用されない"""
    with pytest.raises(DbError):
        open_db(db_path, migrations=migrations)

    if db_path.exists():
        with sqlite3.connect(db_path) as raw:
            assert table_names(raw).isdisjoint({"t1", "t2", "t3"})


def test_unopenable_path_raises_db_error(tmp_path: Path) -> None:
    """AC-002-12"""
    with pytest.raises(DbError):
        open_db(tmp_path)


# ------------------------------------------------------------ 初期スキーマ


def test_tables_have_spec_columns(conn: sqlite3.Connection) -> None:
    """AC-002-7"""
    for table, expected in SPEC_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        assert {str(r["name"]) for r in rows} == expected, table


def test_article_url_is_unique(conn: sqlite3.Connection) -> None:
    """AC-002-8"""
    insert_article(conn, "https://example.com/a")

    with pytest.raises(sqlite3.IntegrityError):
        insert_article(conn, "https://example.com/a")


def test_article_status_defaults_to_new(conn: sqlite3.Connection) -> None:
    """AC-002-9 の補足: status の既定値は new"""
    conn.execute(
        "INSERT INTO articles (url, source_id, title, fetched_at, content_hash)"
        " VALUES ('https://example.com/a', 'src', 't', ?, 'h')",
        (NOW,),
    )

    row = conn.execute("SELECT status FROM articles").fetchone()
    assert row["status"] == "new"


@pytest.mark.parametrize("status", ["new", "scored", "selected", "summarized", "sent", "skipped"])
def test_article_accepts_allowed_status(conn: sqlite3.Connection, status: str) -> None:
    """AC-002-9: 許可された値は入る"""
    insert_article(conn, f"https://example.com/{status}", status)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO articles (url, source_id, title, fetched_at, content_hash, status)"
        " VALUES ('u', 's', 't', :now, 'h', 'bogus')",
        "INSERT INTO digests (run_id, created_at, status, article_count)"
        " VALUES (:run_id, :now, 'bogus', 0)",
        "INSERT INTO llm_calls (run_id, purpose, model, input_tokens, output_tokens,"
        " cost_usd, created_at) VALUES (:run_id, 'bogus', 'm', 1, 1, 0.0, :now)",
        "INSERT INTO source_health (run_id, source_id, status, item_count)"
        " VALUES (:run_id, 's', 'bogus', 0)",
    ],
    ids=["articles.status", "digests.status", "llm_calls.purpose", "source_health.status"],
)
def test_disallowed_enum_values_are_rejected(conn: sqlite3.Connection, sql: str) -> None:
    """AC-002-9"""
    run_id = insert_run(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, {"run_id": run_id, "now": NOW})


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO digests (run_id, created_at, status, article_count)"
        " VALUES (999, :now, 'sent', 0)",
        "INSERT INTO llm_calls (run_id, purpose, model, input_tokens, output_tokens,"
        " cost_usd, created_at) VALUES (999, 'rerank', 'm', 1, 1, 0.0, :now)",
        "INSERT INTO source_health (run_id, source_id, status, item_count)"
        " VALUES (999, 's', 'ok', 0)",
    ],
    ids=["digests", "llm_calls", "source_health"],
)
def test_unknown_run_id_violates_foreign_key(conn: sqlite3.Connection, sql: str) -> None:
    """AC-002-10"""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, {"now": NOW})


def test_source_health_is_unique_per_run_and_source(conn: sqlite3.Connection) -> None:
    """AC-002-10 の補足: source_health の主キーは (run_id, source_id)"""
    run_id = insert_run(conn)
    sql = (
        "INSERT INTO source_health (run_id, source_id, status, item_count) VALUES (?, 's', 'ok', 0)"
    )
    conn.execute(sql, (run_id,))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (run_id,))


# ------------------------------------------------------------ 設定


def test_db_path_defaults_to_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-002-11"""
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    assert load_settings(tmp_path / "no-such.env").db_path == Path("data/aidigest.db")


def test_db_path_can_be_overridden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-002-11"""
    monkeypatch.setenv("DB_PATH", "/var/lib/aidigest/app.db")

    assert load_settings(tmp_path / "no-such.env").db_path == Path("/var/lib/aidigest/app.db")
