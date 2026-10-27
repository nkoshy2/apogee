"""Measure discovery completeness and field accuracy against an independent reference.

    python scripts/validate_discovery.py [--out report.md] [--db path/to/aims.db] [--sample 20]

Reference: the employers' public HTML job boards (job-boards.greenhouse.io/<key>, jobs.lever.co/<key>).
They are server-rendered pages, separate from the JSON APIs the adapters use, and parsed here without
any adapter code. The run fetches both at nearly the same moment; a posting that appears or closes in
between can cause a small, explainable mismatch.

Spec targets (section 6): completeness >= 95%, required-field accuracy >= 99%.
With --db, it also reports the scheduled-run success rate from imported cloud runs (target >= 95%).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aims.discovery import model, run  # noqa: E402

UA = {"User-Agent": model.USER_AGENT}
PAGE_DELAY_S = 0.4  # be polite to the public boards
# Broader than the scope filter: titles a person might still consider student roles.
MAYBE_STUDENT = re.compile(r"\b(summer|fall|spring|winter)\s+20\d\d\b|university|new grad|graduate program|fellow(ship)?|"
                           r"rotation|early career|emerging talent|entry.level|trainee|apprentic", re.I)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def norm(s: str) -> str:
    """Visible text of an HTML fragment (tags such as a "New" badge removed, entities decoded)."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def key_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------------------------------------------------------------- independent reference parsers

_GH_ROW = re.compile(r'<a href="https://job-boards\.greenhouse\.io/[^/"]+/jobs/(\d+)[^"]*"[^>]*>\s*'
                     r'<p class="body body--medium">(.*?)</p>\s*(?:<p class="body body__secondary body--metadata">(.*?)</p>)?', re.S)


