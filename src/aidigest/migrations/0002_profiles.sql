-- プロファイル（SPEC.md §7 / specs/015-profiles.md）
-- runs と articles に profile を足し、articles の一意の制約を (profile, url) の組にする。
-- SQLite は制約を後から変えられないため、articles は作り直して行を移す（id は変えない）。

ALTER TABLE runs ADD COLUMN profile TEXT NOT NULL DEFAULT 'dev'
    CHECK (profile IN ('dev', 'current-events'));

CREATE TABLE articles_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT    NOT NULL,
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
        CHECK (status IN ('new', 'scored', 'selected', 'summarized', 'sent', 'skipped')),
    profile        TEXT    NOT NULL DEFAULT 'dev'
        CHECK (profile IN ('dev', 'current-events')),
    UNIQUE (profile, url)
);

INSERT INTO articles_new
    (id, url, source_id, category, title, published_at, fetched_at, content_hash,
     local_score, llm_score, final_score, matched_topics, summary_json,
     score_model, summary_model, status, profile)
SELECT
    id, url, source_id, category, title, published_at, fetched_at, content_hash,
    local_score, llm_score, final_score, matched_topics, summary_json,
    score_model, summary_model, status, 'dev'
FROM articles;

DROP TABLE articles;
ALTER TABLE articles_new RENAME TO articles;
CREATE INDEX idx_articles_fetched_at ON articles (fetched_at);
