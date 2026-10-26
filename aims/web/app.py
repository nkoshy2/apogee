"""FastAPI application: JSON API under /api plus the static frontend.

Business logic lives in the aims.* modules; this file only translates HTTP.

No `from __future__ import annotations` here: FastAPI must resolve the locally
defined `Conn` dependency annotation at definition time.
"""

import sqlite3
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from aims import APP_ID, __version__, db, opportunities, settings_store
from aims.config import Config
from aims.web import security

STATIC_DIR = Path(__file__).parent / "static"


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

    @app.get("/api/settings")
    def get_settings(conn: Conn) -> settings_store.Settings:
        return settings_store.load(conn)

    @app.put("/api/settings")
    def put_settings(body: settings_store.Settings, conn: Conn) -> settings_store.Settings:
        settings_store.save(conn, body)
        return settings_store.load(conn)

    @app.get("/api/opportunities")
    def list_opportunities(conn: Conn):
        return opportunities.list_opportunities(conn)

    @app.post("/api/opportunities", status_code=201)
    def create_opportunity(body: opportunities.OpportunityIn, conn: Conn):
        try:
            new_id = opportunities.create_manual(conn, body)
        except opportunities.DuplicateOpportunity as exc:
            return JSONResponse({"detail": str(exc), "existing_id": exc.existing_id}, status_code=409)
        return opportunities.get_opportunity(conn, new_id)

    @app.get("/api/opportunities/{opportunity_id}")
    def get_opportunity(opportunity_id: int, conn: Conn):
        found = opportunities.get_opportunity(conn, opportunity_id)
        if found is None:
            raise HTTPException(404, "Opportunity not found")
        return found

    @app.post("/api/shutdown")
    def shutdown(request: Request):
        server = request.app.state.server
        if server is None:
            raise HTTPException(409, "Not running under the AIMS server")
        server.should_exit = True
        return {"detail": "Shutting down"}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