def greenhouse_api_reference(key: str) -> dict[str, dict]:
    """Fallback for boards hosted on the employer's own site: Greenhouse's bulk endpoint.
    Same API family as the adapter but a different endpoint and no adapter code, so it's a weaker,
    clearly labeled reference."""
    req = urllib.request.Request(f"https://boards-api.greenhouse.io/v1/boards/{key}/jobs?content=true", headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return {str(j["id"]): {"title": norm(j["title"]), "location": norm((j.get("location") or {}).get("name"))} for j in data["jobs"]}


def greenhouse_reference(key: str) -> dict[str, dict]:
    jobs: dict[str, dict] = {}
    for page in range(1, 200):
        rows = _GH_ROW.findall(fetch(f"https://job-boards.greenhouse.io/{key}?page={page}"))
        # The title cell can end with a visual badge ("<span ...>New</span>") that isn't part of the title.
        new = {jid: {"title": norm(t.split("<span", 1)[0]), "location": norm(loc)} for jid, t, loc in rows if jid not in jobs}
        if not new:
            break
        jobs.update(new)
        time.sleep(PAGE_DELAY_S)
    return jobs


_LV_POSTING = re.compile(r'<div class="posting" data-qa-posting-id="([0-9a-f-]{36})".*?(?=<div class="posting" data-qa-posting-id=|$)', re.S)


def lever_reference(key: str) -> dict[str, dict]:
    page = fetch(f"https://jobs.lever.co/{key}")
    jobs = {}
    for m in _LV_POSTING.finditer(page):
        block = m.group(0)
        title = re.search(r'data-qa="posting-name">(.*?)<', block)
        loc = re.search(r'class="sort-by-location[^"]*">(.*?)<', block)
        commit = re.search(r'class="sort-by-commitment[^"]*">(.*?)<', block)
        team = re.search(r'class="sort-by-team[^"]*">(.*?)<', block)
        jobs[m.group(1)] = {"title": norm(title.group(1)) if title else "", "location": norm(loc.group(1)) if loc else "",
                            "labels": [norm(x.group(1)) for x in (commit, team) if x]}
    return jobs


# ---------------------------------------------------------------- comparison

def compare(spec: dict, sample_descriptions: int) -> dict:
    t0 = time.monotonic()
    check = run.check_source(spec)
    reference_kind = "public HTML board"
    if spec["source"] == "greenhouse":
        try:
            reference = greenhouse_reference(spec["key"])
        except urllib.error.URLError:
            reference = {}
        if not reference:  # board hosted on the employer's own site, or the page is down
            reference, reference_kind = greenhouse_api_reference(spec["key"]), "Greenhouse bulk API (weaker)"
    else:
        reference = lever_reference(spec["key"])
    ref_students = {jid: r for jid, r in reference.items() if model.is_student_role(r["title"], r.get("labels", []))}
    got = {p.source_posting_id: p for p in check.postings}

    found = set(ref_students) & set(got)
    missed = sorted(set(ref_students) - set(got))
    extra = sorted(set(got) - set(ref_students))
    field_checks = field_errors = 0
    errors = []
    for jid in sorted(found):
        p, r = got[jid], ref_students[jid]
        for field, ours, theirs in (("title", p.title, r["title"]), ("location", p.location, r["location"])):
            if spec["source"] == "lever" and field == "location" and theirs and theirs in ours:
                theirs = ours  # Lever's page shows the primary location; the API lists all of them
            field_checks += 1
            if key_text(ours) != key_text(theirs):
                field_errors += 1
                errors.append(f"{jid} {field}: feed {ours!r} vs board {theirs!r}")
        field_checks += 1  # the URL must lead to this posting
        if jid not in p.url:
            field_errors += 1
            errors.append(f"{jid} url: {p.url}")

    desc_checked = desc_ok = 0
    for jid in sorted(found)[:sample_descriptions]:
        page_text = key_text(norm(fetch(got[jid].url)))
        snippet = key_text(got[jid].description)[:80]
        desc_checked += 1
        if snippet and snippet in page_text:
            desc_ok += 1
        else:
            errors.append(f"{jid} description: opening text not found on {got[jid].url}")
        time.sleep(PAGE_DELAY_S)

    maybe = sorted({r["title"] for jid, r in reference.items() if jid not in ref_students and MAYBE_STUDENT.search(r["title"])})
    return {
        "source": spec["source"], "key": spec["key"], "employer": spec["employer"], "reference": reference_kind,
        "status": check.status, "complete": check.complete, "error": check.error,
        "board_total": len(reference), "api_total": check.total_on_board,
        "reference_students": len(ref_students), "found": len(found), "missed": missed, "extra": extra,
        "field_checks": field_checks, "field_errors": field_errors,
        "desc_checked": desc_checked, "desc_ok": desc_ok, "errors": errors[:10], "maybe_student": maybe,
        "seconds": round(time.monotonic() - t0, 1),
    }


def scheduled_runs(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    runs = conn.execute("SELECT run_id, generated_at, summary_json FROM imported_feed_runs WHERE origin = 'cloud' ORDER BY generated_at").fetchall()
    if not runs:
        return "No cloud runs imported yet (the scheduled workflow isn't running)."
    ok = sum(json.loads(s)["failed_checks"] == 0 for _, _, s in runs)
    first, last = runs[0][1], runs[-1][1]
    days = (datetime.fromisoformat(last.replace("Z", "+00:00")) - datetime.fromisoformat(first.replace("Z", "+00:00"))).days
    return (f"{len(runs)} cloud runs over {days} days ({first} to {last}); {ok} with every source succeeding "
            f"({ok / len(runs):.1%}; target 95% over at least 14 days).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--sample", type=int, default=3, help="descriptions to spot-check per source")
    args = ap.parse_args()

    results = []
    for spec in run.load_sources():
        try:
            r = compare(spec, args.sample)
        except Exception as exc:  # report a reference failure instead of hiding the source
            r = {"source": spec["source"], "key": spec["key"], "employer": spec["employer"], "fatal": f"{type(exc).__name__}: {exc}"}
        results.append(r)
        print(f"{spec['employer']:24} " + (r.get("fatal") or
              f"found {r['found']}/{r['reference_students']}, fields {r['field_checks'] - r['field_errors']}/{r['field_checks']}, "
              f"descriptions {r['desc_ok']}/{r['desc_checked']} ({r['seconds']} s)"), flush=True)

    ok = [r for r in results if "fatal" not in r]
    ref = sum(r["reference_students"] for r in ok)
    found = sum(r["found"] for r in ok)
    fchk = sum(r["field_checks"] for r in ok) + sum(r["desc_checked"] for r in ok)
    ferr = sum(r["field_errors"] for r in ok) + sum(r["desc_checked"] - r["desc_ok"] for r in ok)
    completeness = found / ref if ref else 1.0
    accuracy = 1 - ferr / fchk if fchk else 1.0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Discovery validation, {now}", "",
             "Reference: the employers' public HTML job boards, parsed independently of the adapters.", "",
             "| Measure | Result | Target |", "|---|---|---|",
             f"| Completeness (student roles on the boards that the feed found) | **{completeness:.1%}** ({found}/{ref}) | ≥ 95% |",
             f"| Field accuracy (title, location, link, description spot checks) | **{accuracy:.1%}** ({fchk - ferr}/{fchk}) | ≥ 99% |",
             f"| Sources checked | {len(ok)}/{len(results)} | all |"]
    if args.db:
        lines += ["", "**Scheduled runs:** " + scheduled_runs(args.db)]
    lines += ["", "## Per source", "",
              "| Employer | Board | Reference | Jobs (reference / API) | Student roles found | Fields correct | Descriptions | Notes |",
              "|---|---|---|---|---|---|---|---|"]
    for r in results:
        if "fatal" in r:
            lines.append(f"| {r['employer']} | {r['source']}/{r['key']} | | | | | | Reference failed: {r['fatal']} |")
            continue
        notes = []
        if r["missed"]:
            notes.append(f"missed {', '.join(r['missed'][:5])}")
        if r["extra"]:
            notes.append(f"{len(r['extra'])} in feed but not on board")
        if r["board_total"] != r["api_total"]:
            notes.append("board and API totals differ")
        lines.append(f"| {r['employer']} | {r['source']}/{r['key']} | {r['reference']} | {r['board_total']} / {r['api_total']} | "
                     f"{r['found']}/{r['reference_students']} | {r['field_checks'] - r['field_errors']}/{r['field_checks']} | "
                     f"{r['desc_ok']}/{r['desc_checked']} | {'; '.join(notes)} |")
    errs = [e for r in ok for e in r["errors"]]
    if errs:
        lines += ["", "## Mismatches", ""] + [f"- {e}" for e in errs]
    maybe = [(r["employer"], t) for r in ok for t in r["maybe_student"]]
    lines += ["", "## Possibly in scope but outside the student-role filter", "",
              "Titles that look student-related but don't contain intern, internship, co-op or student. "
              "Review these to decide whether the filter should be wider.", ""]
    lines += [f"- {e}: {t}" for e, t in maybe] or ["- None."]
    report = "\n".join(lines) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
    print(f"\nCompleteness {completeness:.1%}, accuracy {accuracy:.1%}" + (f" -> {args.out}" if args.out else ""))
    return 0 if completeness >= 0.95 and accuracy >= 0.99 else 1


if __name__ == "__main__":
    sys.exit(main())
