"""Verified experience database: experiences, resume statements, and skills.

Independent of any document format. Nothing here generates or alters wording: every
statement is written by the user, and only statements the user has verified and then
approved are eligible for a resume. Editing an approved statement's text sends it back
to draft, because the approval was for the old wording.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from aims.db import transaction, utc_now

Kind = Literal["education", "employment", "research", "project", "leadership", "achievement"]
KINDS: tuple[str, ...] = ("education", "employment", "research", "project", "leadership", "achievement")
StatementStatus = Literal["draft", "verified", "approved"]
Month = Annotated[str, StringConstraints(pattern=r"^(19|20)\d\d-(0[1-9]|1[0-2])$")]  # YYYY-MM


def Text(max_length: int, min_length: int = 0):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)]


Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class ExperienceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    title: Text(200, 1)
    organization: Text(200) = ""
    location: Text(120) = ""
    start_date: Month | None = None
    end_date: Month | None = None
    is_current: bool = False
    protected: bool = False

    @model_validator(mode="after")
    def _dates(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        return self


class ExperiencePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind | None = None
    title: Text(200, 1) | None = None
    organization: Text(200) | None = None
    location: Text(120) | None = None
    start_date: Month | None = None
    end_date: Month | None = None
    is_current: bool | None = None
    protected: bool | None = None


class StatementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Text(600, 1)
    protected: bool = False
    tags: Annotated[list[Tag], Field(max_length=20)] = []


class StatementPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Text(600, 1) | None = None
    status: StatementStatus | None = None
    protected: bool | None = None
    tags: Annotated[list[Tag], Field(max_length=20)] | None = None
    sort_order: int | None = None


class SkillIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Text(80, 1)
    category: Text(60, 1) = "Technical"


class SkillPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Text(80, 1) | None = None
    category: Text(60, 1) | None = None


class NotFound(LookupError):
    pass


class Conflict(Exception):
    pass


# ---------------------------------------------------------------- experiences

def _bools(row: dict, *fields: str) -> dict:
    for f in fields:
        row[f] = bool(row[f])
    return row


def _statement_row(row: sqlite3.Row) -> dict:
    d = _bools(dict(row), "protected")
    d["tags"] = json.loads(d.pop("tags_json"))
    return d


def list_experiences(conn: sqlite3.Connection) -> list[dict]:
    """All experiences with their statements, in display order (kind, then most recent first)."""
    statements: dict[int, list[dict]] = {}
    for s in conn.execute("SELECT * FROM statements ORDER BY sort_order, id"):
        statements.setdefault(s["experience_id"], []).append(_statement_row(s))
    used = {r[0] for r in conn.execute("SELECT DISTINCT statement_id FROM resume_statements")}
    result = []
    for e in conn.execute(
        "SELECT * FROM experiences ORDER BY "
        f"CASE kind {' '.join(f'WHEN {k!r} THEN {i}' for i, k in enumerate(KINDS))} END, "
        "is_current DESC, COALESCE(end_date, start_date, '') DESC, id DESC"
    ):
        d = _bools(dict(e), "is_current", "protected")
        d["statements"] = statements.get(e["id"], [])
        for s in d["statements"]:
            s["used_in_resume"] = s["id"] in used
        result.append(d)
    return result


def get_experience(conn: sqlite3.Connection, experience_id: int) -> dict:
    found = next((e for e in list_experiences(conn) if e["id"] == experience_id), None)
    if found is None:
        raise NotFound("Experience not found")
    return found


def create_experience(conn: sqlite3.Connection, data: ExperienceIn) -> int:
    now = utc_now()
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO experiences (kind, title, organization, location, start_date, end_date, is_current, "
            "protected, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data.kind, data.title, data.organization, data.location, data.start_date, data.end_date,
             int(data.is_current), int(data.protected), now, now),
        )
        return cur.lastrowid


def update_experience(conn: sqlite3.Connection, experience_id: int, patch: ExperiencePatch) -> None:
    changes = {f: getattr(patch, f) for f in patch.model_fields_set}
    # None clears a date; for other fields it means "leave alone".
    changes = {f: v for f, v in changes.items() if v is not None or f in ("start_date", "end_date")}
    for f in ("is_current", "protected"):
        if f in changes:
            changes[f] = int(changes[f])
    with transaction(conn):
        row = conn.execute("SELECT start_date, end_date FROM experiences WHERE id = ?", (experience_id,)).fetchone()
        if row is None:
            raise NotFound("Experience not found")
        start = changes.get("start_date", row["start_date"])
        end = changes.get("end_date", row["end_date"])
        if start and end and end < start:
            raise Conflict("End date is before start date")
        if changes:
            changes["updated_at"] = utc_now()
            assignments = ", ".join(f"{c} = ?" for c in changes)  # column names come from the model
            conn.execute(f"UPDATE experiences SET {assignments} WHERE id = ?", (*changes.values(), experience_id))


def delete_experience(conn: sqlite3.Connection, experience_id: int) -> None:
    with transaction(conn):
        if conn.execute("SELECT 1 FROM experiences WHERE id = ?", (experience_id,)).fetchone() is None:
            raise NotFound("Experience not found")
        if conn.execute("SELECT 1 FROM statements WHERE experience_id = ? LIMIT 1", (experience_id,)).fetchone():
            raise Conflict("Delete or move this experience's statements first.")
        conn.execute("DELETE FROM experiences WHERE id = ?", (experience_id,))


# ---------------------------------------------------------------- statements

def create_statement(conn: sqlite3.Connection, experience_id: int, data: StatementIn) -> int:
    now = utc_now()
    with transaction(conn):
        if conn.execute("SELECT 1 FROM experiences WHERE id = ?", (experience_id,)).fetchone() is None:
            raise NotFound("Experience not found")
        order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM statements WHERE experience_id = ?", (experience_id,)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO statements (experience_id, text, status, protected, tags_json, sort_order, created_at, updated_at) "
            "VALUES (?, ?, 'draft', ?, ?, ?, ?, ?)",
            (experience_id, data.text, int(data.protected), json.dumps(data.tags), order, now, now),
        )
        return cur.lastrowid


def update_statement(conn: sqlite3.Connection, statement_id: int, patch: StatementPatch) -> None:
    given = patch.model_fields_set
    with transaction(conn):
        row = conn.execute("SELECT text, status FROM statements WHERE id = ?", (statement_id,)).fetchone()
        if row is None:
            raise NotFound("Statement not found")
        changes: dict = {}
        text_changed = "text" in given and patch.text is not None and patch.text != row["text"]
        if text_changed:
            changes["text"] = patch.text
        # New wording needs new verification and approval.
        status = patch.status if "status" in given and patch.status else ("draft" if text_changed else row["status"])
        if status == "approved" and (text_changed or row["status"] not in ("verified", "approved")):
            raise Conflict("Verify a statement before approving it.")
        if status != row["status"]:
            changes["status"] = status
        if "protected" in given and patch.protected is not None:
            changes["protected"] = int(patch.protected)
        if "tags" in given and patch.tags is not None:
            changes["tags_json"] = json.dumps(patch.tags)
        if "sort_order" in given and patch.sort_order is not None:
            changes["sort_order"] = patch.sort_order
        if changes:
            changes["updated_at"] = utc_now()
            assignments = ", ".join(f"{c} = ?" for c in changes)  # column names are fixed above
            conn.execute(f"UPDATE statements SET {assignments} WHERE id = ?", (*changes.values(), statement_id))


def delete_statement(conn: sqlite3.Connection, statement_id: int) -> None:
    with transaction(conn):
        if conn.execute("SELECT 1 FROM statements WHERE id = ?", (statement_id,)).fetchone() is None:
            raise NotFound("Statement not found")
        if conn.execute("SELECT 1 FROM resume_statements WHERE statement_id = ? LIMIT 1", (statement_id,)).fetchone():
            raise Conflict("This statement is part of a generated resume, so it is kept for that record. "
                           "Set it back to draft to stop using it.")
        conn.execute("DELETE FROM statements WHERE id = ?", (statement_id,))


# ---------------------------------------------------------------- skills

def list_skills(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, category FROM skills ORDER BY category COLLATE NOCASE, name COLLATE NOCASE")]


def create_skill(conn: sqlite3.Connection, data: SkillIn) -> int:
    with transaction(conn):
        if conn.execute("SELECT 1 FROM skills WHERE name = ?", (data.name,)).fetchone():
            raise Conflict(f"{data.name} is already in your skills.")
        return conn.execute(
            "INSERT INTO skills (name, category, created_at) VALUES (?, ?, ?)", (data.name, data.category, utc_now())
        ).lastrowid


def update_skill(conn: sqlite3.Connection, skill_id: int, patch: SkillPatch) -> None:
    changes = {f: getattr(patch, f) for f in patch.model_fields_set if getattr(patch, f) is not None}
    with transaction(conn):
        if conn.execute("SELECT 1 FROM skills WHERE id = ?", (skill_id,)).fetchone() is None:
            raise NotFound("Skill not found")
        if "name" in changes and conn.execute(
            "SELECT 1 FROM skills WHERE name = ? AND id != ?", (changes["name"], skill_id)
        ).fetchone():
            raise Conflict(f"{changes['name']} is already in your skills.")
        if changes:
            assignments = ", ".join(f"{c} = ?" for c in changes)
            conn.execute(f"UPDATE skills SET {assignments} WHERE id = ?", (*changes.values(), skill_id))


def delete_skill(conn: sqlite3.Connection, skill_id: int) -> None:
    with transaction(conn):
        if conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,)).rowcount == 0:
            raise NotFound("Skill not found")
