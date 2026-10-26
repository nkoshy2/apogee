-- 0001: settings and opportunities.
-- Do not edit after release; add a new numbered migration instead.
-- No BEGIN/COMMIT here: the runner wraps each file in a transaction.

CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE opportunities (
    id                 INTEGER PRIMARY KEY,

    -- Source-owned columns: the only ones an import may write.
    source             TEXT NOT NULL,              -- 'manual', 'greenhouse', 'lever', ...
    source_key         TEXT NOT NULL DEFAULT '',   -- board token / company slug; '' for manual
    source_posting_id  TEXT,                       -- NULL for manual entries
    employer           TEXT NOT NULL,
    title              TEXT NOT NULL,
    url                TEXT NOT NULL,
    url_key            TEXT NOT NULL,              -- normalized URL used for duplicate detection
    location           TEXT NOT NULL DEFAULT '',
    opportunity_type   TEXT NOT NULL DEFAULT 'internship',
    description        TEXT NOT NULL DEFAULT '',
    deadline           TEXT,                       -- YYYY-MM-DD
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    listing_status     TEXT NOT NULL DEFAULT 'active'
                       CHECK (listing_status IN ('active', 'not_seen', 'closed')),
    missing_since      TEXT,

    -- User-owned columns: never written by an import.
    user_state         TEXT NOT NULL DEFAULT 'new'
                       CHECK (user_state IN ('new', 'saved', 'dismissed')),
    user_notes         TEXT NOT NULL DEFAULT '',

    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,

    UNIQUE (source, source_key, source_posting_id)
);

CREATE INDEX opportunities_url_key ON opportunities (url_key);
CREATE INDEX opportunities_user_state ON opportunities (user_state);
