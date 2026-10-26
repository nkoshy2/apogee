"""Opportunity storage, URL normalization, and duplicate detection.

Phase 1 covers manual entry and listing. Imports (Phase 4) must only write the
source-owned columns listed in migrations/0001_initial.sql.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from aims.db import transaction, utc_now

OpportunityType = Literal["internship", "co-op", "government", "research", "full-time", "other"]

# Query parameters that only track how a link was shared, not which posting it is.
# Kept deliberately narrow: wrongly merging two postings is worse than missing a duplicate.
_TRACKING_PARAMS = {"gh_src", "lever-source", "lever-origin"}

_LIST_COLUMNS = (
    "id, source, employer, title, url, location, opportunity_type, deadline, "
    "first_seen_at, last_seen_at, listing_status, user_state"
)


def Text(max_length: int, min_length: int = 0):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)]


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
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http:// or https:// link")
        return value


class DuplicateOpportunity(Exception):
    def __init__(self, existing_id: int):
        super().__init__(f"An opportunity with this URL already exists (id {existing_id})")
        self.existing_id = existing_id


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


def create_manual(conn: sqlite3.Connection, data: OpportunityIn) -> int:
    key = url_key(data.url)
    now = utc_now()
    with transaction(conn):
        existing = conn.execute("SELECT id FROM opportunities WHERE url_key = ?", (key,)).fetchone()
        if existing:
            raise DuplicateOpportunity(existing["id"])
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


def list_opportunities(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"SELECT {_LIST_COLUMNS} FROM opportunities ORDER BY first_seen_at DESC, id DESC")
    return [dict(r) for r in rows]


def get_opportunity(conn: sqlite3.Connection, opportunity_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
    return dict(row) if row else None
