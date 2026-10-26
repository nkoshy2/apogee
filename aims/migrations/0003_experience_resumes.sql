-- 0003: verified experience database, skills, and generated resume documents.

CREATE TABLE experiences (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN (
                      'education', 'employment', 'research', 'project', 'leadership', 'achievement')),
    title         TEXT NOT NULL,              -- role, degree, project, or award
    organization  TEXT NOT NULL DEFAULT '',
    location      TEXT NOT NULL DEFAULT '',
    start_date    TEXT,                       -- YYYY-MM, entered by the user
    end_date      TEXT,                       -- YYYY-MM (for education with is_current: expected)
    is_current    INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    protected     INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),  -- always on the resume
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Resume statements. Only 'approved' statements are ever placed on a resume.
CREATE TABLE statements (
    id             INTEGER PRIMARY KEY,
    experience_id  INTEGER NOT NULL REFERENCES experiences (id) ON DELETE RESTRICT,
    text           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'verified', 'approved')),
    protected      INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
    tags_json      TEXT NOT NULL DEFAULT '[]',  -- extra user tags for relevance matching
    sort_order     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX statements_by_experience ON statements (experience_id, sort_order, id);

CREATE TABLE skills (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL COLLATE NOCASE UNIQUE,
    category    TEXT NOT NULL DEFAULT 'Technical',
    created_at  TEXT NOT NULL
);

-- Skills used to live in the profile setting (Phase 2). Carry them over.
INSERT OR IGNORE INTO skills (name, created_at)
SELECT trim(j.value), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
FROM settings s, json_each(s.value_json, '$.skills') j
WHERE s.key = 'profile' AND json_type(s.value_json, '$.skills') = 'array' AND trim(j.value) <> '';

-- A generated resume. Rows are only written for successful, validated generations and are
-- never updated: content_json is the full snapshot that was rendered, so the document can be
-- reproduced even after the underlying statements change.
CREATE TABLE resume_documents (
    id                INTEGER PRIMARY KEY,
    opportunity_id    INTEGER REFERENCES opportunities (id) ON DELETE RESTRICT,
    template_version  TEXT NOT NULL,
    page_limit        INTEGER NOT NULL,
    page_count        INTEGER NOT NULL,
    content_json      TEXT NOT NULL,
    validation_json   TEXT NOT NULL,
    folder            TEXT NOT NULL UNIQUE,     -- under <data>/documents/
    tex_sha256        TEXT NOT NULL,
    pdf_sha256        TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE resume_statements (
    resume_id     INTEGER NOT NULL REFERENCES resume_documents (id) ON DELETE RESTRICT,
    statement_id  INTEGER NOT NULL REFERENCES statements (id) ON DELETE RESTRICT,
    position      INTEGER NOT NULL,
    PRIMARY KEY (resume_id, statement_id)
);

CREATE TRIGGER resume_documents_immutable
BEFORE UPDATE ON resume_documents
BEGIN
    SELECT RAISE(ABORT, 'Generated resumes are immutable; generate a new one instead');
END;

ALTER TABLE applications ADD COLUMN resume_document_id INTEGER REFERENCES resume_documents (id);
