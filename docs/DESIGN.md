# Apogee: Technical Design

Apogee (internal code name AIMS, the Aerospace Internship Management System) is a personal, local-first, single-user app.
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
  migrations/*.sql   schema, append-only                                 [Done: 0001-0003]
  settings_store.py  preferences, profile facts, resume header           [Done]
  summary.py         counts behind the sentence summaries and nav badges [Done]
  opportunities.py   storage, dedup, search/filter, edit, manual import  [Done]
  ranking.py         eligibility + relevance + preference scoring        [Done]
  data/skills.json   skill/discipline vocabulary used by ranking         [Done]
  applications.py    tracking + append-only status history               [Done]
  experience.py      experiences, statements (draft/verified/approved), skills [Done]
  resume/            plan.py select+order, latex.py escape+render, build.py
                     compile+validate, service.py generate+store, template.tex [Done]
  discovery/         model, greenhouse, lever, run, publish (cloud-safe, stdlib only);
                     importer (local), sources.json                    [Done]
  backup.py          create/verify/retain/mirror/restore/import, scheduler [Done]
  web/               FastAPI app, security middleware, static frontend   [Done]
  server.py          single-instance lock + uvicorn                      [Done]
launcher.pyw         shortcut target: start-or-reuse server, open browser [Done]
.github/workflows/discovery.yml  twice-daily cloud run -> discovery-feed branch [Written, not yet pushed]
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

**0002 (Done)**

- `applications(id, opportunity_id UNIQUE FK RESTRICT, status, submitted_on, deadline, notes, created_at, updated_at)`. One application per opportunity. Status is one of preparing, submitted, assessment, interviewing, offer, rejected, withdrawn. Any transition is allowed.
- `application_events(id, application_id FK, at, from_status, to_status, note)`. Creation has `from_status` NULL. A note-only event has `from = to`.
- Triggers enforce the integrity rules: events cannot be updated or deleted, and an application cannot move to a different opportunity. An opportunity with an application cannot be deleted (FK RESTRICT).

**0003 (Done)**

- `experiences(kind education|employment|research|project|leadership|achievement, title, organization, location, start_date, end_date (YYYY-MM), is_current, protected)`. Education lives here too (one table, simpler than a separate one).
- `statements(experience_id FK RESTRICT, text, status draft|verified|approved, protected, tags_json, sort_order)`.
- `skills(name UNIQUE NOCASE, category)`. The migration copies Phase 2's `profile.skills` into this table. Ranking reads skills from here (exposed read-only as `profile.skills`).
- `resume_documents(opportunity_id, template_version, page_limit, page_count, content_json, validation_json, folder UNIQUE, tex_sha256, pdf_sha256)`, immutable by trigger. Written only for validated generations. Files live in `documents/<folder>/resume.{tex,pdf,log}`.
- `resume_statements(resume_id, statement_id FK RESTRICT, position)`, so statements a resume used can't be deleted.
- `applications.resume_document_id` (which resume was sent). Attaching or detaching one is logged in the history.

**0004 (Done)**

- `sources(source, source_key UNIQUE, employer)`.
- `source_checks(source_id, run_id, origin cloud|local|file, checked_at, status, complete, total_on_board, posting_count, new_count, error)`. Failures are recorded too: this table is the source-health history.
- `imported_feed_runs(run_id PK, origin, generated_at, imported_at, summary_json)`.
- `opportunity_snapshots(opportunity_id, fetched_at, content_sha256, data_json)`, written only when a posting's content changes.

**0005 (Done)**

- `meta(id = 1, data_revision, changed_at)`, plus AFTER INSERT/UPDATE/DELETE triggers on every user-data table (39 triggers).

**Planned**

- (None outstanding.)

## 5. Local HTTP API

All endpoints are under `/api`. JSON only. Mutations require the header `X-AIMS-Request: 1`.

| Endpoint | Status |
|---|---|
| `GET /api/health` (app id, version, schema version, data dir, db check) | Done |
| `GET/PUT /api/settings` | Done |
| `GET /api/opportunities?q=&state=&opportunity_type=&eligibility=&sort=` (ranked list, no descriptions) | Done |
| `POST /api/opportunities`, `GET /api/opportunities/{id}` (with full ranking explanation) | Done |
| `PATCH /api/opportunities/{id}`: save/dismiss/notes always; details only for manual entries | Done |
| `POST /api/opportunities/import` (`{format: csv\|json, content}`, per-row result, never modifies existing) | Done |
| `GET/POST /api/applications`, `GET/PATCH /api/applications/{id}`, `POST /api/applications/{id}/events` | Done |
| `GET /api/summary` (counts for sentence summaries and nav badges) | Done |
| `GET/POST /api/experiences`, `PATCH/DELETE /api/experiences/{id}`, `POST /api/experiences/{id}/statements` | Done |
| `PATCH/DELETE /api/statements/{id}` (verify-before-approve; edits reset to draft; used ones can't be deleted) | Done |
| `GET/POST /api/skills`, `PATCH/DELETE /api/skills/{id}` | Done |
| `POST /api/resumes/plan` (proposed content with scores and reasons; `include_ids`/`exclude_ids`) | Done |
| `POST /api/resumes` (generate; 422 with reasons and checks on failure, nothing saved) | Done |
| `GET /api/resumes[?opportunity_id=]`, `GET /api/resumes/{id}`, `GET /api/resumes/{id}/pdf\|tex[?download=true]` | Done |
| `POST /api/shutdown` | Done |
| `GET /api/discovery/health`, `POST /api/discovery/run` (check boards from here), `POST /api/discovery/sync` (import cloud runs), `POST /api/discovery/import` (feed file) | Done |
| `GET/POST /api/backups`, `POST /api/backups/import` (upload), `POST /api/backups/{name}/verify`, `POST /api/backups/{name}/restore` (`confirm` = name, `allow_newer`) | Done |

## 6. Discovery (Phase 4, Done; cloud schedule awaiting repo hosting)

**Sources** (`aims/discovery/sources.json`, public): every board key was verified live on 2026-10-26, including the company name behind it. For example, `archer` is a veterinary clinic and `archer56` is Archer Aviation.
- Greenhouse (13): SpaceX, Relativity, Anduril, Rocket Lab, Astranis, Varda, Muon, Ursa Major, Inversion, Vast, Archer, HawkEye 360, Planet.
- Lever (3): Hermeus, Shield AI, Loft Orbital.

First live run: 16/16 sources complete, 287 student roles out of about 8,300 jobs, in about 6 s.

**Not automatable (feasibility probe):**
- Blue Origin's Lever board is empty. Like Boeing, Lockheed Martin, Northrop and GE Aerospace, it uses Workday or Taleo, which have no official public API. These are added manually.
- USAJOBS has an official API but needs a free personal API key (account registration), so it is deferred until wanted.
- NASA OSTEM internships have no API.

**Adapters** (standard library only):
- **Greenhouse:** list `/v1/boards/{key}/jobs`. The check is complete only if the returned count equals `meta.total`. Student roles are then read one by one from `/jobs/{id}` (four in parallel). A failed detail read makes the check partial, not failed.
- **Lever:** `/v0/postings/{key}?mode=json` returns everything in one response.
- **Student-role filter:** intern / internship / co-op / student, matched in the title or in Lever's commitment/team labels. Apprenticeships are excluded.
- **Untrusted content:** HTML is converted to text. URLs must be http(s).
- **Network limits:** each request has a size cap and a total time budget, and 5xx errors and timeouts are retried twice.

**Feed** (`schema: aims-feed`, version 1): `{run_id, origin, generated_at, checks: [{source, source_key, employer, status ok|failed, complete, error, fetched_at, total_on_board, postings: [...]}]}`.
- Each posting carries `source_api_url` and `raw_sha256` as its audit reference.
- `publish.py` keeps `latest.json` (an index with per-run SHA-256) plus `runs/<run_id>.json`. It prunes runs older than 14 days but always keeps the newest 4.
- The GitHub workflow force-pushes the feed as a single commit to a `discovery-feed` branch, so the repo never grows (about 1.9 MB per run).

**Import** (`importer.py`, local):
- Validated in full or rejected (pydantic). Each run is imported once, in one transaction.
- Upserted on `(source, source_key, source_posting_id)`. A matching manual entry (same normalized URL) is adopted rather than duplicated.
- Only source columns are written. A deadline the source stops stating is kept.
- `not_seen` is set only after an ok **and** complete check, and only for postings last seen before that check. A posting that reappears goes back to `active`. User-closed listings stay closed, and an older run never overwrites newer data.
- A snapshot is stored only when the content hash changes.

**Getting feeds:**
- Remote sync downloads unimported runs oldest first and verifies each SHA-256. A bad run is skipped and the others still import.
- Remote sync runs once per browser session when Apogee opens, or on demand.
- Other routes: "Run discovery here", or importing a feed file.

**Health:** per source: ok, partial, failing, stale (no complete check in 3 days), or never checked. It shows the last error, open and not-seen counts, and the time of the last cloud run.

**Privacy:** the cloud-run modules (`run.py`, `publish.py`, adapters) import only the standard library. A test fails if they ever import the database, settings, `sqlite3`, `pydantic` or `fastapi`. The feed contains only posting fields.

## 7. Ranking (Phase 2, Done)

`aims/ranking.py` contains pure functions over the title, description, profile, and preferences. Every point carries a reason string, and every eligibility finding quotes the sentence that triggered it.

- **Eligibility.** Per-sentence rules, applied to the title and description:
  - Rules: U.S. citizenship/clearance, U.S. person (ITAR/export control, "citizen or permanent resident"), no visa sponsorship, minimum GPA, graduation window, degree level.
  - A sentence containing "preferred / a plus / desired" makes the rule optional, so it never disqualifies. Negations ("not required", "regardless of") are skipped.
  - `ineligible` only if a mandatory rule is definitely failed. `uncertain` if a mandatory rule cannot be answered from the profile, or if there is no description. Otherwise `eligible`. When no rules are found, the result says so.
  - Degree mismatch disqualifies only with explicit "only" wording. The wording is too varied for anything else.
- **Relevance (0–100)** = role fit + skills + degree fit (retuned 2026-10-27, D16):
  - **Role fit (max 50), from the title.** `roles` in `aims/data/skills.json` maps title phrases to fields such as Propulsion, Structures, GNC, Test, Electrical/RF, Software, Civil, and Chemistry/bio, each with its disciplines.
    - Points: 50 if a field matches your disciplines; 40 for a general engineering title, or any engineering role when you set no disciplines; 20 for engineering in another discipline, or an unclear title.
    - Precedence: a technical field beats a business match ("Supplier Quality Engineer"), and a business match beats the bare word "engineer" ("Business Operations Engineer"). An alias inside a longer matched alias doesn't count.
    - Non-engineering roles (supply chain, finance, people, security analyst, government operations…) are capped at 10 relevance. They are labeled and sorted low, never hidden.
  - **Skills (max 30):** 0 / 15 / 24 / 30 for 0 / 1 / 2 / 3+ of your skills mentioned. Skills you don't list cost nothing (they're shown as "also mentions"). Matching is by whole tokens, so "ros" doesn't match inside "across".
  - **Degree fit (max 20):** majors named in sentences about degrees only, so "an aerospace company" doesn't count.
    - Points: 20 if they include yours; 15 if the sentence accepts engineering in general; 10 if no major is named; 0 if only other majors are named.
- **Preference (0–100).** Employer 40, location 30, term 30.
- **Overall:** relevance × (0.6 + 0.4 × preference / 100). Preference scales relevance instead of adding to it, so a preferred employer lifts a relevant role but can't make an irrelevant one rank high. Lists sort by eligibility group, then by overall score.
- **Quality check:** 287 real postings from the first discovery run, hand-labeled by title before tuning.

  | Metric | Before | After |
  |---|---|---|
  | Engineering above non-engineering | 51.8% | 99.3% |
  | Relevant above other engineering | 53.3% | 95.1% |
  | Non-engineering in top 50 | 16 | 0 |

  The 259 unique labeled titles are a regression test (`tests/data/role_titles.json`).
- **Performance:** about 1.6 ms per posting cold (2,000 postings ≈ 3 s). Results are memoized on their exact inputs, so repeat views take about 0.1 s.

## 8. Resume generation (Phase 3, Done)

1. **Candidates:** approved statements only. Protected statements and entries are always included. Education and awards appear even without bullet statements. Other entries need at least one approved statement.
2. **Scoring** (`resume/plan.py`): 10 points per skill/discipline tag shared with the posting (the same vocabulary as ranking, plus the statement's own tags), and up to 5 for other shared significant words.
   - Education comes first and Skills last. The sections in between are ordered by total relevance.
   - Education, employment and research entries stay reverse-chronological. Projects, leadership and awards are ordered by relevance.
   - Each entry starts with its top 4 statements. The user can tick or untick any approved statement.
3. **Rendering** (`resume/latex.py`): every user string is escaped. Common Unicode (dashes, quotes, ±, °, µ, →) is mapped to LaTeX. Anything else is rejected with a clear message rather than risking a broken PDF. The body uses only the template's own macros (`\resumeheader`, `\entry`, `\entryline`, `\skillline`).
4. **Compile and fit** (`resume/build.py`, `resume/service.py`): `pdflatex` runs in a private temp folder, at most 8 attempts. If the PDF is over the page limit, the overflow lines are measured with `pypdf`, and about that many lines' worth of the lowest-scoring optional statements are dropped. If only protected or user-forced content remains, generation stops and explains why. Fonts and margins are never changed.
5. **Validate:**
   - Must pass: compiles, page count, extractable text, and reading order (name, then section headings, in order).
   - Warnings only: any statement that can't be read back, and `Overfull \hbox` > 2pt.
6. **Store:** copy into a new `documents/<timestamp-uuid>/` folder, then insert the immutable row (content snapshot, template name+hash, file hashes, statement links). If the insert fails, the folder is removed. Failed generations store nothing and leave earlier resumes untouched.

## 9. Backups and recovery (Phase 5, Done)

`aims/backup.py`. One zip per backup, `backups/aims-YYYYMMDD-HHMMSS-<kind>.zip` (kind: auto, manual, pre-restore, imported):

```
manifest.json          {format: 1, kind, created_at, app_version, schema_version, data_revision, changed_at,
                        template_version, files: [{path, sha256, size}]}
aims.db                consistent snapshot via the SQLite backup API (never a copy of the live file)
documents/<id>/...     every generated resume (.tex, .pdf, .log)
reference/...          copies of template.tex and skills.json, for reference (not restored)
```

- **Create:** snapshot, then zip to `*.partial`, then verify (manifest matches contents, every SHA-256, `PRAGMA integrity_check`, schema version), then rename.
  - Any failure, including Ctrl-C or shutdown, removes the partial file and leaves the live database and existing backups untouched.
  - Stale `.partial` files from a killed run are cleared before the next backup.
  - Logs, locks, temp files and tokens are never included.
- **Change tracking** (migration 0005): triggers on all 13 user-data tables bump `meta.data_revision` and `meta.changed_at`. A test fails if a new table lacks them.
- **Schedule:** a background thread in the server checks 15 s after start, then hourly. A backup is due when the newest one is over 24 h old *and* data changed since. This catches up after days the app was closed, with no Windows Task Scheduler (R7).
- **Retention:** runs only right after a successful, verified backup. It deletes backups older than 30 days but always keeps the newest readable one, and never deletes unreadable files.
- **Mirror:** an optional copy to `backup_mirror_dir` (external drive, OneDrive), written as `.partial`, checksum-compared, then renamed. A mirror failure is reported but doesn't fail the backup. The mirror is pruned in step with local retention.
- **Restore** (a typed confirmation is required):
  1. Verify the archive. Backups from a newer schema are refused.
  2. Refuse if live data changed since the backup, unless the user explicitly allows it. "Changed" means `changed_at` is later *or* `data_revision` differs; the counter catches edits in the same second as the backup.
  3. Take a verified `pre-restore` safety backup. If that fails, nothing else happens.
  4. Copy the snapshot into the live database with the SQLite backup API, under its lock, with no file swapping.
  5. Run the integrity check and migrate an older backup's schema forward.
  6. Add missing resume files; existing ones are never overwritten (they're immutable).
- **Import:** "Restore from a file" uploads a backup from another drive or computer. It's streamed to disk, verified in full, kept as `-imported.zip`, then restored through the same path.
- **Visibility:** every attempt is appended to `backups/backup-log.jsonl`, which sits outside the database so a restore can't erase it. A failed attempt shows in Settings → Backups and as a banner on whatever page is open.
- **Spec 7 recovery tests** (`tests/test_backup.py`): normal backup, backup during an open write transaction, interrupted backup, disk-full and permission failures, missed schedules and catch-up, four kinds of corruption, full restore (including after restart and from an older schema), restore from an off-device copy on a fresh install, retention, and restoring over newer records. All pass.

## 10. External dependencies requiring validation

| Dependency | Status |
|---|---|
| Python 3.14 + FastAPI/uvicorn/pydantic | Validated |
| MiKTeX `pdflatex` | Validated (MiKTeX 25.12). `latexmk` not used (D10) |
| Greenhouse / Lever public APIs for the pilot employers | Validated 2026-10-26: 16 boards, all complete |
| Non-Greenhouse/Lever priority employer, gov/research source | Probed: Workday/Taleo (Blue Origin, Boeing, ...) have no official API, so they stay manual. USAJOBS needs a free API key, so it is deferred |
| GitHub Actions schedule + repo visibility | Workflow written. **Waiting on the user**: push to a GitHub repo (public recommended, R3) |
| `pypdf` | Validated, pinned 6.21.2 |

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

**Phase 2: Opportunity management (Done)**
1. Search/filter/sort params
2. Save/dismiss/notes (PATCH)
3. Compare view (2–4 side by side)
4. Bulk JSON/CSV import through the same normalization path
5. Profile facts plus skills vocabulary
6. Ranking engine plus "why" panel
7. Applications plus event history
8. Applications tab

**Phase 3: Resume generation (Done)**
1. Experience/statement schema and CRUD with verify/approve
2. Template plus LaTeX escaping
3. Selection algorithm
4. Compiler with fallback
5. Page-fit loop
6. Validation
7. Document storage plus preview/export
8. Resume Studio UI

**Phase 4: Automated discovery (Done, except enabling the cloud schedule)**
1. Probe script plus pilot list
2. Canonical model
3. Greenhouse adapter
4. Lever adapter
5. Feed writer plus Actions workflow
6. Importer
7. Source health UI

**Phase 5: Reliability and recovery (Done)**
1. Revision triggers
2. Backup create/verify
3. Schedule plus retention
4. Mirror copy
5. Restore with guards
6. Recovery test suite
7. Backup UI

**Phase 6: End-to-end validation (Done except the 2-week observation and real-use timing; see docs/VALIDATION.md)**
1. Ten-opportunity walkthrough
2. Discovery metrics against verified snapshots
3. Two-week observation log
4. Fix list
