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
