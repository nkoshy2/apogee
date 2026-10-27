"""Backups and restores. Rules (spec 3.8, docs/DESIGN.md section 9):

- A backup is one zip: a consistent database snapshot (SQLite backup API, never a raw copy of
  the live file), every generated resume, reference copies of the template and vocabulary, and
  a manifest with SHA-256 checksums. It is written as *.partial, fully verified (checksums,
  database integrity, schema), and only then renamed. A failure leaves nothing behind and
  never touches the live database.
- Retention keeps 30 days. Old backups are deleted only right after a newer one was created
  and verified, and the newest verified backup is always kept.
- A restore verifies the archive first, refuses to overwrite data changed after the backup
  unless the user explicitly allows it, always takes a safety backup of the current data
  first, and copies the snapshot in with the SQLite backup API (no file swapping).
- Every attempt is appended to backups/backup-log.jsonl, which lives outside the database so
  a restore can't erase the record of failures.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from aims import __version__, db
from aims.config import Config
from aims.resume import latex

RETENTION_DAYS = 30
DUE_AFTER_HOURS = 24
CHECK_EVERY_S = 3600
FORMAT = 1
NAME = re.compile(r"^aims-(\d{8})-(\d{6})-(auto|manual|pre-restore|imported)\.zip$")
_MEMBER = re.compile(r"^(manifest\.json|aims\.db|documents/[A-Za-z0-9._-]+/resume\.(tex|pdf|log)|reference/(template\.tex|skills\.json))$")
_LOCK = threading.Lock()  # one backup or restore at a time
VOCABULARY = Path(__file__).parent / "data" / "skills.json"


class BackupError(RuntimeError):
    pass


class NewerDataError(BackupError):
    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


# ---------------------------------------------------------------- helpers

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _log(config: Config, entry: dict) -> None:
    try:
        config.backup_dir.mkdir(parents=True, exist_ok=True)
        with open(config.backup_dir / "backup-log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": _iso(_now()), **entry}) + "\n")
    except OSError:
        pass  # the log is informational; failing to write it must not mask the real outcome


def read_log(config: Config, limit: int = 20) -> list[dict]:
    path = config.backup_dir / "backup-log.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[::-1]


def live_state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT data_revision, changed_at FROM meta WHERE id = 1").fetchone()
    return {"data_revision": row[0], "changed_at": row[1]}


def changed_since(state: dict, backup_meta: dict) -> bool:
    """Has live data changed since this backup was taken?

    Timestamps have one-second resolution, so a change in the same second as the backup would
    look older. The change counter recorded in the backup catches that case. This errs on the
    side of asking: a backup from another install with a different counter also counts as
    "changed", which only means the user is asked to confirm."""
    return (state["changed_at"] > backup_meta["created_at"]
            or state["data_revision"] != backup_meta.get("data_revision"))


# ---------------------------------------------------------------- verify

def verify(path: Path) -> dict:
    """Check a backup completely. Returns its manifest; raises BackupError on any problem."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            bad = [n for n in names if not _MEMBER.match(n)]
            if bad:
                raise BackupError(f"Unexpected file in backup: {bad[0]}")
            manifest = json.loads(zf.read("manifest.json"))
            if manifest.get("format") != FORMAT:
                raise BackupError("Unknown backup format")
            listed = {f["path"]: f for f in manifest["files"]}
            if set(listed) != set(names) - {"manifest.json"}:
                raise BackupError("Backup contents don't match its manifest")
            for name, meta in listed.items():
                h = hashlib.sha256()
                with zf.open(name) as member:
                    for chunk in iter(lambda: member.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != meta["sha256"]:
                    raise BackupError(f"Checksum mismatch for {name}: the backup is corrupted")
            with tempfile.TemporaryDirectory() as tmp:
                zf.extract("aims.db", tmp)
                conn = sqlite3.connect(Path(tmp) / "aims.db")
                try:
                    check = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                finally:
                    conn.close()
            if check != "ok":
                raise BackupError(f"Database in backup failed its integrity check: {check}")
            if version != manifest["schema_version"]:
                raise BackupError("Database schema doesn't match the manifest")
            return manifest
    except BackupError:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError, OSError, sqlite3.Error, TypeError) as exc:
        raise BackupError(f"Not a readable Apogee backup: {exc}") from None


