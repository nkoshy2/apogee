# AIMS: Technical Design

Aerospace Internship Management System. This is a personal, local-first, single-user app.
This document covers the spec review, the design, and the implementation plan. Significant
decisions and deviations are logged in [DECISIONS.md](DECISIONS.md).

Status legend: **Done** = implemented and tested. **Planned** = designed only.

---

## 1. Spec review: inconsistencies, gaps, risks

| # | Issue | Resolution |
|---|-------|------------|
| R1 | The home directory `C:\Users\nkosh` is itself a git repo (no commits). A repo there would capture `.ssh`, `NTUSER.DAT`, and similar files. | The project has its own repo (`git init` in the project folder). Private data lives **outside** the repo (R2). |
| R2 | Scheduled discovery runs from GitHub. If private data sat in the project folder, a single `git add .` could publish it. | Data directory defaults to `%LOCALAPPDATA%\AIMS` (database, documents, backups, logs). The repo holds only code, the template, and public source config. |
| R3 | GitHub Actions cron: public repos get free minutes, but scheduled workflows are **disabled after 60 days without repo activity**. Private repos get 2,000 free minutes/month, and fetching their feed locally needs a token. | Decide in Phase 4: a small public repo for discovery only (holds only the public company list and public postings). The feed commit itself counts as activity. Health shows a stale feed after N days without a run. |
| R4 | MiKTeX `latexmk` requires Perl. Perl exists only inside Git (`C:\Program Files\Git\usr\bin`), which is **not on PATH** when the app is launched from a shortcut. | Phase 3: the compiler first tries `latexmk`, then falls back to running `pdflatex` directly (2 passes). The health page reports which method is in use. |
| R5 | Greenhouse/Lever rarely include deadlines, opportunity type, or eligibility fields. | Deadline stays nullable and editable by hand. Type is classified by rules from the title (`intern`, `co-op`, ...) and shown as "inferred". Eligibility is mostly "uncertain" unless the text matches explicit patterns. This matches the spec: missing info never means ineligible. |
| R6 | "Restore must not silently overwrite newer info" needs a reliable notion of "newer". File mtimes are unreliable. | Phase 5: a `meta.data_revision` counter is bumped by triggers on every user-data table. Backups record the revision. If a restore would go back past newer live data, the user must confirm again, and a pre-restore safety backup is always taken. |
| R7 | "Daily backup when the computer is available" plus "catch up after missed runs" assumes a scheduler. | Back up on app start if the newest verified backup is older than 24 h, and recheck hourly while running. If the app is not running, the data cannot change, so nothing is lost. No Windows Task Scheduler is needed. |
| R8 | PDF validation (text extraction, reading order) needs a PDF reader. MiKTeX does not ship `pdftotext`. | Use `pypdf` (pure Python) in Phase 3. |
| R9 | Python 3.14 is very new. | Verified: fastapi, uvicorn, pydantic, pytest, and httpx install and run on 3.14. Dependencies are pinned in `requirements.txt`. |
| R10 | Discovery completeness (≥95%) needs a "verified snapshot" as ground truth. | Phase 6: hand-verify 3–5 boards against their public careers pages and store them as fixtures. Measure completeness separately from ranking. |
| R11 | Adapter feasibility is assumed. | Phase 4 starts with a probe script that checks each pilot company's board token and records the HTTP status and posting counts before an adapter is relied on. |
| R12 | A localhost server can be reached by any web page the user visits (CSRF, DNS rebinding). | **Done:** Host-header allowlist, a custom request header on every state-changing request, an Origin check, and a strict CSP. Untrusted text is rendered with `textContent` only, and only http(s) URLs are accepted. |

## 2. Material deviations from the spec

1. **No SQLAlchemy or Alembic** (the spec says "if justified"). Stdlib `sqlite3` plus numbered SQL migration
   files tracked with `PRAGMA user_version`. A backup is taken automatically before migrating an existing database. This saves two
   heavy dependencies for about 60 lines of code, and the SQLite backup API is used directly anyway.
2. **Data outside the repo** (R2).
3. **No OS-level scheduler for backups** (R7).

None of these remove a requirement.

## 3. Architecture

One Python package, `aims/`, with one module per responsibility. No business module imports `aims.web`.

```
aims/
  config.py          paths/port (env overrides: AIMS_DATA_DIR, AIMS_PORT)
  db.py              connection, transactions, migration runner          [Done]
  migrations/*.sql   schema, append-only                                 [Done: 0001]
  settings_store.py  user preferences (validated, JSON values)            [Done]
  opportunities.py   storage, normalization, dedup, user state           [Done: manual create/list]
  ranking.py         eligibility + relevance + preference scoring        [Planned P2]
  applications.py    tracking + status history                           [Planned P2]
  experience.py      verified experience + statements                    [Planned P3]
  resume/            selection, LaTeX render, compile, validate          [Planned P3]
  discovery/         adapters (greenhouse, lever), feed writer, importer [Planned P4]
  backup.py          create/verify/retain/restore                        [Planned P5]
  web/               FastAPI app, security middleware, static frontend   [Done: shell]
  server.py          single-instance lock + uvicorn                      [Done]
launcher.pyw         shortcut target: start-or-reuse server, open browser [Done]
discovery_cloud/     entrypoint run by GitHub Actions (imports aims.discovery only) [Planned P4]
```

