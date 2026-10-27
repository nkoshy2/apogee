# Decision log

Short records of significant decisions and deviations from the spec.

## D1: Stdlib sqlite3 + SQL migrations instead of SQLAlchemy/Alembic (2026-10-26)
Single user, single SQLite file, a handful of tables. Hand-written SQL plus `PRAGMA user_version` gives
transactional, append-only migrations with no extra dependencies. Before any pending migration
runs against an existing DB, the runner takes a backup with the SQLite backup API. Revisit if the schema
gets complicated enough that hand-written SQL becomes error-prone.

## D2: Private data lives in %LOCALAPPDATA%\AIMS, not in the repo (2026-10-26)
The repo will be used by GitHub Actions for discovery. Keeping data outside it means private data
cannot be committed by accident. Override with `AIMS_DATA_DIR`.

## D3: Localhost hardening (2026-10-26)
The server binds to 127.0.0.1 only. Requests must use an allowlisted Host header (DNS-rebinding defense).
Non-GET requests need the `X-AIMS-Request: 1` header and a same-origin `Origin` if one is present. Browsers
cannot send a custom header cross-origin without a CORS preflight, and the server never grants one.
CSP is `default-src 'self'`. The frontend never uses `innerHTML` with data.

## D4: Single instance via an OS file lock (2026-10-26)
The server holds an exclusive `msvcrt` lock on `<data>/server.lock` for its lifetime. Windows releases
the lock if the process dies, so a stale lock file cannot block startup. The launcher first probes
`/api/health` for the AIMS app id and reuses a running server if it finds one.

## D5: Ranking is computed on read, memoized in memory (2026-10-26)
Rankings depend on settings and on the posting text, both of which change. Storing them would
mean invalidation logic and a new table. Instead they are recomputed on each request and memoized
on their exact inputs (settings JSON plus the posting fields read), so a cached result can never be stale.
Measured: about 1 ms per posting cold and 0.1 s for 2,000 postings warm. Revisit if lists grow well past that.

## D6: One application per opportunity; any status transition allowed (2026-10-26)
`applications.opportunity_id` is UNIQUE, which prevents accidental duplicate tracking. Real hiring
processes skip and repeat steps, so the lifecycle is not enforced as a state machine. Instead,
every status change is recorded in an append-only history (enforced by triggers). Starting an
application marks the opportunity as saved. Nothing ever infers that an application was submitted. The
UI only pre-fills today's date when the user picks "submitted", and the user can change it.

## D7: Discovered postings are read-only except user fields (2026-10-26)
PATCH accepts title, URL, and other details only for `source = 'manual'`. Discovered postings are kept
in sync with their source (Phase 4), and local edits would be silently fought by imports.
`user_state` and `user_notes` are always editable.

## D8: Name: Apogee; internal identifiers unchanged (2026-10-26)
The app is called Apogee everywhere the user sees it: UI, launcher, desktop shortcut, and docs.
The package (`aims`), environment variables (`AIMS_*`), data folder (`%LOCALAPPDATA%\AIMS`), and
request header (`X-AIMS-Request`) keep the original code name. Renaming those would orphan existing data
for no benefit. `scripts/setup.ps1` removes the old "Internship Manager" shortcut only if it points at this launcher.

## D9: UI follows the Polaris redesign principles (2026-10-26)
Taken from `polaris/UI Redesign Planning.zip` (Organic design system plus the "1b, refined" notes): warm cream
ground, soft over-rounded cards, pill controls, a header shell with pill navigation and counts, calm
bold-body headings, sentence summaries with inline link-buttons, and sentence-style filters. Color carries
meaning: terracotta for actions, sage only for success/complete, ochre for "needs attention". Fonts are not
fetched from Google (local-first; the CSP allows only 'self'). Figtree is used if installed, otherwise Segoe UI.

## D10: pdflatex directly, not latexmk (2026-10-26)
MiKTeX's latexmk needs Perl, which isn't on PATH when Apogee starts from its shortcut. The single template
has no cross-references, so one pdflatex pass is enough. Runs use `-no-shell-escape` and MiKTeX's
`-disable-installer`, so a compile never executes shell commands or downloads packages silently.

