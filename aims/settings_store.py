"""User preferences, stored one key per row as JSON in the settings table."""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from aims.db import transaction, utc_now

Item = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ItemList = Annotated[list[Item], Field(max_length=200)]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_employers: ItemList = []
    preferred_locations: ItemList = []
    disciplines: ItemList = []
    internship_terms: ItemList = []
    backup_mirror_dir: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


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
