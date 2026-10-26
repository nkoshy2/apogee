"""Opportunity storage, URL normalization, duplicate detection, search, and manual import.

Imports from discovery sources (Phase 4) must only write the source-owned columns listed
in migrations/0001_initial.sql. User edits here only touch user-owned columns, except
that manually added opportunities may also have their details corrected.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sqlite3
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, field_validator

from aims import ranking
from aims.db import transaction, utc_now
from aims.settings_store import Settings

OpportunityType = Literal["internship", "co-op", "government", "research", "full-time", "other"]
UserState = Literal["new", "saved", "dismissed"]
StateFilter = Literal["active", "new", "saved", "dismissed", "all"]
SortOrder = Literal["rank", "newest", "deadline", "employer"]

# Query parameters that only track how a link was shared, not which posting it is.
# Kept deliberately narrow: wrongly merging two postings is worse than missing a duplicate.
_TRACKING_PARAMS = {"gh_src", "lever-source", "lever-origin"}

# Fields a user may correct on a manually added opportunity.
MANUAL_FIELDS = ("employer", "title", "url", "location", "opportunity_type", "description", "deadline")

_LIST_COLUMNS = (
    "o.id, o.source, o.employer, o.title, o.url, o.location, o.opportunity_type, o.description, "
    "o.deadline, o.first_seen_at, o.last_seen_at, o.listing_status, o.user_state, "
    "a.id AS application_id, a.status AS application_status"
)
_STATE_SQL = {
    "active": "o.user_state IN ('new', 'saved')",
    "new": "o.user_state = 'new'",
    "saved": "o.user_state = 'saved'",
    "dismissed": "o.user_state = 'dismissed'",
    "all": "1 = 1",
}
MAX_IMPORT_ROWS = 2000


def Text(max_length: int, min_length: int = 0):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)]


def _check_http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http:// or https:// link")
    return value


class OpportunityIn(BaseModel):
    """A manually entered opportunity. All fields are untrusted input."""

    model_config = ConfigDict(extra="forbid")

    employer: Text(200, 1)
    title: Text(300, 1)
    url: Text(2000, 1)
    location: Text(300) = ""
    opportunity_type: OpportunityType = "internship"
    description: Text(100_000) = ""
    deadline: dt.date | None = None
    user_notes: Text(20_000) = ""

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        return _check_http_url(value)


class OpportunityPatch(BaseModel):
    """Only fields present in the request are changed."""

    model_config = ConfigDict(extra="forbid")

    user_state: UserState | None = None
    user_notes: Text(20_000) | None = None
    employer: Text(200, 1) | None = None
    title: Text(300, 1) | None = None
    url: Text(2000, 1) | None = None
    location: Text(300) | None = None
    opportunity_type: OpportunityType | None = None
    description: Text(100_000) | None = None
    deadline: dt.date | None = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        return value if value is None else _check_http_url(value)


class Filters(BaseModel):
    q: str = ""
    state: StateFilter = "active"
    opportunity_type: OpportunityType | None = None
    eligibility: Literal["eligible", "uncertain", "ineligible"] | None = None
    sort: SortOrder = "rank"


class DuplicateOpportunity(Exception):
    def __init__(self, existing_id: int):
        super().__init__(f"An opportunity with this URL already exists (id {existing_id})")
        self.existing_id = existing_id


class NotFound(LookupError):
    pass


class NotEditable(Exception):
    pass


def url_key(url: str) -> str:
    """Normalize a posting URL so trivially different links compare equal."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and (scheme, parts.port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    # http and https links to the same posting are the same posting.
    return urlunsplit(("https" if scheme == "http" else scheme, host, path, urlencode(query), ""))


def _find_by_url_key(conn: sqlite3.Connection, key: str, exclude_id: int | None = None) -> int | None:
    row = conn.execute(
        "SELECT id FROM opportunities WHERE url_key = ? AND id IS NOT ?", (key, exclude_id)
    ).fetchone()
    return row["id"] if row else None


