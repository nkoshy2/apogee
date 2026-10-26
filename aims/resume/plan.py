"""Choose and order resume content for a job. Deterministic; uses only approved statements.

Scoring reuses the ranking vocabulary: a statement earns 10 points per skill or discipline
tag it shares with the posting, plus up to 5 points for other shared significant words.

Ordering rules:
- Education first and Skills last; the sections in between are ordered by relevance.
- Education, employment and research entries stay reverse-chronological (resume convention);
  projects, leadership and achievements are ordered by relevance.
- Statements within an entry are ordered by relevance. Protected statements and entries are
  always included. Each entry starts with its best MAX_BULLETS statements; the user can
  include or exclude any approved statement, and the generator may drop low scorers to fit.
"""

from __future__ import annotations

import json
import re
import sqlite3

from aims import ranking

SECTION_TITLES = {
    "education": "Education", "employment": "Experience", "research": "Research",
    "project": "Projects", "leadership": "Leadership", "achievement": "Honors & Awards",
}
MIDDLE_SECTIONS = ("employment", "research", "project", "leadership", "achievement")
CHRONOLOGICAL = {"education", "employment", "research"}
HEADER_ONLY_KINDS = {"education", "achievement"}  # shown even without bullet statements
MAX_BULLETS = 4
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_STOPWORDS = set(
    "with that this from have will your their they about into using used work worked working team teams "
    "support supported including design designed engineering engineer intern internship experience "
    "skills ability strong able also more than other such well".split()
)


def _month(value: str | None) -> str:
    if not value:
        return ""
    year, month = value.split("-")
    return f"{_MONTHS[int(month) - 1]} {year}"


def format_dates(e: dict) -> str:
    start, end = _month(e["start_date"]), _month(e["end_date"])
    if e["kind"] == "education":
        # For a current program the end date is the expected graduation the user entered.
        if end:
            return f"Expected {end}" if e["is_current"] else end
        return f"{start} – Present" if start and e["is_current"] else start
    if e["is_current"]:
        return f"{start} – Present" if start else "Present"
    if start and end:
        return start if start == end else f"{start} – {end}"
    return start or end


def _significant_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9+#]{3,}", text.lower()) if w not in _STOPWORDS}


def _tags(text: str, extra: list[str] = ()) -> set[str]:
    skills, disciplines = ranking.load_vocabulary()
    tags = set(ranking._tags_in(text, skills)) | set(ranking._tags_in(text, disciplines))
    for t in extra:
        mapped = ranking._tags_in(t, skills) + ranking._tags_in(t, disciplines)
        tags.update(mapped or [t.lower()])
    return tags


class Job:
    def __init__(self, opportunity: dict | None):
        text = f"{opportunity['title']}\n{opportunity.get('description') or ''}" if opportunity else ""
        self.present = bool(opportunity)
        self.tags = _tags(text)
        self.words = _significant_words(text)
        self.lowered = text.lower()

    def score(self, text: str, extra_tags: list[str] = ()) -> tuple[int, list[str]]:
        if not self.present:
            return 0, []
        own = _tags(text, extra_tags)
        matched = sorted(own & self.tags) + sorted(t for t in own - self.tags if t.islower() and t in self.lowered)
        shared = len(_significant_words(text) & self.words)
        return 10 * len(matched) + min(shared, 5), matched


