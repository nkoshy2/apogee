"""FastAPI application: JSON API under /api plus the static frontend.

Business logic lives in the aims.* modules; this file only translates HTTP.

No `from __future__ import annotations` here: FastAPI must resolve the locally
defined `Conn` dependency annotation at definition time.
"""

import sqlite3
from pathlib import Path
from typing import Annotated, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from aims import APP_ID, __version__, applications, db, opportunities, settings_store
from aims.config import Config
from aims.web import security

STATIC_DIR = Path(__file__).parent / "static"


class ImportIn(BaseModel):
    format: Literal["csv", "json"]
    content: str = Field(max_length=5_000_000)


def conflict(detail: str, existing_id: int) -> JSONResponse:
    return JSONResponse({"detail": detail, "existing_id": existing_id}, status_code=409)


def create_app(config: Config) -> FastAPI:
    config.ensure_dirs()
    conn = db.connect(config.db_path)
    try:
        db.migrate(conn, config.backup_dir)
    finally:
        conn.close()

    app = FastAPI(title="AIMS", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
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

    # ---- settings

    @app.get("/api/settings")
    def get_settings(conn: Conn) -> settings_store.Settings:
        return settings_store.load(conn)

    @app.put("/api/settings")
    def put_settings(body: settings_store.Settings, conn: Conn) -> settings_store.Settings:
        settings_store.save(conn, body)
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

    # ---- server

    @app.post("/api/shutdown")
    def shutdown(request: Request):
        server = request.app.state.server
        if server is None:
            raise HTTPException(409, "Not running under the AIMS server")
        server.should_exit = True
        return {"detail": "Shutting down"}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