def create_manual(conn: sqlite3.Connection, data: OpportunityIn) -> int:
    key = url_key(data.url)
    now = utc_now()
    with transaction(conn):
        if (existing := _find_by_url_key(conn, key)) is not None:
            raise DuplicateOpportunity(existing)
        cur = conn.execute(
            """INSERT INTO opportunities
               (source, employer, title, url, url_key, location, opportunity_type, description,
                deadline, first_seen_at, last_seen_at, user_notes, created_at, updated_at)
               VALUES ('manual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.employer, data.title, data.url, key, data.location, data.opportunity_type,
                data.description, data.deadline.isoformat() if data.deadline else None,
                now, now, data.user_notes, now, now,
            ),
        )
        return cur.lastrowid


def update(conn: sqlite3.Connection, opportunity_id: int, patch: OpportunityPatch) -> None:
    given = {f: getattr(patch, f) for f in patch.model_fields_set}
    # None means "clear" only for deadline; for every other field it means "leave alone".
    changes = {f: v for f, v in given.items() if v is not None or f == "deadline"}
    if not changes:
        return
    with transaction(conn):
        row = conn.execute("SELECT source FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
        if row is None:
            raise NotFound("Opportunity not found")
        if row["source"] != "manual" and any(f in MANUAL_FIELDS for f in changes):
            raise NotEditable("Only manually added opportunities can be edited; discovered postings are kept in sync with their source.")
        if "deadline" in changes and changes["deadline"] is not None:
            changes["deadline"] = changes["deadline"].isoformat()
        if "url" in changes:
            changes["url_key"] = url_key(changes["url"])
            if (existing := _find_by_url_key(conn, changes["url_key"], exclude_id=opportunity_id)) is not None:
                raise DuplicateOpportunity(existing)
        changes["updated_at"] = utc_now()
        assignments = ", ".join(f"{col} = ?" for col in changes)  # column names come from the model, not the client
        conn.execute(f"UPDATE opportunities SET {assignments} WHERE id = ?", (*changes.values(), opportunity_id))


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(conn: sqlite3.Connection, filters: Filters) -> list[dict]:
    """Rows matching text/state/type filters; every word in q must match some field."""
    where = [_STATE_SQL[filters.state]]
    params: list = []
    if filters.opportunity_type:
        where.append("o.opportunity_type = ?")
        params.append(filters.opportunity_type)
    for word in filters.q.split()[:10]:
        where.append(
            "(o.employer LIKE ? ESCAPE '\\' OR o.title LIKE ? ESCAPE '\\' "
            "OR o.location LIKE ? ESCAPE '\\' OR o.description LIKE ? ESCAPE '\\')"
        )
        params.extend([_like(word)] * 4)
    sql = (
        f"SELECT {_LIST_COLUMNS} FROM opportunities o "
        f"LEFT JOIN applications a ON a.opportunity_id = o.id WHERE {' AND '.join(where)}"
    )
    return [dict(r) for r in conn.execute(sql, params)]


def browse(conn: sqlite3.Connection, filters: Filters, settings: Settings) -> list[dict]:
    """Search, rank, filter by eligibility, and sort. Descriptions are omitted from the result."""
    results = []
    settings_key = settings.model_dump_json()
    for row in search(conn, filters):
        ranked = ranking.rank_cached(row, settings, settings_key)
        if filters.eligibility and ranked["eligibility"]["status"] != filters.eligibility:
            continue
        row.pop("description")
        row["eligibility"] = ranked["eligibility"]["status"]
        row["score"] = ranked["score"]
        row["relevance"] = ranked["relevance"]["score"]
        row["preference"] = ranked["preference"]["score"]
        results.append(row)

    if filters.sort == "rank":
        results.sort(key=lambda r: (ranking.ELIGIBILITY_ORDER[r["eligibility"]], -r["score"], -r["id"]))
    elif filters.sort == "newest":
        results.sort(key=lambda r: (r["first_seen_at"], r["id"]), reverse=True)
    elif filters.sort == "deadline":
        results.sort(key=lambda r: (r["deadline"] is None, r["deadline"] or "", r["id"]))
    else:
        results.sort(key=lambda r: (r["employer"].lower(), r["title"].lower()))
    return results


def get_opportunity(conn: sqlite3.Connection, opportunity_id: int) -> dict | None:
    row = conn.execute(
        "SELECT o.*, a.id AS application_id, a.status AS application_status FROM opportunities o "
        "LEFT JOIN applications a ON a.opportunity_id = o.id WHERE o.id = ?",
        (opportunity_id,),
    ).fetchone()
    return dict(row) if row else None


def get_ranked(conn: sqlite3.Connection, opportunity_id: int, settings: Settings) -> dict | None:
    opp = get_opportunity(conn, opportunity_id)
    if opp is not None:
        opp["ranking"] = ranking.rank(opp, settings)
    return opp


# ---------------------------------------------------------------- bulk import

class ImportFormatError(ValueError):
    pass


def parse_import(fmt: Literal["csv", "json"], content: str) -> list[dict]:
    if fmt == "json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ImportFormatError(f"Invalid JSON: {exc}") from None
        if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
            raise ImportFormatError("JSON must be a list of objects")
        rows = data
    else:
        reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
        allowed = set(OpportunityIn.model_fields)
        header = [h.strip() for h in (reader.fieldnames or [])]
        if unknown := [h for h in header if h not in allowed]:
            raise ImportFormatError(f"Unknown CSV columns: {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}")
        rows = []
        for raw in reader:
            if None in raw:
                raise ImportFormatError(f"CSV row {reader.line_num} has more values than columns")
            # Blank CSV cells mean "not provided".
            rows.append({k.strip(): v for k, v in raw.items() if v is not None and v.strip() != ""})
    if len(rows) > MAX_IMPORT_ROWS:
        raise ImportFormatError(f"At most {MAX_IMPORT_ROWS} rows per import")
    return rows


def import_manual(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Create each valid, non-duplicate row. Each row is independent; nothing existing is modified."""
    results = []
    for number, raw in enumerate(rows, start=1):
        try:
            data = OpportunityIn.model_validate(raw)
        except ValidationError as exc:
            errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            results.append({"row": number, "result": "invalid", "detail": errors})
            continue
        try:
            new_id = create_manual(conn, data)
            results.append({"row": number, "result": "created", "id": new_id})
        except DuplicateOpportunity as exc:
            results.append({"row": number, "result": "duplicate", "id": exc.existing_id})
    return results