# ---------------------------------------------------------------- create

def _documents(config: Config) -> list[Path]:
    root = config.documents_dir
    if not root.exists():
        return []
    return sorted(p for p in root.glob("*/resume.*") if p.is_file() and _MEMBER.match(f"documents/{p.parent.name}/{p.name}"))


def create(conn: sqlite3.Connection, config: Config, kind: str = "manual", now: dt.datetime | None = None,
           settings_mirror: str = "") -> dict:
    """Create and verify a backup, then mirror it and apply retention. Raises BackupError on failure."""
    with _LOCK:
        return _create(conn, config, kind, now or _now(), settings_mirror)


def _create(conn, config, kind, now, mirror_dir) -> dict:
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    name = f"aims-{now.strftime('%Y%m%d-%H%M%S')}-{kind}.zip"
    final = config.backup_dir / name
    if final.exists():
        raise BackupError(f"A backup named {name} already exists")
    partial = final.with_name(name + ".partial")
    # Leftovers from a run that was killed outright (power cut). Safe to remove: we hold the
    # lock, only one server runs per data folder, and a .partial is never treated as a backup.
    for stale in config.backup_dir.glob("*.partial"):
        stale.unlink(missing_ok=True)
    work = Path(tempfile.mkdtemp(prefix="backup-", dir=config.tmp_dir if config.tmp_dir.exists() else None))
    started = time.monotonic()
    try:
        state = live_state(conn)
        snap = work / "aims.db"
        db.snapshot(conn, snap)  # consistent even while other connections are writing
        sources = [("aims.db", snap)] + [(f"documents/{p.parent.name}/{p.name}", p) for p in _documents(config)]
        sources += [("reference/template.tex", latex.TEMPLATE_PATH), ("reference/skills.json", VOCABULARY)]
        files = [{"path": arc, "sha256": _sha256(src), "size": src.stat().st_size} for arc, src in sources]
        manifest = {
            "format": FORMAT, "app": "apogee", "app_version": __version__, "kind": kind, "created_at": _iso(now),
            "schema_version": db.schema_version(conn), **state, "template_version": latex.template_version(),
            "files": files,
        }
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=1))
            for arc, src in sources:
                zf.write(src, arc)
        verify(partial)
        os.replace(partial, final)
    except BaseException as exc:  # includes Ctrl-C / shutdown: never leave a half-written file
        partial.unlink(missing_ok=True)
        if not isinstance(exc, Exception):
            raise
        message = str(exc) if isinstance(exc, BackupError) else f"{type(exc).__name__}: {exc}"
        _log(config, {"event": "backup", "kind": kind, "status": "failed", "error": message})
        raise BackupError(f"Backup failed, and nothing was changed: {message}") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)

    result = {"name": name, "size": final.stat().st_size, "kind": kind, "created_at": manifest["created_at"],
              "seconds": round(time.monotonic() - started, 1), "mirror": None, "deleted": []}
    if mirror_dir:
        result["mirror"] = _mirror(final, Path(mirror_dir))
    result["deleted"] = apply_retention(config, now)
    _log(config, {"event": "backup", "kind": kind, "status": "ok", "name": name, "size": result["size"],
                  "mirror": result["mirror"], "deleted": result["deleted"]})
    return result


def _mirror(path: Path, mirror_dir: Path) -> dict:
    """Copy a verified backup to the user's second location. A failure here doesn't fail the backup."""
    try:
        mirror_dir.mkdir(parents=True, exist_ok=True)
        dest = mirror_dir / path.name
        tmp = dest.with_name(dest.name + ".partial")
        shutil.copyfile(path, tmp)
        if _sha256(tmp) != _sha256(path):
            tmp.unlink(missing_ok=True)
            raise OSError("copy doesn't match the original")
        os.replace(tmp, dest)
        _prune_mirror(mirror_dir, path.parent)
        return {"status": "ok", "path": str(dest)}
    except OSError as exc:
        return {"status": "failed", "error": f"Could not copy to {mirror_dir}: {exc}"}