def build_plan(conn: sqlite3.Connection, opportunity: dict | None,
               include_ids: set[int] = frozenset(), exclude_ids: set[int] = frozenset()) -> dict:
    job = Job(opportunity)
    warnings: list[str] = []
    entries_by_kind: dict[str, list[dict]] = {k: [] for k in SECTION_TITLES}

    experiences = [dict(r) for r in conn.execute("SELECT * FROM experiences")]
    statements: dict[int, list[dict]] = {}
    for s in conn.execute("SELECT * FROM statements WHERE status = 'approved' ORDER BY sort_order, id"):
        statements.setdefault(s["experience_id"], []).append(dict(s))
    approved_ids = {s["id"] for group in statements.values() for s in group}
    for sid in sorted(include_ids - approved_ids):
        warnings.append(f"Statement {sid} is not approved, so it can't be included.")

    for e in experiences:
        scored = []
        for s in statements.get(e["id"], []):
            score, matched = job.score(s["text"], json.loads(s["tags_json"]))
            scored.append({"id": s["id"], "text": s["text"], "score": score, "matched": matched,
                           "protected": bool(s["protected"]), "sort_order": s["sort_order"]})
        scored.sort(key=lambda s: (-s["score"], s["sort_order"], s["id"]))

        kept = 0
        for s in scored:
            if s["protected"]:
                s["included"], s["reason"] = True, "protected"
                if s["id"] in exclude_ids:
                    warnings.append(f"“{s['text'][:50]}” is protected, so it stays on the resume.")
            elif s["id"] in exclude_ids:
                s["included"], s["reason"] = False, "excluded by you"
            elif s["id"] in include_ids:
                s["included"], s["reason"] = True, "included by you"
            elif kept < MAX_BULLETS:
                s["included"], s["reason"] = True, "relevant" if s["score"] else "best available"
            else:
                s["included"], s["reason"] = False, f"beyond the top {MAX_BULLETS} for this entry"
            kept += s["included"] and not s["protected"]
        header_score, header_matched = job.score(f"{e['title']} {e['organization']}")
        entry = {
            "experience_id": e["id"], "kind": e["kind"], "title": e["title"], "organization": e["organization"],
            "location": e["location"], "dates": format_dates(e), "protected": bool(e["protected"]),
            "sort_key": (e["is_current"], e["end_date"] or e["start_date"] or ""),
            "score": header_score // 2 + sum(sorted((s["score"] for s in scored), reverse=True)[:3]),
            "matched": header_matched, "statements": scored,
        }
        entry["included"] = (entry["protected"] or e["kind"] in HEADER_ONLY_KINDS
                             or any(s["included"] for s in scored))
        if not scored and e["kind"] not in HEADER_ONLY_KINDS and not entry["protected"]:
            warnings.append(f"{e['title']} has no approved statements, so it is left out.")
        entries_by_kind[e["kind"]].append(entry)

    sections = []
    for kind, entries in entries_by_kind.items():
        if not entries:
            continue
        entries.sort(key=lambda x: x["sort_key"], reverse=True)  # most recent first
        if kind not in CHRONOLOGICAL:
            entries.sort(key=lambda x: -x["score"])  # stable: recency breaks ties
        for x in entries:
            del x["sort_key"]
        sections.append({"kind": kind, "title": SECTION_TITLES[kind], "entries": entries,
                         "score": sum(x["score"] for x in entries if x["included"])})
    middle = sorted((s for s in sections if s["kind"] in MIDDLE_SECTIONS),
                    key=lambda s: (-s["score"], MIDDLE_SECTIONS.index(s["kind"])))
    ordered = [s for s in sections if s["kind"] == "education"] + middle

    return {
        "opportunity": ({k: opportunity[k] for k in ("id", "title", "employer", "url")} if opportunity else None),
        "job_tags": sorted(job.tags),
        "sections": ordered,
        "skills": _skills(conn, job),
        "warnings": warnings,
    }


def _skills(conn: sqlite3.Connection, job: Job) -> list[dict]:
    """All skills, grouped by category; skills the posting asks for come first in each group."""
    groups: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT name, category FROM skills ORDER BY name COLLATE NOCASE"):
        relevant = bool(job.present and (_tags(r["name"]) & job.tags or r["name"].lower() in job.lowered))
        groups.setdefault(r["category"], []).append({"name": r["name"], "relevant": relevant})
    lines = []
    for category, items in groups.items():
        items.sort(key=lambda i: not i["relevant"])  # stable: alphabetical within each group
        lines.append({"category": category, "items": items, "relevant": sum(i["relevant"] for i in items)})
    lines.sort(key=lambda l: (-l["relevant"], l["category"].lower()))
    return lines


def content_from_plan(plan: dict, header: dict) -> dict:
    """The snapshot that is rendered and stored: only included content, in final order."""
    sections = []
    for section in plan["sections"]:
        entries = [
            {"experience_id": e["experience_id"], "title": e["title"], "organization": e["organization"],
             "location": e["location"], "dates": e["dates"],
             "items": [{"statement_id": s["id"], "text": s["text"]} for s in e["statements"] if s["included"]]}
            for e in section["entries"] if e["included"]
        ]
        if entries:
            sections.append({"kind": section["kind"], "title": section["title"], "entries": entries})
    if plan["skills"]:
        sections.append({"kind": "skills", "title": "Skills",
                         "lines": [{"category": l["category"], "items": [i["name"] for i in l["items"]]}
                                   for l in plan["skills"]]})
    return {"header": header, "sections": sections}


def included_statements(plan: dict) -> list[dict]:
    return [s for sec in plan["sections"] for e in sec["entries"] if e["included"]
            for s in e["statements"] if s["included"]]
