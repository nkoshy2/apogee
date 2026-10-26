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
