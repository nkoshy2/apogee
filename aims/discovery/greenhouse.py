"""Greenhouse Job Board API adapter (public, unauthenticated).

1. GET /v1/boards/{key}/jobs lists every job (without descriptions). The check is complete
   only if the number of jobs returned equals the board's reported meta.total.
2. Student roles are kept, and each one's full record is read from /jobs/{id}. A failed
   detail read makes the check incomplete instead of failing it, so that posting is not
   treated as gone.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from aims.discovery.model import (
    LOCATION_LIMIT, FetchError, HttpGet, Posting, SourceCheck, classify, clean_text, html_to_text, http_get_json,
    is_student_role, iso_date, raw_hash,
)

API = "https://boards-api.greenhouse.io/v1/boards"
DETAIL_WORKERS = 4


def _posting(key: str, job: dict) -> Posting:
    labels = [d.get("name", "") for d in job.get("departments") or []]
    title = clean_text(job.get("title"), 300)
    url = str(job.get("absolute_url") or "")
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError(f"job {job.get('id')} has no usable URL")
    return Posting(
        source_posting_id=str(job["id"]),
        title=title,
        url=url[:2000],
        location=clean_text((job.get("location") or {}).get("name"), LOCATION_LIMIT),
        opportunity_type=classify(title, labels),
        description=html_to_text(str(job.get("content") or "")),
        deadline=iso_date(job.get("application_deadline")),
        updated_at=job.get("updated_at"),
        source_api_url=f"{API}/{key}/jobs/{job['id']}",
        raw_sha256=raw_hash(job),
    )


def fetch(key: str, employer: str, http_get: HttpGet = http_get_json) -> SourceCheck:
    check = SourceCheck(source="greenhouse", source_key=key, employer=employer)
    try:
        listing = http_get(f"{API}/{key}/jobs")
        jobs = listing["jobs"]
        if not isinstance(jobs, list):
            raise TypeError("jobs is not a list")
    except (FetchError, KeyError, TypeError) as exc:
        check.status, check.error = "failed", f"Could not list jobs: {exc}"
        return check

    total = (listing.get("meta") or {}).get("total")
    check.total_on_board = len(jobs)
    complete = isinstance(total, int) and total == len(jobs)
    problems = [] if complete else [f"board reports {total} jobs but returned {len(jobs)}"]

    wanted = [j for j in jobs if isinstance(j, dict) and "id" in j and is_student_role(str(j.get("title", "")))]

    def detail(job: dict):
        try:
            return _posting(key, http_get(f"{API}/{key}/jobs/{job['id']}")), None
        except (FetchError, KeyError, TypeError, ValueError) as exc:
            return None, f"job {job['id']}: {exc}"

    with ThreadPoolExecutor(DETAIL_WORKERS) as pool:
        for posting, problem in pool.map(detail, wanted):
            if posting:
                check.postings.append(posting)
            else:
                problems.append(problem)

    check.complete = not problems
    check.error = "; ".join(problems[:5]) or None
    return check