## D11: Settings PUT replaces only the sections sent (2026-10-26)
Resume Studio's header form and the Settings page each save different sections. Full-replace semantics
would let one form silently wipe the other's data, so PUT now merges at the top level.

## D12: Resume content rules (2026-10-26)
Only approved statements are used. A statement must be verified before it can be approved, and editing its
text sends it back to draft. Generated resumes are immutable (trigger-enforced). Each one stores a full
content snapshot, the template name plus hash, and file hashes. Statements a resume used can't be deleted.
Fitting the page limit drops the lowest-scoring optional statements, never protected or user-forced ones,
and never touches fonts or margins. Education/employment/research stay reverse-chronological; projects,
leadership and awards are ordered by relevance.

## D13: Discovery keeps student roles only, filtered at the source (2026-10-27)
Boards list about 8,300 jobs, of which about 290 are internships or co-ops. Adapters keep only titles (or Lever
labels) matching intern / internship / co-op / student. This keeps the feed small (about 1.9 MB per run)
and the opportunity list relevant. Apprenticeships are excluded (they are technician roles).
Completeness is defined against the filtered set, so a filter change can mark postings "not seen"
but never deletes them.

## D14: Feed published as a single force-pushed commit (2026-10-27)
Committing every run would add about 100 MB a month to git history. Instead the workflow rebuilds a
`discovery-feed` branch holding the last 14 days of runs (always at least 4) and force-pushes it as one
commit. Local import verifies each run's SHA-256 from the index before using it.

## D15: Sources are a file in the repo, not a UI setting (2026-10-27)
The cloud job can only read what's in the repository, so `aims/discovery/sources.json` is the single
source of truth for both cloud and local runs. Settings shows health per source. Editing the list is a
one-line change to that file. Revisit if adding sources becomes frequent.

## D16: Ranking retuned against real postings (2026-10-27)
On the first 287 real postings the original relevance model ordered roles no better than chance:
engineering ranked above non-engineering only 51.8% of the time, and 10 of the top 20 were roles like
Supply Chain or Security Analyst. There were three causes:
- Skill score was a ratio, so a posting naming one skill you have scored full marks while one naming many
  topics you didn't list scored zero.
- Field topics (propulsion, thermal) were treated as tools.
- Preference was added on top, so a preferred employer lifted any role.

The fix:
- Classify the role from its title (a vocabulary list with explicit precedence rules) and cap
  non-engineering roles at 10 relevance.
- Count your skills with diminishing returns and no penalty for unlisted ones.
- Count majors only in degree sentences.
- Make preference scale relevance instead of adding to it.

Measured on the same hand-labeled postings (labels written before any rule change):
- Engineering above non-engineering: 99.3%.
- Relevant above other engineering: 95.1%.
- Non-engineering roles in the top 50: none.

The labeled titles are kept as a regression test. A graduation rule that misread "Graduate Engineer
Internship" as a date requirement was fixed at the same time.

## D17: Change tracking by triggers, compared by counter and time (2026-10-27)
Restore must never silently overwrite newer work. That needs a reliable notion of "changed since the
backup", and file times aren't reliable. Triggers on every user-data table bump a counter and a timestamp
in `meta`. Both are compared, because timestamps have one-second resolution (a test caught an edit made in
the same second as the backup). The comparison errs toward asking: a backup from another install may
prompt for confirmation even when nothing is lost.

## D18: Restore copies in with the SQLite backup API (2026-10-27)
Swapping database files under a running server risks WAL mismatches and half-replaced state. The backup
API copies page by page into the live database under its own lock, so readers see either the old data or
the new. A verified safety backup is always taken first, so every restore can be undone.

## D19: UI files are revalidated on every load (2026-10-27)
Static files had no cache header, so a browser could keep showing an old UI after an update. They now send
`Cache-Control: no-cache`. That costs one cheap 304 check per file against a local server.
