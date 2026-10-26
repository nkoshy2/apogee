import sqlite3

import pytest

from aims import db


def fresh(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_initial_migration_applies_and_is_idempotent(tmp_path):
    conn = fresh(tmp_path)
    latest = db.available_migrations()[-1][0]
    assert db.migrate(conn, tmp_path / "b") == latest
    assert db.migrate(conn, tmp_path / "b") == latest
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "opportunities"} <= tables
    # Fresh DB: nothing to protect, so no pre-migration snapshot.
    assert not (tmp_path / "b" / "pre-migration").exists()


def test_failed_migration_rolls_back_and_keeps_data(tmp_path):
    conn = fresh(tmp_path)
    good = [(1, "0001_a.sql", "CREATE TABLE t (x INTEGER);")]
    db.migrate(conn, tmp_path / "b", good)
    conn.execute("INSERT INTO t VALUES (42)")

    bad = good + [(2, "0002_b.sql", "CREATE TABLE u (y INTEGER); INSERT INTO nope VALUES (1);")]
    with pytest.raises(db.MigrationError, match="rolled back"):
        db.migrate(conn, tmp_path / "b", bad)

    assert db.schema_version(conn) == 1
    assert conn.execute("SELECT x FROM t").fetchall()[0][0] == 42
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='u'").fetchone() is None


def test_pre_migration_snapshot_taken_for_existing_db(tmp_path):
    conn = fresh(tmp_path)
    m1 = [(1, "0001_a.sql", "CREATE TABLE t (x INTEGER);")]
    db.migrate(conn, tmp_path / "b", m1)
    conn.execute("INSERT INTO t VALUES (7)")
    db.migrate(conn, tmp_path / "b", m1 + [(2, "0002_b.sql", "ALTER TABLE t ADD COLUMN y TEXT;")])

    snaps = list((tmp_path / "b" / "pre-migration").glob("*.db"))
    assert len(snaps) == 1 and "v1" in snaps[0].name
    snap = sqlite3.connect(snaps[0])
    assert snap.execute("SELECT x FROM t").fetchall() == [(7,)]
    assert snap.execute("PRAGMA user_version").fetchone()[0] == 1
    snap.close()


def test_refuses_db_newer_than_app(tmp_path):
    conn = fresh(tmp_path)
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(db.MigrationError, match="newer"):
        db.migrate(conn, tmp_path / "b")


def test_migration_files_are_well_formed():
    versions = [v for v, _, _ in db.available_migrations()]
    assert versions == list(range(1, len(versions) + 1))


def test_transaction_rolls_back_on_error(tmp_path):
    conn = fresh(tmp_path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
