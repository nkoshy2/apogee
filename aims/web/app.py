"""FastAPI application: JSON API under /api plus the static frontend.

Business logic lives in the aims.* modules; this file only translates HTTP.

No `from __future__ import annotations` here: FastAPI must resolve the locally
defined `Conn` dependency annotation at definition time.
"""

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Annotated, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from aims import APP_ID, __version__, applications, backup, db, experience, opportunities, settings_store, summary
from aims.config import Config
from aims.discovery import importer
from aims.resume import service as resume_service
from aims.web import security

STATIC_DIR = Path(__file__).parent / "static"


class ImportIn(BaseModel):
    format: Literal["csv", "json"]
    content: str = Field(max_length=5_000_000)


MAX_BACKUP_UPLOAD = 2_000_000_000


class RestoreRequest(BaseModel):
    confirm: str  # must equal the backup name: a deliberate, explicit confirmation
    allow_newer: bool = False


class FeedUpload(BaseModel):
    content: str = Field(max_length=60_000_000)


class ResumeRequest(BaseModel):
    opportunity_id: int | None = None  # None: a general resume
    page_limit: int = Field(1, ge=1, le=3)
    include_ids: list[int] = Field(default_factory=list, max_length=500)
    exclude_ids: list[int] = Field(default_factory=list, max_length=500)


def conflict(detail: str, existing_id: int) -> JSONResponse:
    return JSONResponse({"detail": detail, "existing_id": existing_id}, status_code=409)


