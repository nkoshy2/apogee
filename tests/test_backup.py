"""Backup and recovery tests, including every scenario required by spec section 7."""

import datetime as dt
import json
import os
import shutil
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest

from aims import backup, db, settings_store
from tests.conftest import make_client

T0 = dt.datetime(2026, 11, 1, 9, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def conn(config):
    config.ensure_dirs()
    c = db.connect(config.db_path)
    db.migrate(c, config.backup_dir)
    yield c
    c.close()


def add_opportunity(conn, n=1, title="Propulsion Intern"):
    from aims import opportunities
    return opportunities.create_manual(conn, opportunities.OpportunityIn(
        employer="Acme", title=title, url=f"https://acme.example/jobs/{n}", user_notes=f"note {n}"))


def titles(conn):
    return sorted(r[0] for r in conn.execute("SELECT title FROM opportunities"))


def add_document(config, folder="20261101-090000-abcd1234", body=b"%PDF-1.4 resume"):
    d = config.documents_dir / folder
    d.mkdir(parents=True)
    (d / "resume.pdf").write_bytes(body)
    (d / "resume.tex").write_text("\\documentclass{article}", encoding="utf-8")
    return d


def backups(config):
    return sorted(p.name for p in config.backup_dir.glob("aims-*.zip"))


def leftovers(config):
    return list(config.backup_dir.glob("*.partial")) + [p for p in config.tmp_dir.iterdir()]


# ---------------------------------------------------------------- change tracking

def test_every_user_table_is_change_tracked(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN ('meta', 'sqlite_sequence')")]
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    missing = [f"rev_{t}_{e}" for t in tables for e in ("insert", "update", "delete") if f"rev_{t}_{e}" not in triggers]
    assert missing == [], "new tables need change-tracking triggers (see migration 0005)"


def test_changes_bump_revision(conn):
    before = backup.live_state(conn)["data_revision"]
    add_opportunity(conn)
    conn.execute("UPDATE opportunities SET user_notes = 'x'")
    assert backup.live_state(conn)["data_revision"] == before + 2


# ---------------------------------------------------------------- 1. normal backup

def test_backup_contains_database_documents_manifest(conn, config):
    add_opportunity(conn)
    add_document(config)
    r = backup.create(conn, config, "manual", now=T0)
    assert r["name"] == "aims-20261101-090000-manual.zip" and backups(config) == [r["name"]]
    path = config.backup_dir / r["name"]
    m = backup.verify(path)
    assert m["kind"] == "manual" and m["schema_version"] == db.schema_version(conn)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
    assert {"manifest.json", "aims.db", "documents/20261101-090000-abcd1234/resume.pdf",
            "reference/template.tex", "reference/skills.json"} <= names
    assert not any("log" in n and n.endswith(".jsonl") for n in names)  # no logs, locks or temp files
    assert leftovers(config) == []


# ---------------------------------------------------------------- 2. backup while the database is active

def test_backup_is_consistent_while_another_connection_writes(conn, config):
    for i in range(20):
        add_opportunity(conn, i)
    writer = db.connect(config.db_path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE opportunities SET title = 'half-written'")  # uncommitted while we back up
    try:
        r = backup.create(conn, config, "manual", now=T0)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    with zipfile.ZipFile(config.backup_dir / r["name"]) as zf:
        zf.extract("aims.db", config.tmp_dir)
    snap = sqlite3.connect(config.tmp_dir / "aims.db")
    assert snap.execute("SELECT COUNT(*) FROM opportunities WHERE title = 'half-written'").fetchone()[0] == 0
    assert snap.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 20
    snap.close()


def test_concurrent_backups_are_serialized(conn, config):
    errors = []

    def run(kind, t):
        c = db.connect(config.db_path)
        try:
            backup.create(c, config, kind, now=t)
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()
    threads = [threading.Thread(target=run, args=("manual", T0 + dt.timedelta(seconds=i))) for i in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and len(backups(config)) == 3


# ---------------------------------------------------------------- 3. interrupted backup

def test_interrupted_backup_leaves_no_partial_file_and_keeps_old_backups(conn, config, monkeypatch):
    add_opportunity(conn)
    good = backup.create(conn, config, "manual", now=T0)["name"]

    real_write = zipfile.ZipFile.write

    def crash(self, filename, arcname=None, *a, **kw):
        if arcname == "reference/template.tex":
            raise KeyboardInterrupt("power cut")  # not even an ordinary exception
        return real_write(self, filename, arcname, *a, **kw)
    monkeypatch.setattr(zipfile.ZipFile, "write", crash)
    with pytest.raises(BaseException):
        backup.create(conn, config, "manual", now=T0 + dt.timedelta(hours=1))
    assert backups(config) == [good]
    assert not list(config.backup_dir.glob("*.partial"))
    assert titles(conn) == ["Propulsion Intern"]  # live data untouched
    backup.verify(config.backup_dir / good)


def test_stale_partial_from_a_killed_run_is_cleaned_up(conn, config):
    stale = config.backup_dir / "aims-20261031-000000-auto.zip.partial"
    stale.write_bytes(b"half a zip")
    backup.create(conn, config, "manual", now=T0)
    assert not stale.exists() and len(backups(config)) == 1


# ---------------------------------------------------------------- 4. disk and permission failures

@pytest.mark.parametrize("error", [PermissionError(13, "Access is denied"), OSError(28, "No space left on device")])
def test_disk_and_permission_failures_are_reported_and_harmless(conn, config, monkeypatch, error):
    add_opportunity(conn)
    good = backup.create(conn, config, "manual", now=T0)["name"]

    def fail(*a, **kw):
        raise error
    monkeypatch.setattr(backup.os, "replace", fail)
    with pytest.raises(backup.BackupError, match="nothing was changed"):
        backup.create(conn, config, "manual", now=T0 + dt.timedelta(hours=1))
    monkeypatch.undo()
    assert backups(config) == [good] and leftovers(config) == []
    assert titles(conn) == ["Propulsion Intern"]
    last = backup.read_log(config)[0]
    assert last["status"] == "failed" and str(error.strerror) in last["error"]


def test_failed_backup_never_triggers_retention(conn, config, monkeypatch):
    add_opportunity(conn)
    old = backup.create(conn, config, "manual", now=T0 - dt.timedelta(days=60))["name"]
    monkeypatch.setattr(backup, "verify", lambda p: (_ for _ in ()).throw(backup.BackupError("bad")))
    with pytest.raises(backup.BackupError):
        backup.create(conn, config, "manual", now=T0)
    assert backups(config) == [old]  # the only good backup survives, however old


# ---------------------------------------------------------------- 5. missed scheduled backups

def test_due_after_a_day_with_changes_and_catches_up(conn, config):
    assert backup.is_due(conn, config, T0)  # never backed up
    assert backup.run_if_due(config, now=T0)["kind"] == "auto"
    conn.execute("UPDATE meta SET changed_at = ?", ("2026-11-01T10:00:00Z",))
    assert not backup.is_due(conn, config, T0 + dt.timedelta(hours=23))
    assert backup.run_if_due(config, now=T0 + dt.timedelta(hours=23)) is None
    # The computer was off for three days: the first check after start catches up once.
    later = T0 + dt.timedelta(days=3)
    assert backup.is_due(conn, config, later)
    assert backup.run_if_due(config, now=later) is not None
    assert backup.run_if_due(config, now=later + dt.timedelta(minutes=5)) is None


def test_not_due_when_nothing_changed(conn, config):
    backup.create(conn, config, "manual", now=T0)
    conn.execute("UPDATE meta SET changed_at = ?", ("2026-10-31T00:00:00Z",))
    assert not backup.is_due(conn, config, T0 + dt.timedelta(days=5))


def test_scheduler_failure_is_logged_not_raised(config, conn, monkeypatch):
    monkeypatch.setattr(backup.db, "snapshot", lambda *a: (_ for _ in ()).throw(OSError("disk gone")))
    assert backup.run_if_due(config, now=T0) is None
    assert backup.read_log(config)[0]["status"] == "failed"


# ---------------------------------------------------------------- 6. corrupted backup detection

def corrupt_member(path: Path, member: str, data: bytes):
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, data if item.filename == member else src.read(item.filename))
    os.replace(tmp, path)


@pytest.mark.parametrize("damage", ["truncate", "member", "db", "extra"])
def test_corruption_is_detected_and_restore_refused(conn, config, damage):
    add_opportunity(conn)
    add_document(config)
    name = backup.create(conn, config, "manual", now=T0)["name"]
    path = config.backup_dir / name
    if damage == "truncate":
        path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])
    elif damage == "member":
        corrupt_member(path, "documents/20261101-090000-abcd1234/resume.pdf", b"tampered")
    elif damage == "db":
        corrupt_member(path, "aims.db", b"SQLite format 3\x00" + b"\x00" * 100)
    else:
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("../../evil.py", "print('pwned')")
    with pytest.raises(backup.BackupError):
        backup.verify(path)
    add_opportunity(conn, 2, "After")
    with pytest.raises(backup.BackupError):
        backup.restore(conn, config, name, allow_newer=True, now=T0 + dt.timedelta(hours=1))
    assert titles(conn) == ["After", "Propulsion Intern"]  # live data untouched
    assert not (config.data_dir.parent / "evil.py").exists()


# ---------------------------------------------------------------- 7. full restoration

def test_full_restore_brings_back_database_and_documents(conn, config):
    add_opportunity(conn, 1, "Kept")
    folder = add_document(config)
    name = backup.create(conn, config, "manual", now=T0)["name"]

    conn.execute("UPDATE opportunities SET user_notes = 'edited after backup'")
    add_opportunity(conn, 2, "Added after backup")
    shutil.rmtree(folder)  # a lost document
    r = backup.restore(conn, config, name, allow_newer=True, now=T0 + dt.timedelta(hours=2))

    assert titles(conn) == ["Kept"]
    assert conn.execute("SELECT user_notes FROM opportunities").fetchone()[0] == "note 1"
    assert (folder / "resume.pdf").read_bytes() == b"%PDF-1.4 resume" and r["documents_restored"] == 2
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # The data that was replaced is preserved in the safety backup.
    safety = r["safety_backup"]
    assert safety.endswith("-pre-restore.zip") and backup.verify(config.backup_dir / safety)
    assert backup.read_log(config)[0] == {**backup.read_log(config)[0], "event": "restore", "status": "ok"}


def test_restore_survives_app_restart(config):
    with make_client(config) as c:
        c.post("/api/opportunities", json={"employer": "E", "title": "Before", "url": "https://e.example/1"})
        name = c.post("/api/backups").json()["name"]
        c.post("/api/opportunities", json={"employer": "E", "title": "After", "url": "https://e.example/2"})
        r = c.post(f"/api/backups/{name}/restore", json={"confirm": name, "allow_newer": True})
        assert r.status_code == 200, r.text
    with make_client(config) as c:
        assert [o["title"] for o in c.get("/api/opportunities").json()] == ["Before"]


def test_restore_of_older_schema_is_migrated(conn, config, tmp_path):
    old = tmp_path / "old"
    old_cfg = type(config)(data_dir=old, port=config.port)
    old_cfg.ensure_dirs()
    oc = db.connect(old_cfg.db_path)
    db.migrate(oc, old_cfg.backup_dir, db.available_migrations()[:5])  # current schema
    oc.execute("UPDATE meta SET changed_at = '2026-01-01T00:00:00Z'")
    name = backup.create(oc, old_cfg, "manual", now=T0)["name"]
    oc.close()
    shutil.copy(old_cfg.backup_dir / name, config.backup_dir / name)
    r = backup.restore(conn, config, name, allow_newer=True, now=T0 + dt.timedelta(hours=1))
    assert r["schema_version"] == db.available_migrations()[-1][0]


def test_backup_from_newer_app_is_refused(conn, config):
    name = backup.create(conn, config, "manual", now=T0)["name"]
    path = config.backup_dir / name
    with zipfile.ZipFile(path) as zf:
        m = json.loads(zf.read("manifest.json"))
    m["schema_version"] = 999
    corrupt_member(path, "manifest.json", json.dumps(m).encode())
    with pytest.raises(backup.BackupError):
        backup.restore(conn, config, name, allow_newer=True)


# ---------------------------------------------------------------- 8. restore from an off-device copy

def test_mirror_copy_then_restore_on_a_fresh_install(conn, config, tmp_path):
    add_opportunity(conn, 1, "From my old laptop")
    add_document(config)
    mirror = tmp_path / "external-drive"
    r = backup.create(conn, config, "auto", now=T0, settings_mirror=str(mirror))
    assert r["mirror"]["status"] == "ok" and (mirror / r["name"]).exists()

    fresh = type(config)(data_dir=tmp_path / "new-computer", port=config.port)
    with make_client(fresh) as c:
        up = c.post("/api/backups/import", content=(mirror / r["name"]).read_bytes())
        assert up.status_code == 201, up.text
        name = up.json()["name"]
        assert name.endswith("-imported.zip")
        res = c.post(f"/api/backups/{name}/restore", json={"confirm": name, "allow_newer": True})
        assert res.status_code == 200, res.text
        assert [o["title"] for o in c.get("/api/opportunities").json()] == ["From my old laptop"]
    assert (fresh.documents_dir / "20261101-090000-abcd1234" / "resume.pdf").exists()


def test_mirror_failure_does_not_fail_the_backup(conn, config, tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("I'm a file")
    r = backup.create(conn, config, "manual", now=T0, settings_mirror=str(blocker))
    assert r["mirror"]["status"] == "failed" and backups(config) == [r["name"]]


def test_import_rejects_garbage(client):
    r = client.post("/api/backups/import", content=b"definitely not a zip")
    assert r.status_code == 422


# ---------------------------------------------------------------- 9. retention

def test_retention_keeps_30_days_and_never_the_last_good_backup(conn, config):
    names = []
    for days_ago in (45, 35, 20, 5):
        conn.execute("UPDATE meta SET changed_at = '2000-01-01T00:00:00Z'")
        names.append(backup.create(conn, config, "auto", now=T0 - dt.timedelta(days=days_ago))["name"])
    # Retention runs after each successful backup, relative to that backup's time; check at T0.
    deleted = backup.apply_retention(config, T0)
    remaining = backups(config)
    assert names[0] not in remaining and names[1] not in remaining
    assert names[2] in remaining and names[3] in remaining
    # Even a very old backup survives if it's the only one.
    for n in remaining[:-1]:
        (config.backup_dir / n).unlink()
    assert backup.apply_retention(config, T0 + dt.timedelta(days=400)) == []
    assert backups(config) == [names[3]]


def test_retention_ignores_unreadable_files(conn, config):
    bad = config.backup_dir / "aims-20200101-000000-manual.zip"
    bad.write_bytes(b"junk")
    backup.create(conn, config, "manual", now=T0)
    assert bad.exists()  # left for the user to inspect, never silently removed


# ---------------------------------------------------------------- 10. restoring over newer records

def test_restore_over_newer_data_requires_explicit_permission(conn, config):
    add_opportunity(conn, 1, "Old")
    name = backup.create(conn, config, "manual", now=T0)["name"]
    add_opportunity(conn, 2, "Newer work")
    conn.execute("UPDATE meta SET changed_at = '2026-11-01T12:00:00Z'")
    with pytest.raises(backup.NewerDataError) as exc:
        backup.restore(conn, config, name, now=T0 + dt.timedelta(hours=5))
    assert exc.value.details["changes_since"] >= 1
    assert titles(conn) == ["Newer work", "Old"] and backups(config) == [name]  # nothing happened, no safety backup either

    r = backup.restore(conn, config, name, allow_newer=True, now=T0 + dt.timedelta(hours=5))
    assert titles(conn) == ["Old"]
    # The newer work is not lost: it's in the safety backup.
    safety = config.backup_dir / r["safety_backup"]
    with zipfile.ZipFile(safety) as zf:
        zf.extract("aims.db", config.tmp_dir)
    s = sqlite3.connect(config.tmp_dir / "aims.db")
    assert sorted(x[0] for x in s.execute("SELECT title FROM opportunities")) == ["Newer work", "Old"]
    s.close()


def test_restore_without_newer_data_needs_no_override(conn, config):
    add_opportunity(conn)
    conn.execute("UPDATE meta SET changed_at = '2026-11-01T08:00:00Z'")
    name = backup.create(conn, config, "manual", now=T0)["name"]
    assert backup.restore(conn, config, name, now=T0 + dt.timedelta(hours=1))["restored"] == name


# ---------------------------------------------------------------- API guards

def test_api_restore_requires_confirmation_and_valid_name(client):
    name = client.post("/api/backups").json()["name"]
    assert client.post(f"/api/backups/{name}/restore", json={"confirm": "yes"}).status_code == 400
    # Traversal attempts are refused (by the router or by the name check) and restore nothing.
    assert client.post("/api/backups/..%2F..%2Fsecret.zip/restore", json={"confirm": "x"}).status_code in (400, 404, 405, 422)
    assert client.post("/api/backups/evil.zip/restore", json={"confirm": "evil.zip"}).status_code == 422
    assert client.post("/api/backups/aims-20990101-000000-manual.zip/verify").status_code == 422
    client.post("/api/opportunities", json={"employer": "E", "title": "New", "url": "https://e.example/1"})
    r = client.post(f"/api/backups/{name}/restore", json={"confirm": name})
    assert r.status_code == 409 and r.json()["newer"]["changes_since"] >= 1
    status = client.get("/api/backups").json()
    assert status["changed_since_newest"] is True and status["newest"]["name"] == name


def test_change_in_the_same_second_as_the_backup_still_counts_as_newer(conn, config):
    """Regression: timestamps have 1 s resolution, so only the change counter can see this edit."""
    name = backup.create(conn, config, "manual", now=T0)["name"]
    add_opportunity(conn, 1, "Saved a moment later")
    conn.execute("UPDATE meta SET changed_at = ?", (backup._iso(T0),))  # same second as the backup
    with pytest.raises(backup.NewerDataError):
        backup.restore(conn, config, name, now=T0)
    assert titles(conn) == ["Saved a moment later"]
