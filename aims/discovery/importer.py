"""Import discovery feed runs into the local database. This is the local side of discovery.

Integrity rules (docs/DESIGN.md section 6):
- A feed is untrusted input. It is validated in full before anything is written; an invalid
  run is rejected whole.
- Each run is imported at most once (imported_feed_runs), in one transaction.
- Postings are matched on (source, source_key, source_posting_id), so repeated imports never
  duplicate. A discovered posting with the same URL as a manual entry adopts that entry.
- Imports write only source-owned columns. User state, notes and applications are never touched.
- A posting is marked 'not_seen' only when a check was successful AND complete and the posting
  was absent. Failed or partial checks never change listing status. Nothing is ever deleted.
- An older run never overwrites data from a newer one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Annotated, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from aims.db import transaction, utc_now
from aims.discovery import model, run
from aims.opportunities import url_key

STALE_AFTER_DAYS = 3


def Text(max_length: int, min_length: int = 0):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)]


def _utc(value: str) -> str:
    """Normalize any ISO timestamp to the app's 'YYYY-MM-DDTHH:MM:SSZ' form, so strings compare in time order."""
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PostingIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_posting_id: Text(200, 1)
    title: Text(300, 1)
    url: Text(2000, 1)
    location: Text(300) = ""
    opportunity_type: Literal["internship", "co-op", "government", "research", "full-time", "other"] = "internship"
    description: Annotated[str, StringConstraints(max_length=100_000)] = ""
    deadline: dt.date | None = None
    source_api_url: Text(2000) = ""
    raw_sha256: Text(64) = ""

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        if not re.match(r"^https?://[^\s/]+", v, re.I):
            raise ValueError("must be an http(s) URL")
        return v


class CheckIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: Literal["greenhouse", "lever"]
    source_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,100}$")]
    employer: Text(200, 1)
    status: Literal["ok", "failed"]
    complete: bool
    error: Annotated[str, StringConstraints(max_length=5000)] | None = None
    fetched_at: str
    total_on_board: int | None = None
    postings: Annotated[list[PostingIn], Field(max_length=5000)] = []

    @field_validator("fetched_at")
    @classmethod
    def _ts(cls, v: str) -> str:
        return _utc(v)


class FeedIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_: Literal["aims-feed"] = Field(alias="schema")
    version: Literal[1]
    run_id: Annotated[str, StringConstraints(pattern=r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")]
    origin: Literal["cloud", "local"]
    generated_at: str
    checks: Annotated[list[CheckIn], Field(max_length=500)]

    @field_validator("generated_at")
    @classmethod
    def _ts(cls, v: str) -> str:
        return _utc(v)


class FeedError(ValueError):
    pass


def _content_hash(p: PostingIn) -> str:
    fields = [p.title, p.url, p.location, p.opportunity_type, p.description, p.deadline.isoformat() if p.deadline else None]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def _snapshot(conn, opp_id: int, fetched_at: str, p: PostingIn) -> bool:
    digest = _content_hash(p)
    last = conn.execute("SELECT content_sha256 FROM opportunity_snapshots WHERE opportunity_id = ? ORDER BY id DESC LIMIT 1",
                        (opp_id,)).fetchone()
    if last and last[0] == digest:
        return False
    conn.execute("INSERT INTO opportunity_snapshots (opportunity_id, fetched_at, content_sha256, data_json) VALUES (?, ?, ?, ?)",
                 (opp_id, fetched_at, digest, p.model_dump_json()))
    return True


def _upsert(conn, check: CheckIn, p: PostingIn) -> str:
    """Returns 'new', 'adopted', 'updated', 'unchanged' or 'stale'."""
    at, now, key = check.fetched_at, utc_now(), url_key(p.url)
    deadline = p.deadline.isoformat() if p.deadline else None
    row = conn.execute(
        "SELECT id, last_seen_at FROM opportunities WHERE source = ? AND source_key = ? AND source_posting_id = ?",
        (check.source, check.source_key, p.source_posting_id)).fetchone()
    if row is None:
        manual = conn.execute("SELECT id FROM opportunities WHERE source = 'manual' AND url_key = ?", (key,)).fetchone()
        if manual is None:
            opp_id = conn.execute(
                """INSERT INTO opportunities (source, source_key, source_posting_id, employer, title, url, url_key, location,
                   opportunity_type, description, deadline, first_seen_at, last_seen_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (check.source, check.source_key, p.source_posting_id, check.employer, p.title, p.url, key, p.location,
                 p.opportunity_type, p.description, deadline, at, at, now, now)).lastrowid
            _snapshot(conn, opp_id, at, p)
            return "new"
        opp_id, outcome = manual["id"], "adopted"
        conn.execute("UPDATE opportunities SET source = ?, source_key = ?, source_posting_id = ? WHERE id = ?",
                     (check.source, check.source_key, p.source_posting_id, opp_id))
    else:
        if at < row["last_seen_at"]:
            return "stale"  # an older run must not overwrite newer data
        opp_id, outcome = row["id"], "updated"

    # Source-owned columns only. A deadline the source stops stating keeps its last known value,
    # and a user-closed listing stays closed.
    conn.execute(
        """UPDATE opportunities SET employer = ?, title = ?, url = ?, url_key = ?, location = ?, opportunity_type = ?,
           description = ?, deadline = COALESCE(?, deadline), last_seen_at = ?,
           listing_status = CASE WHEN listing_status = 'closed' THEN 'closed' ELSE 'active' END,
           missing_since = CASE WHEN listing_status = 'closed' THEN missing_since ELSE NULL END,
           updated_at = ? WHERE id = ?""",
        (check.employer, p.title, p.url, key, p.location, p.opportunity_type, p.description, deadline, at, now, opp_id))
    changed = _snapshot(conn, opp_id, at, p)
    return outcome if outcome == "adopted" or changed else "unchanged"


def _source_id(conn, check: CheckIn) -> int:
    conn.execute("INSERT OR IGNORE INTO sources (source, source_key, employer, first_seen_at) VALUES (?, ?, ?, ?)",
                 (check.source, check.source_key, check.employer, check.fetched_at))
    conn.execute("UPDATE sources SET employer = ? WHERE source = ? AND source_key = ?",
                 (check.employer, check.source, check.source_key))
    return conn.execute("SELECT id FROM sources WHERE source = ? AND source_key = ?",
                        (check.source, check.source_key)).fetchone()[0]


def import_feed(conn: sqlite3.Connection, raw: dict, origin: str | None = None) -> dict:
    try:
        feed = FeedIn.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise FeedError(f"Feed rejected (nothing imported): {'.'.join(map(str, first['loc']))}: {first['msg']}") from None
    origin = origin or feed.origin
    with transaction(conn):
        if conn.execute("SELECT 1 FROM imported_feed_runs WHERE run_id = ?", (feed.run_id,)).fetchone():
            return {"run_id": feed.run_id, "skipped": True}
        totals = {"new": 0, "adopted": 0, "updated": 0, "unchanged": 0, "stale": 0, "not_seen": 0, "failed_checks": 0}
        sources = []
        for check in feed.checks:
            source_id = _source_id(conn, check)
            counts = dict.fromkeys(totals, 0)
            if check.status == "ok":
                seen: set[str] = set()
                for p in check.postings:
                    if p.source_posting_id in seen:
                        continue  # a feed listing the same posting twice must not count it twice
                    seen.add(p.source_posting_id)
                    counts[_upsert(conn, check, p)] += 1
                if check.complete:
                    counts["not_seen"] = conn.execute(
                        f"""UPDATE opportunities SET listing_status = 'not_seen',
                            missing_since = COALESCE(missing_since, ?), updated_at = ?
                            WHERE source = ? AND source_key = ? AND listing_status = 'active' AND last_seen_at < ?
                            AND source_posting_id NOT IN ({','.join('?' * len(seen)) or "''"})""",
                        (check.fetched_at, utc_now(), check.source, check.source_key, check.fetched_at, *seen)).rowcount
            else:
                counts["failed_checks"] = 1
            conn.execute(
                """INSERT INTO source_checks (source_id, run_id, origin, checked_at, status, complete, total_on_board,
                   posting_count, new_count, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_id, feed.run_id, origin, check.fetched_at, check.status, int(check.status == "ok" and check.complete),
                 check.total_on_board, len(check.postings) if check.status == "ok" else 0, counts["new"], check.error))
            for k in totals:
                totals[k] += counts[k]
            sources.append({"source": check.source, "source_key": check.source_key, "status": check.status,
                            "complete": check.complete, **counts})
        summary = {"run_id": feed.run_id, "origin": origin, "generated_at": feed.generated_at, **totals}
        conn.execute("INSERT INTO imported_feed_runs (run_id, origin, generated_at, imported_at, summary_json) VALUES (?, ?, ?, ?, ?)",
                     (feed.run_id, origin, feed.generated_at, utc_now(), json.dumps(summary)))
    return {**summary, "skipped": False, "sources": sources}


# ---------------------------------------------------------------- local run and remote sync

def run_here(conn: sqlite3.Connection, http_get: model.HttpGet = model.http_get_json) -> dict:
    """'Run discovery here': check every source from this computer and import the result."""
    return import_feed(conn, run.run(run.load_sources(), "local", http_get))


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": model.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return model._read_bounded(resp, time.monotonic() + 180, url)
    except urllib.error.HTTPError as exc:
        raise FeedError(f"HTTP {exc.code} from {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FeedError(f"Could not reach {url}: {getattr(exc, 'reason', exc)}") from None


_RUN_FILE = re.compile(r"^runs/\d{8}T\d{6}Z-[0-9a-f]{6}\.json$")


def check_feed_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not re.match(r"^https://[^\s/]+(/\S*)?$", url, re.I) and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/", url + "/"):
        raise FeedError("The feed URL must start with https://")
    return url


def sync_remote(conn: sqlite3.Connection, feed_url: str, get: Callable[[str], bytes] = fetch_bytes) -> dict:
    """Import every run in the cloud feed that hasn't been imported yet, oldest first."""
    base = check_feed_url(feed_url)
    try:
        index = json.loads(get(f"{base}/latest.json"))
        entries = sorted(index["runs"], key=lambda e: e["generated_at"])
    except (ValueError, KeyError, TypeError) as exc:
        raise FeedError(f"The feed index at {base}/latest.json is not readable: {exc}") from None
    imported, errors = [], []
    for e in entries:
        run_id, file = str(e.get("run_id", "")), str(e.get("file", ""))
        if conn.execute("SELECT 1 FROM imported_feed_runs WHERE run_id = ?", (run_id,)).fetchone():
            continue
        if not _RUN_FILE.match(file):
            errors.append(f"{run_id}: unexpected file name {file!r}")
            continue
        try:
            data = get(f"{base}/{file}")
            if hashlib.sha256(data).hexdigest() != e.get("sha256"):
                raise FeedError("checksum does not match the index (corrupted or tampered download)")
            imported.append(import_feed(conn, json.loads(data), origin="cloud"))
        except (FeedError, ValueError) as exc:
            errors.append(f"{run_id}: {exc}")  # other runs still import; this one is retried next sync
    return {"imported": [s for s in imported if not s.get("skipped")], "errors": errors,
            "new": sum(s.get("new", 0) for s in imported)}


# ---------------------------------------------------------------- health

def health(conn: sqlite3.Connection, now: str | None = None) -> dict:
    now = now or utc_now()
    stale_before = (dt.datetime.fromisoformat(now.replace("Z", "+00:00")) - dt.timedelta(days=STALE_AFTER_DAYS)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    configured = {(s["source"], s["key"]): s for s in run.load_sources()}
    rows = {(r["source"], r["source_key"]): dict(r) for r in conn.execute("SELECT * FROM sources")}
    out = []
    for key in sorted(set(configured) | set(rows), key=lambda k: (configured.get(k) or rows[k])["employer"].lower()):
        src = rows.get(key)
        last = good = None
        counts = {"active": 0, "not_seen": 0}
        if src:
            last = conn.execute("SELECT * FROM source_checks WHERE source_id = ? ORDER BY checked_at DESC, id DESC LIMIT 1",
                                (src["id"],)).fetchone()
            good = conn.execute("SELECT MAX(checked_at) FROM source_checks WHERE source_id = ? AND status = 'ok' AND complete = 1",
                                (src["id"],)).fetchone()[0]
            for r in conn.execute("SELECT listing_status, COUNT(*) FROM opportunities WHERE source = ? AND source_key = ? "
                                  "GROUP BY listing_status", key):
                counts[r[0]] = r[1]
        state = ("never checked" if last is None else "failing" if last["status"] == "failed"
                 else "partial" if not last["complete"] else "ok")
        if state == "ok" and (good is None or good < stale_before):
            state = "stale"
        out.append({
            "source": key[0], "source_key": key[1],
            "employer": (configured.get(key) or src)["employer"], "configured": key in configured,
            "state": state, "last_checked_at": last["checked_at"] if last else None,
            "last_complete_at": good, "last_error": last["error"] if last else None,
            "last_posting_count": last["posting_count"] if last else None, **counts,
        })
    last_cloud = conn.execute("SELECT MAX(generated_at) FROM imported_feed_runs WHERE origin = 'cloud'").fetchone()[0]
    return {"sources": out, "last_cloud_run_at": last_cloud,
            "runs_imported": conn.execute("SELECT COUNT(*) FROM imported_feed_runs").fetchone()[0]}
