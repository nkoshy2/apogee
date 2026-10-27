-- 0004: discovery sources, per-run source checks, imported feed runs, and posting snapshots.

CREATE TABLE sources (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,              -- 'greenhouse', 'lever'
    source_key     TEXT NOT NULL,              -- board token / company slug
    employer       TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    UNIQUE (source, source_key)
);

-- One row per source per run, failures included: this is the source-health record.
CREATE TABLE source_checks (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES sources (id) ON DELETE RESTRICT,
    run_id          TEXT NOT NULL,
    origin          TEXT NOT NULL,             -- 'cloud' | 'local' | 'file'
    checked_at      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    complete        INTEGER NOT NULL CHECK (complete IN (0, 1)),
    total_on_board  INTEGER,
    posting_count   INTEGER NOT NULL,
    new_count       INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
CREATE INDEX source_checks_by_source ON source_checks (source_id, checked_at);

CREATE TABLE imported_feed_runs (
    run_id        TEXT PRIMARY KEY,
    origin        TEXT NOT NULL,
    generated_at  TEXT NOT NULL,
    imported_at   TEXT NOT NULL,
    summary_json  TEXT NOT NULL
);

-- The audit trail: the canonical posting as received, stored only when its content changes.
CREATE TABLE opportunity_snapshots (
    id              INTEGER PRIMARY KEY,
    opportunity_id  INTEGER NOT NULL REFERENCES opportunities (id) ON DELETE RESTRICT,
    fetched_at      TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    data_json       TEXT NOT NULL
);
CREATE INDEX opportunity_snapshots_by_opp ON opportunity_snapshots (opportunity_id, id);

CREATE INDEX opportunities_by_source ON opportunities (source, source_key, listing_status);