def _prune_mirror(mirror_dir: Path, backup_dir: Path) -> None:
    """Keep the mirror in step with local retention: remove only mirrored backups that local retention removed."""
    local = {p.name for p in backup_dir.glob("aims-*.zip")}
    oldest_local = min(local, default=None)
    for p in mirror_dir.glob("aims-*.zip"):
        if p.name not in local and oldest_local and p.name < oldest_local and NAME.match(p.name):
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------- list & retention

def list_backups(config: Config) -> list[dict]:
    """Backups on disk, newest first. Reads each manifest only (fast); use verify() for a full check."""
    out = []
    for p in sorted(config.backup_dir.glob("aims-*.zip"), reverse=True):
        if not NAME.match(p.name):
            continue
        entry = {"name": p.name, "size": p.stat().st_size}
        try:
            with zipfile.ZipFile(p) as zf:
                m = json.loads(zf.read("manifest.json"))
            entry.update({k: m.get(k) for k in ("kind", "created_at", "schema_version", "data_revision", "changed_at", "app_version")})
            entry["documents"] = sum(f["path"].startswith("documents/") and f["path"].endswith(".pdf") for f in m["files"])
            entry["readable"] = True
        except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
            entry.update({"readable": False, "error": str(exc)})
        out.append(entry)
    return out


def apply_retention(config: Config, now: dt.datetime | None = None) -> list[str]:
    """Delete backups older than RETENTION_DAYS, but never the newest readable one."""
    now = now or _now()
    backups = [b for b in list_backups(config) if b.get("readable")]
    if not backups:
        return []
    newest = max(backups, key=lambda b: b["created_at"])
    cutoff = now - dt.timedelta(days=RETENTION_DAYS)
    deleted = []
    for b in backups:
        if b is not newest and _parse(b["created_at"]) < cutoff:
            (config.backup_dir / b["name"]).unlink(missing_ok=True)
            deleted.append(b["name"])
    return deleted


def status(conn: sqlite3.Connection, config: Config, now: dt.datetime | None = None) -> dict:
    now = now or _now()
    backups = list_backups(config)
    newest = next((b for b in backups if b.get("readable") and b["kind"] != "pre-restore"), None)
    log = read_log(config)
    last_attempt = next((e for e in log if e.get("event") == "backup"), None)
    state = live_state(conn)
    due_at = _parse(newest["created_at"]) + dt.timedelta(hours=DUE_AFTER_HOURS) if newest else now
    return {
        "backups": backups, "newest": newest, "live": state,
        "changed_since_newest": newest is None or changed_since(state, newest),
        "due": is_due(conn, config, now), "next_due_at": _iso(due_at),
        "last_attempt": last_attempt, "log": log[:10],
    }


def is_due(conn: sqlite3.Connection, config: Config, now: dt.datetime | None = None) -> bool:
    """A daily backup is due if the newest one is over 24 h old (or missing) and data changed since."""
    now = now or _now()
    readable = [b for b in list_backups(config) if b.get("readable")]
    if not readable:
        return True
    newest = max(readable, key=lambda b: b["created_at"])
    old_enough = now - _parse(newest["created_at"]) >= dt.timedelta(hours=DUE_AFTER_HOURS)
    return old_enough and changed_since(live_state(conn), newest)


# ---------------------------------------------------------------- restore & import

def _resolve(config: Config, name: str) -> Path:
    if not NAME.match(name):
        raise BackupError("Not a backup name")
    path = config.backup_dir / name
    if not path.is_file():
        raise BackupError(f"Backup {name} not found")
    return path


