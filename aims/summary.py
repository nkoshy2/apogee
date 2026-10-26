"""Counts behind the sentence summaries and navigation badges ("You have 2 deadlines this week...")."""

from __future__ import annotations

import datetime as dt
import sqlite3

from aims import applications, opportunities
from aims.settings_store import Settings

WAITING_STATUSES = ("submitted", "assessment", "interviewing")
SOON_DAYS = 7


def summarize(conn: sqlite3.Connection, settings: Settings, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    soon = (today + dt.timedelta(days=SOON_DAYS)).isoformat()
    now = today.isoformat()

    opps = opportunities.browse(conn, opportunities.Filters(state="active"), settings)
    # A deadline matters only while nothing has been submitted yet.
    open_deadlines = [o["deadline"] for o in opps
                      if o["deadline"] and o["application_status"] in (None, "preparing")]
    apps = applications.list_applications(conn, "active")
    return {
        "opportunities": {
            "active": len(opps),
            "eligible": sum(o["eligibility"] == "eligible" for o in opps),
            "uncertain": sum(o["eligibility"] == "uncertain" for o in opps),
            "ineligible": sum(o["eligibility"] == "ineligible" for o in opps),
            "due_soon": sum(now <= d <= soon for d in open_deadlines),
            "overdue": sum(d < now for d in open_deadlines),
        },
        "applications": {
            "active": len(apps),
            "preparing": sum(a["status"] == "preparing" for a in apps),
            "waiting": sum(a["status"] in WAITING_STATUSES for a in apps),
            "offers": sum(a["status"] == "offer" for a in apps),
            "due_soon": sum(a["status"] == "preparing" and a["deadline"] is not None and now <= a["deadline"] <= soon for a in apps),
        },
    }
