"""SQLite access: connections, transactions, snapshots, and the migration runner.

Migrations are append-only files named NNNN_description.sql in aims/migrations.
Each one runs in its own transaction together with the PRAGMA user_version bump,
so a failed migration leaves the database exactly as it was. Before migrating an
existing database, a verified snapshot is written to <backups>/pre-migration/.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

Migration = tuple[int, str, str]  # (version, filename, sql)


class MigrationError(RuntimeError):
    pass


class SnapshotError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: no implicit transactions; we issue BEGIN/COMMIT ourselves.
    # check_same_thread=False: FastAPI may open and use a request's connection on
    # different threadpool threads, but never concurrently.
    conn = sqlite3.connect(path, isolation_level=None, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def snapshot(conn: sqlite3.Connection, dest: Path) -> None:
    """Write a consistent copy of a live database using the SQLite backup API.

    The copy is written to a temporary name, integrity-checked, then renamed, so
    `dest` either does not exist or is a verified snapshot.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    tmp.unlink(missing_ok=True)
    target = sqlite3.connect(tmp)
    try:
        conn.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        target.close()
    if result != "ok":
        tmp.unlink(missing_ok=True)
        raise SnapshotError(f"Snapshot failed integrity check: {result}")
    tmp.replace(dest)


def available_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise MigrationError(f"Badly named migration file: {path.name}")
        found.append((int(match.group(1)), path.name, path.read_text(encoding="utf-8")))
    versions = [v for v, _, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"Migrations must be numbered 0001, 0002, ... without gaps; found {versions}")
    return found


def migrate(conn: sqlite3.Connection, backup_dir: Path, migrations: list[Migration] | None = None) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    if migrations is None:
        migrations = available_migrations()
    current = schema_version(conn)
    latest = migrations[-1][0] if migrations else 0
    if current > latest:
        raise MigrationError(
            f"Database schema version {current} is newer than this app supports ({latest}). "
            "Refusing to run an older app against newer data."
        )
    pending = [m for m in migrations if m[0] > current]
    if not pending:
        return current

    if current > 0:
        stamp = utc_now().replace("-", "").replace(":", "")
        snapshot(conn, backup_dir / "pre-migration" / f"aims-v{current}-{stamp}.db")

    for version, name, sql in pending:
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\n;PRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"Migration {name} failed and was rolled back: {exc}") from exc
    return pending[-1][0]
