-- 0002: application tracking with append-only status history.

CREATE TABLE applications (
    id              INTEGER PRIMARY KEY,
    opportunity_id  INTEGER NOT NULL UNIQUE REFERENCES opportunities (id) ON DELETE RESTRICT,
    status          TEXT NOT NULL CHECK (status IN (
                        'preparing', 'submitted', 'assessment', 'interviewing',
                        'offer', 'rejected', 'withdrawn')),
    submitted_on    TEXT,                     -- YYYY-MM-DD, entered by the user
    deadline        TEXT,                     -- YYYY-MM-DD
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE application_events (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications (id) ON DELETE RESTRICT,
    at              TEXT NOT NULL,
    from_status     TEXT,                     -- NULL for the creation event
    to_status       TEXT NOT NULL,            -- equals from_status for note-only events
    note            TEXT NOT NULL DEFAULT ''
);

CREATE INDEX application_events_by_app ON application_events (application_id, id);

-- An application always stays attached to the opportunity it was created for.
CREATE TRIGGER applications_opportunity_fixed
BEFORE UPDATE OF opportunity_id ON applications
WHEN NEW.opportunity_id IS NOT OLD.opportunity_id
BEGIN
    SELECT RAISE(ABORT, 'An application cannot be moved to a different opportunity');
END;

CREATE TRIGGER application_events_no_update
BEFORE UPDATE ON application_events
BEGIN
    SELECT RAISE(ABORT, 'Application history is append-only');
END;

CREATE TRIGGER application_events_no_delete
BEFORE DELETE ON application_events
BEGIN
    SELECT RAISE(ABORT, 'Application history is append-only');
END;
