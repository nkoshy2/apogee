"""Manual application tracking with an append-only history of status changes and notes.

Status is only ever changed by an explicit user action; nothing here infers submission.
Any transition between statuses is allowed, because real processes skip and repeat steps.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from aims.db import transaction, utc_now

STATUSES = ("preparing", "submitted", "assessment", "interviewing", "offer", "rejected", "withdrawn")
CLOSED_STATUSES = ("rejected", "withdrawn")
Status = Literal["preparing", "submitted", "assessment", "interviewing", "offer", "rejected", "withdrawn"]
StatusFilter = Literal["active", "closed", "all", "preparing", "submitted", "assessment", "interviewing", "offer", "rejected", "withdrawn"]

Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
Notes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=20_000)]


class ApplicationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: int
    status: Status = "preparing"
    deadline: dt.date | None = None  # defaults to the opportunity's deadline
    notes: Notes = ""


class ApplicationPatch(BaseModel):
    """Only fields present in the request are changed. `event_note` is recorded in the history."""

    model_config = ConfigDict(extra="forbid")

    status: Status | None = None
    submitted_on: dt.date | None = None
    deadline: dt.date | None = None
    notes: Notes | None = None
    event_note: Note = ""


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class NotFound(LookupError):
    pass


class DuplicateApplication(Exception):
    def __init__(self, existing_id: int):
        super().__init__(f"This opportunity already has an application (id {existing_id})")
        self.existing_id = existing_id


def _event(conn: sqlite3.Connection, app_id: int, at: str, from_status: str | None, to_status: str, note: str) -> None:
    conn.execute(
        "INSERT INTO application_events (application_id, at, from_status, to_status, note) VALUES (?, ?, ?, ?, ?)",
        (app_id, at, from_status, to_status, note),
    )


def create(conn: sqlite3.Connection, data: ApplicationIn) -> int:
    now = utc_now()
    with transaction(conn):
        opp = conn.execute("SELECT id, deadline FROM opportunities WHERE id = ?", (data.opportunity_id,)).fetchone()
        if opp is None:
            raise NotFound("Opportunity not found")
        existing = conn.execute("SELECT id FROM applications WHERE opportunity_id = ?", (data.opportunity_id,)).fetchone()
        if existing:
            raise DuplicateApplication(existing["id"])
        deadline = data.deadline.isoformat() if data.deadline else opp["deadline"]
        cur = conn.execute(
            "INSERT INTO applications (opportunity_id, status, deadline, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (data.opportunity_id, data.status, deadline, data.notes, now, now),
        )
        app_id = cur.lastrowid
        _event(conn, app_id, now, None, data.status, "Application created")
        # Working on an application implies interest: Discovered -> Saved -> Preparing.
        conn.execute(
            "UPDATE opportunities SET user_state = 'saved', updated_at = ? WHERE id = ? AND user_state != 'saved'",
            (now, data.opportunity_id),
        )
        return app_id


def update(conn: sqlite3.Connection, app_id: int, patch: ApplicationPatch) -> None:
    given = patch.model_fields_set
    now = utc_now()
    with transaction(conn):
        row = conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()
        if row is None:
            raise NotFound("Application not found")
        changes: dict = {}
        for field in ("submitted_on", "deadline"):
            if field in given:
                value = getattr(patch, field)
                changes[field] = value.isoformat() if value else None
        if "notes" in given and patch.notes is not None:
            changes["notes"] = patch.notes
        if patch.status is not None and patch.status != row["status"]:
            changes["status"] = patch.status
            _event(conn, app_id, now, row["status"], patch.status, patch.event_note)
        elif patch.event_note:
            _event(conn, app_id, now, row["status"], row["status"], patch.event_note)
        if changes:
            changes["updated_at"] = now
            assignments = ", ".join(f"{col} = ?" for col in changes)  # column names are fixed above
            conn.execute(f"UPDATE applications SET {assignments} WHERE id = ?", (*changes.values(), app_id))


def add_note(conn: sqlite3.Connection, app_id: int, note: str) -> None:
    update(conn, app_id, ApplicationPatch(event_note=note))


_SELECT = (
    "SELECT a.*, o.employer, o.title, o.url, o.listing_status FROM applications a "
    "JOIN opportunities o ON o.id = a.opportunity_id"
)


def list_applications(conn: sqlite3.Connection, status: StatusFilter = "all") -> list[dict]:
    if status == "active":
        where, params = f"a.status NOT IN ({','.join('?' * len(CLOSED_STATUSES))})", CLOSED_STATUSES
    elif status == "closed":
        where, params = f"a.status IN ({','.join('?' * len(CLOSED_STATUSES))})", CLOSED_STATUSES
    elif status == "all":
        where, params = "1 = 1", ()
    else:
        where, params = "a.status = ?", (status,)
    rows = conn.execute(
        f"{_SELECT} WHERE {where} ORDER BY a.deadline IS NULL, a.deadline, a.updated_at DESC", params
    )
    return [dict(r) for r in rows]


def get_application(conn: sqlite3.Connection, app_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE a.id = ?", (app_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["events"] = [
        dict(e) for e in conn.execute(
            "SELECT at, from_status, to_status, note FROM application_events WHERE application_id = ? ORDER BY id",
            (app_id,),
        )
    ]
    return result