Dependency rule: `web → (opportunities, ranking, applications, resume, backup, ...) → db`.
`discovery` depends only on `opportunities` normalization types. It never touches experience, resume, or applications.

## 4. Database schema

SQLite, WAL, `foreign_keys=ON`, `synchronous=FULL`. Timestamps are UTC ISO-8601 text. Dates are `YYYY-MM-DD`.

**0001 (Done)**

- `settings(key PK, value_json, updated_at)`
- `opportunities`
  - Source columns (the only ones an import may write): `source, source_key, source_posting_id, employer, title, url, url_key, location, opportunity_type, description, deadline, first_seen_at, last_seen_at, listing_status, missing_since`
  - User columns (never written by import): `user_state ('new'|'saved'|'dismissed'), user_notes`
  - Constraints: `UNIQUE(source, source_key, source_posting_id)`. Manual rows have a NULL posting id, so manual dedup uses `url_key` (normalized URL) and is enforced in code with a 409 response.
  - `listing_status`: `active` | `not_seen` (absent from a *successful complete* check) | `closed` (user-set). A failed check never changes it.

**Planned**

- P2: `applications(id, opportunity_id FK RESTRICT, status, submitted_on, deadline, notes, resume_document_id, created_at, updated_at)`, `application_events(id, application_id FK, at, from_status, to_status, note)`. History is append-only.
- P3: `education`, `experiences(kind: project|research|employment|achievement, title, org, start, end, ...)`, `skills(name, aliases)`, `experience_skills`, `statements(experience_id FK, text, status draft|verified|approved, protected, tags)`, `resume_documents(id, opportunity_id, template_version, selection_json, tex_sha256, pdf_sha256, page_count, status, created_at)`. Files are stored at `documents/<id>/resume.{tex,pdf}` and are never overwritten. A failed compile writes nothing to the approved document.
- P4: `sources(id, kind, source_key, employer, enabled)`, `source_checks(id, source_id, feed_run_id, checked_at, status ok|failed, complete, posting_count, error)`, `imported_feed_runs(run_id PK, imported_at)`, `opportunity_snapshots(opportunity_id, fetched_at, content_sha256, raw_json)` (written only when the content hash changes; this is the audit trail).
- P5: `meta(data_revision)` plus triggers.

## 5. Local HTTP API

All endpoints are under `/api`. JSON only. Mutations require the header `X-AIMS-Request: 1`.

| Endpoint | Status |
|---|---|
| `GET /api/health` (app id, version, schema version, data dir, db check) | Done |
| `GET/PUT /api/settings` | Done |
| `GET/POST /api/opportunities`, `GET /api/opportunities/{id}` | Done |
| `POST /api/shutdown` | Done |
| `PATCH /api/opportunities/{id}` (save/dismiss/notes), search/filter params, `/api/opportunities/{id}/ranking` | P2 |
| `/api/applications…`, `/api/applications/{id}/events` | P2 |
| `/api/experience…`, `/api/statements…`, `/api/resumes…` (+ PDF preview/export download) | P3 |
| `/api/discovery/sources`, `/api/discovery/import`, `/api/discovery/health` | P4 |
| `/api/backups` (list/create/verify/restore) | P5 |

## 6. Discovery feed (Phase 4)

GitHub Actions (cron, roughly every 12 h) runs the adapters and commits `feed/v1/runs/<run_id>.json` plus `feed/v1/latest.json`, which points to recent runs.

```json
{
  "schema": "aims-feed", "version": 1,
  "run_id": "2026-11-02T06-00-00Z", "generated_at": "...",
  "checks": [
    {"source": "greenhouse", "source_key": "<board>", "employer": "...",
     "status": "ok|failed", "complete": true, "error": null, "fetched_at": "...",
     "postings": [{"source_posting_id": "...", "title": "...", "url": "...",
                   "location": "...", "description": "...", "updated_at": "...",
                   "raw_sha256": "..."}]}
  ]
}
```

- Endpoints: Greenhouse `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`, Lever `api.lever.co/v0/postings/{company}?mode=json`. Both are public and unauthenticated. Each board token is validated by the probe script first.
- The local importer processes runs not listed in `imported_feed_runs`, oldest first, one transaction per run. Postings are upserted on `(source, source_key, source_posting_id)`, and only source columns are updated.
- A posting goes to `not_seen` only if the check is `ok` **and** `complete`. It returns to `active` if seen again.
- Manual adapter runs locally produce the same JSON, so there is a single import path.
- Contents: only public posting data and the company list. Nothing from the local DB is uploaded.

## 7. Ranking (Phase 2)

These are pure functions over the opportunity text plus the profile and preferences. Every point carries a reason string.

