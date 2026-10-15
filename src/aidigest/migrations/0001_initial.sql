-- 初期スキーマ（SPEC.md §7 / specs/002-db.md）
-- 日時はすべて UTC の ISO 8601 文字列

CREATE TABLE runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    fetched_count  INTEGER,
    selected_count INTEGER,
    sent_count     INTEGER,
    error          TEXT
);

CREATE TABLE articles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT    NOT NULL UNIQUE,
    source_id      TEXT    NOT NULL,
    category       TEXT,
    title          TEXT    NOT NULL,
    published_at   TEXT,
    fetched_at     TEXT    NOT NULL,
    content_hash   TEXT    NOT NULL,
    local_score    REAL,
    llm_score      REAL,
    final_score    REAL,
    matched_topics TEXT,
    summary_json   TEXT,
    score_model    TEXT,
    summary_model  TEXT,
    status         TEXT    NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'scored', 'selected', 'summarized', 'sent', 'skipped'))
);

CREATE INDEX idx_articles_fetched_at ON articles (fetched_at);

CREATE TABLE digests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs (id),
    created_at    TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN ('sent', 'dry_run', 'empty', 'failed')),
    article_count INTEGER NOT NULL,
    html          TEXT,
    article_ids   TEXT
);

CREATE TABLE llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs (id),
    purpose       TEXT    NOT NULL CHECK (purpose IN ('rerank', 'summarize')),
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE INDEX idx_llm_calls_created_at ON llm_calls (created_at);

CREATE TABLE source_health (
    run_id     INTEGER NOT NULL REFERENCES runs (id),
    source_id  TEXT    NOT NULL,
    status     TEXT    NOT NULL CHECK (status IN ('ok', 'error')),
    error      TEXT,
    item_count INTEGER NOT NULL,
    PRIMARY KEY (run_id, source_id)
);
