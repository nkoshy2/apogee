# Apogee

Apogee is a personal, local-first tool for finding engineering internships, tailoring resumes, and tracking
applications. It runs entirely on this computer, costs nothing to operate, and never submits applications on its own.

- Design and plan: [docs/DESIGN.md](docs/DESIGN.md)
- Decision log: [docs/DECISIONS.md](docs/DECISIONS.md)

## Status

| Phase | State |
|---|---|
| 1. Foundation (local server, SQLite + migrations, launcher, UI shell) | **Done**: tests pass |
| 2. Opportunity management, ranking, application tracking | **Done**: tests pass |
| 3. Resume generation (experience database, LaTeX, PDF preview/export) | **Done**: tests pass |
| 4. Automated discovery (Greenhouse, Lever) | Not started |
| 5. Backups and recovery | Not started. Only pre-migration snapshots exist so far |
| 6. End-to-end validation | Not started |

## Setup (once)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

This creates `.venv`, installs the pinned dependencies, and adds an **Apogee** shortcut
to the desktop. Double-click the shortcut to start the app (or reuse it if it is already running) and open the browser.

## Where data lives

`%LOCALAPPDATA%\AIMS\` holds the database, logs, and backups. It is never stored in this repository.
Generated resumes are kept under `documents\`, one folder per resume, never overwritten.
Resume generation needs a LaTeX install (MiKTeX, free). Apogee calls `pdflatex` directly.
Overrides: `AIMS_DATA_DIR`, `AIMS_PORT` (default 8765).

## Development

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python -m aims.server      # run in the foreground
```

Schema changes always go in a new `aims/migrations/NNNN_name.sql`. Never edit a released migration.