- **Eligibility:** regex rules (e.g. "U.S. citizen", "ITAR"/"U.S. person", "graduating between", "minimum GPA", class standing) are compared with profile facts. Result is `eligible`, `uncertain` (default when nothing matches), or `ineligible` (only on an explicit, matched disqualifier).
- **Relevance:** a skill-tag vocabulary with aliases (`config/skills.json`). Score = weighted overlap between tags found in the job and verified-skill tags, plus discipline match.
- **Preference:** preferred employers, locations, terms, and disciplines from settings.
- Ineligible and low-score items are sorted lower and labeled. They are never hidden or deleted.

## 8. Resume generation (Phase 3)

1. Candidates: approved statements only. Protected statements are always included.
2. Score each by tag overlap with the job (same vocabulary as ranking). Order optional sections and statements by score.
3. Render into one Jinja-free LaTeX template using simple `\VAR{}` placeholders and escape all text for LaTeX.
4. Compile into a temp dir. Check the page count with `pypdf`. If over the limit, drop the lowest-scoring optional statement and repeat (bounded). If protected content alone overflows, stop and report.
5. Validate: compile OK, page count, extractable text, section headings appear in order, and no `Overfull \hbox` above a threshold in the log.
6. On success, move to `documents/<id>/` and record the selection ids, template version, and hashes. On failure, keep the previous document.

## 9. Backup format (Phase 5)

`backups/aims-YYYYMMDD-HHMMSS.zip`, written as `*.partial` first, then verified, then renamed:

```
manifest.json   {format:1, created_at, app_version, schema_version, data_revision,
                 files: [{path, sha256, size}]}
aims.db         (sqlite3 backup API snapshot; PRAGMA integrity_check passes)
documents/...   generated resumes
config/...      user-editable config (template, skills vocabulary)
```

Excluded: logs, lock files, temp files, any tokens. Retention: delete backups older than 30 days, but only after a newer verified backup exists, and always keep the newest verified one. An optional mirror copy goes to a user-set folder (external drive or OneDrive). Restore: verify the archive, take a pre-restore backup, check `data_revision`, confirm, then swap the DB atomically.

## 10. External dependencies requiring validation

| Dependency | Status |
|---|---|
| Python 3.14 + FastAPI/uvicorn/pydantic | Validated |
| MiKTeX `pdflatex`, `latexmk` | Present. `latexmk` needs Perl not on PATH (R4) |
| Greenhouse / Lever public APIs for the pilot employers | Validate in P4 with the probe script |
| Non-Greenhouse/Lever priority employer, gov/research source | Feasibility probe only. Not blocking |
| GitHub Actions schedule + repo visibility | User decision in P4 (R3) |
| `pypdf` | Add in P3 |

## 11. Testing approach

`pytest`, with each test running against a temp data dir (`AIMS_DATA_DIR`). Layers:

- **Unit:** normalization, URL keys, ranking rules, LaTeX escaping, manifest checks.
- **Persistence/integration:** FastAPI `TestClient` against a real SQLite file. Restart is simulated by building a new app on the same directory.
- **Invariant/regression:** repeated import does not duplicate, import keeps user notes, a failed source check does not close postings, failed compile keeps the previous doc, a failed backup leaves the live DB intact. Every fixed bug gets a test.
- **Recovery suite** (P5): the 10 scenarios in spec §7, using fault injection (monkeypatched I/O errors, truncated zips).

## 12. Implementation plan

Each task leaves the app working and tested.

**Phase 1: Foundation (Done)**
1. Repo, venv, pinned deps, pytest config
2. Config and data-dir layout
3. DB connection, transactions, migration runner with pre-migration backup
4. Migration 0001 (settings, opportunities)
5. FastAPI app, security middleware, health/settings/opportunity endpoints
6. Static frontend shell with 4 tabs (Opportunities and Settings working)
7. Server single-instance lock, launcher, shortcut script
8. Tests: migrations, persistence across restart, security, duplicate URL, lock

**Phase 2: Opportunity management**
1. Search/filter/sort params
2. Save/dismiss/notes (PATCH)
3. Compare view (2–4 side by side)
4. Bulk JSON/CSV import through the same normalization path
5. Profile facts plus skills vocabulary
6. Ranking engine plus "why" panel
7. Applications plus event history
8. Applications tab

**Phase 3: Resume generation**
1. Experience/statement schema and CRUD with verify/approve
2. Template plus LaTeX escaping
3. Selection algorithm
4. Compiler with fallback
5. Page-fit loop
6. Validation
7. Document storage plus preview/export
8. Resume Studio UI

**Phase 4: Automated discovery**
1. Probe script plus pilot list
2. Canonical model
3. Greenhouse adapter
4. Lever adapter
5. Feed writer plus Actions workflow
6. Importer
7. Source health UI

**Phase 5: Reliability and recovery**
1. Revision triggers
2. Backup create/verify
3. Schedule plus retention
4. Mirror copy
5. Restore with guards
6. Recovery test suite
7. Backup UI

**Phase 6: End-to-end validation**
1. Ten-opportunity walkthrough
2. Discovery metrics against verified snapshots
3. Two-week observation log
4. Fix list
