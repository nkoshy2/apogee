"""Lever Postings API adapter (public, unauthenticated).

GET /v0/postings/{key}?mode=json returns every published posting in one response, so a
successful, well-formed response is a complete check. Records that can't be read make the
check incomplete rather than failed.
"""

from __future__ import annotations

from aims.discovery.model import (
    FetchError, HttpGet, Posting, SourceCheck, classify, clean_text, html_to_text, http_get_json,
    is_student_role, raw_hash,
)

API = "https://api.lever.co/v0/postings"


def _description(p: dict) -> str:
    parts = [str(p.get("descriptionPlain") or "")]
    for block in p.get("lists") or []:
        parts.append(f"{block.get('text', '')}\n{html_to_text(str(block.get('content') or ''))}")
    parts.append(str(p.get("additionalPlain") or ""))
    return "\n\n".join(x.strip() for x in parts if x and x.strip())[:100_000]


def _posting(key: str, p: dict) -> Posting:
    cats = p.get("categories") or {}
    labels = [str(cats.get(k) or "") for k in ("commitment", "department", "team")]
    title = clean_text(p.get("text"), 300)
    url = str(p.get("hostedUrl") or "")
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError(f"posting {p.get('id')} has no usable URL")
    locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
    return Posting(
        source_posting_id=str(p["id"]),
        title=title,
        url=url[:2000],
        location=clean_text(" / ".join(map(str, locations)), 300),
        opportunity_type=classify(title, labels),
        description=_description(p),
        source_api_url=f"{API}/{key}/{p['id']}",
        raw_sha256=raw_hash(p),
    )


def fetch(key: str, employer: str, http_get: HttpGet = http_get_json) -> SourceCheck:
    check = SourceCheck(source="lever", source_key=key, employer=employer)
    try:
        postings = http_get(f"{API}/{key}?mode=json")
        if not isinstance(postings, list):
            raise TypeError("response is not a list of postings")
    except (FetchError, TypeError) as exc:
        check.status, check.error = "failed", f"Could not list postings: {exc}"
        return check

    check.total_on_board = len(postings)
    problems = []
    for p in postings:
        if not isinstance(p, dict) or "id" not in p:
            problems.append("unreadable posting record")
            continue
        cats = p.get("categories") or {}
        if not is_student_role(str(p.get("text", "")), [str(cats.get("commitment") or ""), str(cats.get("team") or "")]):
            continue
        try:
            check.postings.append(_posting(key, p))
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"posting {p.get('id')}: {exc}")
    check.complete = not problems
    check.error = "; ".join(problems[:5]) or None
    return check
