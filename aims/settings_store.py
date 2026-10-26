"""User preferences and profile facts, stored one key per row as JSON in the settings table."""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from aims.db import transaction, utc_now

Item = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ItemList = Annotated[list[Item], Field(max_length=200)]


class Profile(BaseModel):
    """Facts used for eligibility and relevance. None means "not specified", never "no"."""

    model_config = ConfigDict(extra="forbid")

    us_citizen: bool | None = None
    us_person: bool | None = None  # citizen or permanent resident (ITAR / export control)
    needs_sponsorship: bool | None = None
    graduation: Annotated[str, StringConstraints(pattern=r"^20\d\d-(0[1-9]|1[0-2])$")] | None = None  # YYYY-MM
    degree_level: Literal["bachelors", "masters", "phd"] | None = None
    gpa: Annotated[float, Field(ge=0, le=4.0)] | None = None
    # Interim: Phase 3 adds verified skills from the experience database.
    skills: ItemList = []

    @model_validator(mode="after")
    def _consistent(self) -> "Profile":
        if self.us_citizen and self.us_person is False:
            raise ValueError("A U.S. citizen is also a U.S. person")
        return self

    @property
    def is_us_person(self) -> bool | None:
        return True if self.us_citizen else self.us_person


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_employers: ItemList = []
    preferred_locations: ItemList = []
    disciplines: ItemList = []
    internship_terms: ItemList = []
    backup_mirror_dir: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""
    profile: Profile = Profile()


def load(conn: sqlite3.Connection) -> Settings:
    known = Settings.model_fields.keys()
    stored = {
        row["key"]: json.loads(row["value_json"])
        for row in conn.execute("SELECT key, value_json FROM settings")
        if row["key"] in known  # unknown keys are left untouched in the DB, never deleted
    }
    return Settings.model_validate(stored)


def save(conn: sqlite3.Connection, settings: Settings) -> None:
    now = utc_now()
    with transaction(conn):
        for key, value in settings.model_dump().items():
            conn.execute(
                "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                (key, json.dumps(value), now),
            )
