-- 0005: change tracking for safe restores.
-- meta.data_revision counts every change to user data and meta.changed_at records when the last one
-- happened. A restore compares changed_at with the backup's time so it can never silently overwrite
-- newer work. Any new user-data table must get the same three triggers in its own migration.

CREATE TABLE meta (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    data_revision  INTEGER NOT NULL,
    changed_at     TEXT NOT NULL
);
INSERT INTO meta (id, data_revision, changed_at) VALUES (1, 0, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

CREATE TRIGGER rev_settings_insert AFTER INSERT ON settings BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_settings_update AFTER UPDATE ON settings BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_settings_delete AFTER DELETE ON settings BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_opportunities_insert AFTER INSERT ON opportunities BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_opportunities_update AFTER UPDATE ON opportunities BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_opportunities_delete AFTER DELETE ON opportunities BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_applications_insert AFTER INSERT ON applications BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_applications_update AFTER UPDATE ON applications BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_applications_delete AFTER DELETE ON applications BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_application_events_insert AFTER INSERT ON application_events BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_application_events_update AFTER UPDATE ON application_events BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_application_events_delete AFTER DELETE ON application_events BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_experiences_insert AFTER INSERT ON experiences BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_experiences_update AFTER UPDATE ON experiences BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_experiences_delete AFTER DELETE ON experiences BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_statements_insert AFTER INSERT ON statements BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_statements_update AFTER UPDATE ON statements BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_statements_delete AFTER DELETE ON statements BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_skills_insert AFTER INSERT ON skills BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_skills_update AFTER UPDATE ON skills BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_skills_delete AFTER DELETE ON skills BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_resume_documents_insert AFTER INSERT ON resume_documents BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_resume_documents_update AFTER UPDATE ON resume_documents BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_resume_documents_delete AFTER DELETE ON resume_documents BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_resume_statements_insert AFTER INSERT ON resume_statements BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_resume_statements_update AFTER UPDATE ON resume_statements BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_resume_statements_delete AFTER DELETE ON resume_statements BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_sources_insert AFTER INSERT ON sources BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_sources_update AFTER UPDATE ON sources BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_sources_delete AFTER DELETE ON sources BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_source_checks_insert AFTER INSERT ON source_checks BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_source_checks_update AFTER UPDATE ON source_checks BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_source_checks_delete AFTER DELETE ON source_checks BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_imported_feed_runs_insert AFTER INSERT ON imported_feed_runs BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_imported_feed_runs_update AFTER UPDATE ON imported_feed_runs BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_imported_feed_runs_delete AFTER DELETE ON imported_feed_runs BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;

CREATE TRIGGER rev_opportunity_snapshots_insert AFTER INSERT ON opportunity_snapshots BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_opportunity_snapshots_update AFTER UPDATE ON opportunity_snapshots BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
CREATE TRIGGER rev_opportunity_snapshots_delete AFTER DELETE ON opportunity_snapshots BEGIN UPDATE meta SET data_revision = data_revision + 1, changed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1; END;