def restore(conn: sqlite3.Connection, config: Config, name: str, allow_newer: bool = False,
            settings_mirror: str = "", now: dt.datetime | None = None) -> dict:
    with _LOCK:
        now = now or _now()
        path = _resolve(config, name)
        manifest = verify(path)
        latest = db.available_migrations()[-1][0]
        if manifest["schema_version"] > latest:
            raise BackupError("This backup was made by a newer version of Apogee. Update Apogee before restoring it.")
        state = live_state(conn)
        if changed_since(state, manifest) and not allow_newer:
            raise NewerDataError(
                "Your current data has changes made after this backup. Restoring would undo them.",
                {"backup_created_at": manifest["created_at"], "live_changed_at": state["changed_at"],
                 "changes_since": max(state["data_revision"] - manifest["data_revision"], 0)})

        # A safety net first: if this fails, nothing below runs.
        safety = _create(conn, config, "pre-restore", now, settings_mirror)

        work = Path(tempfile.mkdtemp(prefix="restore-", dir=config.tmp_dir if config.tmp_dir.exists() else None))
        try:
            with zipfile.ZipFile(path) as zf:
                zf.extract("aims.db", work)
                docs = [n for n in zf.namelist() if n.startswith("documents/")]
                restored_docs = 0
                for n in docs:
                    dest = config.documents_dir / n.split("/", 1)[1]
                    if not dest.exists():  # documents are immutable; never overwrite one
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        tmp = dest.with_name(dest.name + ".partial")
                        with zf.open(n) as src, open(tmp, "wb") as out:
                            shutil.copyfileobj(src, out)
                        os.replace(tmp, dest)
                        restored_docs += 1
            source = sqlite3.connect(work / "aims.db")
            try:
                source.backup(conn)  # page-by-page copy into the live database, under its own lock
            finally:
                source.close()
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise BackupError(f"Restored database failed its integrity check ({check}). "
                                  f"Your previous data is in {safety['name']}.")
            db.migrate(conn, config.backup_dir)  # bring an older backup up to the current schema
        except BackupError:
            _log(config, {"event": "restore", "status": "failed", "name": name, "safety": safety["name"]})
            raise
        except Exception as exc:
            _log(config, {"event": "restore", "status": "failed", "name": name, "safety": safety["name"],
                          "error": f"{type(exc).__name__}: {exc}"})
            raise BackupError(f"Restore failed: {exc}. Your previous data is saved in {safety['name']}.") from None
        finally:
            shutil.rmtree(work, ignore_errors=True)

        _log(config, {"event": "restore", "status": "ok", "name": name, "safety": safety["name"]})
        return {"restored": name, "safety_backup": safety["name"], "documents_restored": restored_docs,
                "schema_version": db.schema_version(conn)}


def import_file(config: Config, source: Path, now: dt.datetime | None = None) -> dict:
    """Bring in a backup from elsewhere (e.g. an external drive). Verified before it's kept."""
    manifest = verify(source)
    created = _parse(manifest["created_at"])
    name = f"aims-{created.strftime('%Y%m%d-%H%M%S')}-imported.zip"
    dest = config.backup_dir / name
    if not dest.exists():
        tmp = dest.with_name(name + ".partial")
        shutil.copyfile(source, tmp)
        if _sha256(tmp) != _sha256(source):
            tmp.unlink(missing_ok=True)
            raise BackupError("The copy didn't match the uploaded file")
        os.replace(tmp, dest)
    _log(config, {"event": "import", "status": "ok", "name": name})
    return {"name": name, "created_at": manifest["created_at"], "kind": manifest["kind"]}


# ---------------------------------------------------------------- scheduler

def run_if_due(config: Config, now: dt.datetime | None = None) -> dict | None:
    """One scheduler tick: back up if due. Failures are logged (and shown in the UI), never raised."""
    from aims import settings_store

    conn = db.connect(config.db_path)
    try:
        if not is_due(conn, config, now):
            return None
        mirror = settings_store.load(conn).backup_mirror_dir
        return create(conn, config, "auto", now, mirror)
    except BackupError:
        return None  # already logged by create()
    finally:
        conn.close()


def start_scheduler(config: Config, first_delay_s: float = 15) -> threading.Thread:
    """Daily backups while Apogee runs. The first check soon after start catches up on missed days."""
    def loop():
        time.sleep(first_delay_s)
        while True:
            try:
                run_if_due(config)
            except Exception as exc:  # never let the scheduler die silently
                _log(config, {"event": "backup", "kind": "auto", "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            time.sleep(CHECK_EVERY_S)

    t = threading.Thread(target=loop, name="apogee-backups", daemon=True)
    t.start()
    return t