def create_app(config: Config) -> FastAPI:
    config.ensure_dirs()
    conn = db.connect(config.db_path)
    try:
        db.migrate(conn, config.backup_dir)
    finally:
        conn.close()

    app = FastAPI(title="Apogee", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.server = None  # set by aims.server so /api/shutdown can stop uvicorn
    security.install(app, config.port)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    @app.get("/api/health")
    def health(conn: Conn):
        return {
            "app": APP_ID,
            "version": __version__,
            "schema_version": db.schema_version(conn),
            "database": conn.execute("PRAGMA quick_check").fetchone()[0],
            "data_dir": str(config.data_dir),
        }

    @app.get("/api/summary")
    def get_summary(conn: Conn):
        return summary.summarize(conn, settings_store.load(conn))

    # ---- settings

    @app.get("/api/settings")
    def get_settings(conn: Conn) -> settings_store.Settings:
        return settings_store.load(conn)

    @app.put("/api/settings")
    def put_settings(body: settings_store.Settings, conn: Conn) -> settings_store.Settings:
        # Only the top-level sections present in the request are replaced, so a form that edits
        # one section (e.g. the resume header) can never wipe the others.
        current = settings_store.load(conn)
        merged = current.model_copy(update={f: getattr(body, f) for f in body.model_fields_set})
        settings_store.save(conn, merged)
        return settings_store.load(conn)

    # ---- opportunities

    @app.get("/api/opportunities")
    def list_opportunities(filters: Annotated[opportunities.Filters, Query()], conn: Conn):
        return opportunities.browse(conn, filters, settings_store.load(conn))

    @app.post("/api/opportunities", status_code=201)
    def create_opportunity(body: opportunities.OpportunityIn, conn: Conn):
        try:
            new_id = opportunities.create_manual(conn, body)
        except opportunities.DuplicateOpportunity as exc:
            return conflict(str(exc), exc.existing_id)
        return opportunities.get_ranked(conn, new_id, settings_store.load(conn))

    @app.post("/api/opportunities/import")
    def import_opportunities(body: ImportIn, conn: Conn):
        try:
            rows = opportunities.parse_import(body.format, body.content)
        except opportunities.ImportFormatError as exc:
            raise HTTPException(400, str(exc))
        results = opportunities.import_manual(conn, rows)
        counts = {k: sum(r["result"] == k for r in results) for k in ("created", "duplicate", "invalid")}
        return {"counts": counts, "rows": results}

    @app.get("/api/opportunities/{opportunity_id}")
    def get_opportunity(opportunity_id: int, conn: Conn):
        found = opportunities.get_ranked(conn, opportunity_id, settings_store.load(conn))
        if found is None:
            raise HTTPException(404, "Opportunity not found")
        return found

    @app.patch("/api/opportunities/{opportunity_id}")
    def patch_opportunity(opportunity_id: int, body: opportunities.OpportunityPatch, conn: Conn):
        try:
            opportunities.update(conn, opportunity_id, body)
        except opportunities.NotFound as exc:
            raise HTTPException(404, str(exc))
        except opportunities.NotEditable as exc:
            raise HTTPException(409, str(exc))
        except opportunities.DuplicateOpportunity as exc:
            return conflict(str(exc), exc.existing_id)
        return opportunities.get_ranked(conn, opportunity_id, settings_store.load(conn))

    # ---- applications

    @app.get("/api/applications")
    def list_applications(conn: Conn, status: applications.StatusFilter = "all"):
        return applications.list_applications(conn, status)

    @app.post("/api/applications", status_code=201)
    def create_application(body: applications.ApplicationIn, conn: Conn):
        try:
            app_id = applications.create(conn, body)
        except applications.NotFound as exc:
            raise HTTPException(404, str(exc))
        except applications.DuplicateApplication as exc:
            return conflict(str(exc), exc.existing_id)
        return applications.get_application(conn, app_id)

    @app.get("/api/applications/{app_id}")
    def get_application(app_id: int, conn: Conn):
        found = applications.get_application(conn, app_id)
        if found is None:
            raise HTTPException(404, "Application not found")
        return found

    @app.patch("/api/applications/{app_id}")
    def patch_application(app_id: int, body: applications.ApplicationPatch, conn: Conn):
        try:
            applications.update(conn, app_id, body)
        except applications.NotFound as exc:
            raise HTTPException(404, str(exc))
        return applications.get_application(conn, app_id)

    @app.post("/api/applications/{app_id}/events", status_code=201)
    def add_application_note(app_id: int, body: applications.NoteIn, conn: Conn):
        try:
            applications.add_note(conn, app_id, body.note)
        except applications.NotFound as exc:
            raise HTTPException(404, str(exc))
        return applications.get_application(conn, app_id)

    # ---- verified experience

    def run(fn, *args):
        """Call a business function, translating its errors into HTTP responses."""
        try:
            return fn(*args)
        except (experience.NotFound, resume_service.NotFound) as exc:
            raise HTTPException(404, str(exc))
        except experience.Conflict as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/experiences")
    def list_experiences(conn: Conn):
        return experience.list_experiences(conn)

    @app.post("/api/experiences", status_code=201)
    def create_experience(body: experience.ExperienceIn, conn: Conn):
        return experience.get_experience(conn, experience.create_experience(conn, body))

    @app.patch("/api/experiences/{experience_id}")
    def patch_experience(experience_id: int, body: experience.ExperiencePatch, conn: Conn):
        run(experience.update_experience, conn, experience_id, body)
        return experience.get_experience(conn, experience_id)

    @app.delete("/api/experiences/{experience_id}", status_code=204)
    def delete_experience(experience_id: int, conn: Conn):
        run(experience.delete_experience, conn, experience_id)

    @app.post("/api/experiences/{experience_id}/statements", status_code=201)
    def create_statement(experience_id: int, body: experience.StatementIn, conn: Conn):
        run(experience.create_statement, conn, experience_id, body)
        return experience.get_experience(conn, experience_id)

    @app.patch("/api/statements/{statement_id}")
    def patch_statement(statement_id: int, body: experience.StatementPatch, conn: Conn):
        run(experience.update_statement, conn, statement_id, body)
        exp_id = conn.execute("SELECT experience_id FROM statements WHERE id = ?", (statement_id,)).fetchone()[0]
        return experience.get_experience(conn, exp_id)

    @app.delete("/api/statements/{statement_id}", status_code=204)
    def delete_statement(statement_id: int, conn: Conn):
        run(experience.delete_statement, conn, statement_id)

    @app.get("/api/skills")
    def list_skills(conn: Conn):
        return experience.list_skills(conn)

    @app.post("/api/skills", status_code=201)
    def create_skill(body: experience.SkillIn, conn: Conn):
        run(experience.create_skill, conn, body)
        return experience.list_skills(conn)

    @app.patch("/api/skills/{skill_id}")
    def patch_skill(skill_id: int, body: experience.SkillPatch, conn: Conn):
        run(experience.update_skill, conn, skill_id, body)
        return experience.list_skills(conn)

    @app.delete("/api/skills/{skill_id}", status_code=204)
    def delete_skill(skill_id: int, conn: Conn):
        run(experience.delete_skill, conn, skill_id)

    # ---- resumes

    @app.post("/api/resumes/plan")
    def plan_resume(body: ResumeRequest, conn: Conn):
        return run(resume_service.plan, conn, body.opportunity_id, body.include_ids, body.exclude_ids)

    @app.post("/api/resumes", status_code=201)
    def generate_resume(body: ResumeRequest, conn: Conn):
        try:
            return run(resume_service.generate, conn, config, settings_store.load(conn), body.opportunity_id,
                       body.page_limit, body.include_ids, body.exclude_ids)
        except resume_service.GenerationError as exc:
            return JSONResponse({"detail": str(exc), **exc.details}, status_code=422)

    @app.get("/api/resumes")
    def list_resumes(conn: Conn, opportunity_id: int | None = None):
        return resume_service.list_documents(conn, opportunity_id)

    @app.get("/api/resumes/{doc_id}")
    def get_resume(doc_id: int, conn: Conn):
        return run(resume_service.get_document, conn, doc_id)

    @app.get("/api/resumes/{doc_id}/{kind}")
    def resume_file(doc_id: int, kind: Literal["pdf", "tex"], conn: Conn, download: bool = False):
        path, name = run(resume_service.document_file, conn, config, doc_id, kind)
        media = "application/pdf" if kind == "pdf" else "text/plain; charset=utf-8"
        disposition = "attachment" if download or kind == "tex" else "inline"
        return FileResponse(path, media_type=media, filename=name, content_disposition_type=disposition)

    # ---- discovery

    @app.get("/api/discovery/health")
    def discovery_health(conn: Conn):
        return importer.health(conn)

    @app.post("/api/discovery/run")
    def discovery_run(conn: Conn):
        """Check every source from this computer now (takes a few seconds to a minute)."""
        try:
            return importer.run_here(conn)
        except importer.FeedError as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/discovery/sync")
    def discovery_sync(conn: Conn):
        """Import any cloud runs not yet imported (called when Apogee opens)."""
        url = settings_store.load(conn).discovery_feed_url
        if not url:
            raise HTTPException(409, "No cloud feed is set up. Add its URL in Settings → Discovery.")
        try:
            return importer.sync_remote(conn, url)
        except importer.FeedError as exc:
            raise HTTPException(502, str(exc))

    @app.post("/api/discovery/import")
    def discovery_import(body: FeedUpload, conn: Conn):
        try:
            return importer.import_feed(conn, json.loads(body.content), origin="file")
        except ValueError as exc:  # includes FeedError and JSON errors
            raise HTTPException(422, str(exc))

    # ---- backups

    @app.get("/api/backups")
    def backup_status(conn: Conn):
        return backup.status(conn, config)

    @app.post("/api/backups", status_code=201)
    def create_backup(conn: Conn):
        try:
            return backup.create(conn, config, "manual", settings_mirror=settings_store.load(conn).backup_mirror_dir)
        except backup.BackupError as exc:
            raise HTTPException(500, str(exc))

    @app.post("/api/backups/import", status_code=201)
    async def import_backup(request: Request):
        """Upload a backup file (e.g. from an external drive). Streamed to disk, then fully verified."""
        config.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = config.tmp_dir / f"upload-{uuid.uuid4().hex}.zip"
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_BACKUP_UPLOAD:
                        raise HTTPException(413, "That file is too large to be an Apogee backup.")
                    f.write(chunk)
            return backup.import_file(config, tmp)
        except backup.BackupError as exc:
            raise HTTPException(422, str(exc))
        finally:
            tmp.unlink(missing_ok=True)

    @app.post("/api/backups/{name}/verify")
    def verify_backup(name: str):
        try:
            path = backup._resolve(config, name)
            manifest = backup.verify(path)
        except backup.BackupError as exc:
            raise HTTPException(422, str(exc))
        return {"name": name, "ok": True, "created_at": manifest["created_at"], "files": len(manifest["files"])}

    @app.post("/api/backups/{name}/restore")
    def restore_backup(name: str, body: RestoreRequest, conn: Conn):
        if body.confirm != name:
            raise HTTPException(400, "Restore not confirmed: send the backup's name as 'confirm'.")
        try:
            return backup.restore(conn, config, name, allow_newer=body.allow_newer,
                                  settings_mirror=settings_store.load(conn).backup_mirror_dir)
        except backup.NewerDataError as exc:
            return JSONResponse({"detail": str(exc), "newer": exc.details}, status_code=409)
        except backup.BackupError as exc:
            raise HTTPException(422, str(exc))

    # ---- server

    @app.post("/api/shutdown")
    def shutdown(request: Request):
        server = request.app.state.server
        if server is None:
            raise HTTPException(409, "Not running under the Apogee server")
        server.should_exit = True
        return {"detail": "Shutting down"}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
