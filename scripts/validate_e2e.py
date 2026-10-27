"""End-to-end validation of the MVP workflow (spec section 7) against a real Apogee server.

    python scripts/validate_e2e.py [--out report.md] [--count 12] [--keep]

Starts `python -m aims.server` on a spare port with a throwaway data folder, then drives it over HTTP
exactly like the browser does:

  1. discover live postings (Greenhouse/Lever) and import two by hand
  2. confirm they're stored once, appear in the list, and carry eligibility and relevance explanations
  3. save or dismiss them
  4. generate a tailored resume for each selected opportunity from verified experience
  5. check the PDF and LaTeX files, and that the posting link is usable
  6. create and update an application record with the resume attached
  7. confirm everything survives a repeated import, a server restart, and a backup restore

The profile is a clearly fictional sample student. Your real data folder is never touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Fail(Exception):
    pass


class Server:
    def __init__(self, data_dir: Path, port: int):
        self.data_dir, self.port, self.proc = data_dir, port, None
        self.base = f"http://127.0.0.1:{port}"

    def start(self) -> float:
        t0 = time.monotonic()
        env = {**os.environ, "AIMS_DATA_DIR": str(self.data_dir), "AIMS_PORT": str(self.port)}
        log = open(self.data_dir.parent / "server.log", "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "aims.server"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                if self.call("GET", "/api/health")["app"] == "aims":
                    return time.monotonic() - t0
            except (OSError, Fail):
                time.sleep(0.2)
        raise Fail("server did not start")

    def stop(self) -> None:
        try:
            self.call("POST", "/api/shutdown")
        except (OSError, Fail):
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def call(self, method: str, path: str, body=None, raw: bytes | None = None, expect=(200, 201, 204)):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("X-AIMS-Request", "1")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _opener.open(req, timeout=300) as r:
                status, payload, ctype = r.status, r.read(), r.headers.get("content-type", "")
        except urllib.error.HTTPError as e:
            status, payload, ctype = e.code, e.read(), e.headers.get("content-type", "")
        if status not in expect:
            raise Fail(f"{method} {path} -> {status}: {payload[:300]!r}")
        if "json" in ctype:
            return json.loads(payload) if payload else None
        return {"status": status, "bytes": payload, "content_type": ctype}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- sample profile (fictional)

SAMPLE_SETTINGS = {
    "preferred_employers": ["Rocket Lab", "Astranis", "Varda"],
    "preferred_locations": ["CA", "Remote"],
    "disciplines": ["Aerospace", "Mechanical"],
    "internship_terms": ["Summer 2027"],
    "profile": {"us_citizen": True, "needs_sponsorship": False, "degree_level": "bachelors", "graduation": "2028-05", "gpa": 3.6},
    "resume_header": {"name": "Sample Student", "email": "sample.student@example.com", "phone": "555-0100",
                      "links": ["github.com/sample-student"]},
}
SAMPLE_EXPERIENCE = [
    ({"kind": "education", "title": "B.S. Aerospace Engineering", "organization": "Example State University",
      "location": "Example City, IL", "start_date": "2024-08", "end_date": "2028-05", "is_current": True},
     ["GPA: 3.6/4.0; coursework in propulsion, structures, and controls"]),
    ({"kind": "employment", "title": "Propulsion Team Member", "organization": "Student Rocketry Club",
      "location": "Example City, IL", "start_date": "2024-09", "is_current": True},
     ["Built a LabVIEW data acquisition system for static fire tests and logged 40 test runs",
      "Wrote Python scripts to reduce test-stand data, cutting post-test analysis from hours to minutes",
      "Designed injector plumbing in SolidWorks and checked pressure drop with hand calculations",
      "Led a four-person team through a design review for a hybrid motor"]),
    ({"kind": "research", "title": "Undergraduate Researcher, Thermal Lab", "organization": "Example State University",
      "start_date": "2025-01", "end_date": "2025-08"},
     ["Ran thermal vacuum tests on small-satellite components and documented results",
      "Built a MATLAB thermal model that matched test data within 5 degrees C"]),
    ({"kind": "project", "title": "CubeSat Attitude Control Simulation", "organization": "Course project",
      "start_date": "2025-09", "end_date": "2025-12"},
     ["Simulated a reaction-wheel attitude controller in MATLAB and Simulink with a Kalman filter estimator"]),
    ({"kind": "project", "title": "Composite Wing Spar", "organization": "Design course",
      "start_date": "2025-01", "end_date": "2025-05"},
     ["Designed and tested a carbon fiber wing spar; FEA in ANSYS predicted failure load within 8% of test"]),
    ({"kind": "achievement", "title": "Dean's List", "start_date": "2025-05"}, []),
]
SAMPLE_SKILLS = [("MATLAB", "Software"), ("Python", "Software"), ("Simulink", "Software"), ("LabVIEW", "Software"),
                 ("SolidWorks", "CAD and analysis"), ("ANSYS", "CAD and analysis"), ("FEA", "CAD and analysis")]
MANUAL_IMPORT = [
    {"employer": "Example Aerospace Co (manual entry)", "title": "Structures Engineering Intern, Summer 2027",
     "url": "https://example.com/careers/structures-intern-2027", "location": "Seattle, WA", "deadline": "2026-12-15",
     "description": "Support structural analysis of flight hardware using FEA. Pursuing a degree in aerospace or mechanical engineering."},
    {"employer": "Example Research Lab (manual entry)", "title": "Undergraduate Research Intern, Propulsion",
     "url": "https://example.edu/jobs/propulsion-ura", "location": "Remote", "opportunity_type": "research",
     "description": "Assist with combustion experiments and data analysis in MATLAB. Must be a U.S. citizen."},
]


# ---------------------------------------------------------------- the run

class Report:
    def __init__(self):
        self.steps: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
        return ok


def pick(opps: list[dict], count: int) -> list[dict]:
    """A representative spread: distinct employers and both platforms first, then co-ops and uncertain cases."""
    chosen, employers = [], set()
    eligible = [o for o in opps if o["eligibility"] == "eligible" and o["source"] != "manual"]
    for o in eligible:  # best fit per employer
        if o["employer"] not in employers:
            chosen.append(o)
            employers.add(o["employer"])
        if len(chosen) >= count - 4:
            break
    for want in (lambda o: o["opportunity_type"] == "co-op", lambda o: o["eligibility"] == "uncertain"):
        extra = next((o for o in opps if want(o) and o not in chosen and o["source"] != "manual"), None)
        if extra:
            chosen.append(extra)
    chosen += [o for o in opps if o["source"] == "manual"]
    return chosen[:count + 2]


def fingerprint(srv: Server, ids: list[int]) -> dict:
    fp = {}
    apps = {a["opportunity_id"]: a for a in srv.call("GET", "/api/applications?status=all")}
    for oid in ids:
        o = srv.call("GET", f"/api/opportunities/{oid}")
        a = apps.get(oid)
        detail = srv.call("GET", f"/api/applications/{a['id']}") if a else None
        fp[oid] = {"user_state": o["user_state"], "notes": o["user_notes"], "title": o["title"],
                   "application": a and {"status": a["status"], "submitted_on": a["submitted_on"],
                                         "resume": a["resume_document_id"], "events": len(detail["events"])}}
    fp["_resumes"] = sorted((d["id"], d["pdf_sha256"]) for d in srv.call("GET", "/api/resumes"))
    return fp


def run(args) -> Report:
    rep = Report()
    work = Path(tempfile.mkdtemp(prefix="apogee-e2e-"))
    data = work / "data"
    srv = Server(data, free_port())
    timings: dict[str, list[float]] = {"resume": [], "per_opportunity": []}
    try:
        rep.check("Server starts on a fresh data folder", True, f"{srv.start():.1f} s")

        srv.call("PUT", "/api/settings", SAMPLE_SETTINGS)
        approved = 0
        for exp, statements in SAMPLE_EXPERIENCE:
            e = srv.call("POST", "/api/experiences", exp)
            for text in statements:
                sid = srv.call("POST", f"/api/experiences/{e['id']}/statements", {"text": text})["statements"][-1]["id"]
                srv.call("PATCH", f"/api/statements/{sid}", {"status": "verified"})
                srv.call("PATCH", f"/api/statements/{sid}", {"status": "approved"})
                approved += 1
        for name, cat in SAMPLE_SKILLS:
            srv.call("POST", "/api/skills", {"name": name, "category": cat})
        rep.check("Sample profile, verified experience and skills set up", True, f"{approved} approved statements")

        # 1-2. Discover and import; stored once; listed with explanations.
        t0 = time.monotonic()
        d1 = srv.call("POST", "/api/discovery/run")
        rep.check("Live discovery imports postings", d1["new"] > 0 and d1["failed_checks"] == 0,
                  f"{d1['new']} new from {len(d1['sources'])} sources in {time.monotonic() - t0:.1f} s, {d1['failed_checks']} failed sources")
        csv = "employer,title,url,location,opportunity_type,deadline,description\n" + "".join(
            f'"{m["employer"]}","{m["title"]}",{m["url"]},"{m["location"]}",{m.get("opportunity_type", "internship")},'
            f'{m.get("deadline", "")},"{m["description"]}"\n' for m in MANUAL_IMPORT)
        imp = srv.call("POST", "/api/opportunities/import", {"format": "csv", "content": csv})
        rep.check("Manual import of opportunities", imp["counts"]["created"] == len(MANUAL_IMPORT), json.dumps(imp["counts"]))
        imp2 = srv.call("POST", "/api/opportunities/import", {"format": "csv", "content": csv})
        rep.check("Repeating the manual import creates no duplicates", imp2["counts"]["created"] == 0 and imp2["counts"]["duplicate"] == len(MANUAL_IMPORT))

        opps = srv.call("GET", "/api/opportunities?state=all&sort=rank")
        keys = [(o["source"], o["url"]) for o in opps]
        rep.check("Every posting is stored once", len(keys) == len(set(keys)), f"{len(opps)} opportunities")
        chosen = pick(opps, args.count)
        rep.check(f"At least {args.count} representative opportunities selected", len(chosen) >= args.count,
                  f"{len(chosen)} across {len({o['employer'] for o in chosen})} employers, "
                  f"{len({o['source'] for o in chosen})} sources")

        # 3-6. For each: inspect, save, tailor a resume, check files, open link, track the application.
        rows = []
        for o in chosen:
            t_start = time.monotonic()
            detail = srv.call("GET", f"/api/opportunities/{o['id']}")
            r = detail["ranking"]
            explained = bool(r["eligibility"]["status"] and r["eligibility"]["summary"] and r["relevance"]["reasons"])
            link_ok = detail["url"].startswith(("https://", "http://"))
            srv.call("PATCH", f"/api/opportunities/{o['id']}", {"user_state": "saved", "user_notes": "Validated by e2e run"})
            t_res = time.monotonic()
            doc = srv.call("POST", "/api/resumes", {"opportunity_id": o["id"], "page_limit": 1}, expect=(201, 422))
            timings["resume"].append(time.monotonic() - t_res)
            resume_ok = "id" in doc and doc["validation"]["ok"] and doc["page_count"] == 1
            files_ok = False
            if resume_ok:
                pdf = srv.call("GET", f"/api/resumes/{doc['id']}/pdf")
                tex = srv.call("GET", f"/api/resumes/{doc['id']}/tex")
                files_ok = (pdf["bytes"].startswith(b"%PDF") and hashlib.sha256(pdf["bytes"]).hexdigest() == doc["pdf_sha256"]
                            and b"\\begin{document}" in tex["bytes"])
            app = srv.call("POST", "/api/applications", {"opportunity_id": o["id"]})
            upd = srv.call("PATCH", f"/api/applications/{app['id']}", {
                "status": "submitted", "submitted_on": date.today().isoformat(), "event_note": "Submitted on the employer site",
                **({"resume_document_id": doc["id"]} if resume_ok else {})})
            app_ok = upd["status"] == "submitted" and (not resume_ok or upd["resume_document_id"] == doc["id"]) and len(upd["events"]) >= 2
            timings["per_opportunity"].append(time.monotonic() - t_start)
            rows.append({"employer": o["employer"], "title": o["title"], "source": o["source"], "eligibility": r["eligibility"]["status"],
                         "score": r["score"], "explained": explained, "link": link_ok, "resume": resume_ok, "files": files_ok,
                         "dropped": len(doc.get("dropped", [])) if resume_ok else None, "app": app_ok,
                         "error": None if resume_ok else doc.get("detail")})
        for row in rows:
            rep.check(f"Workflow: {row['title'][:55]} ({row['employer']})",
                      row["explained"] and row["link"] and row["resume"] and row["files"] and row["app"],
                      "" if row["resume"] else f"resume: {row['error']}")
        dismiss = next(o for o in opps if o not in chosen)
        srv.call("PATCH", f"/api/opportunities/{dismiss['id']}", {"user_state": "dismissed"})
        rep.check("Dismiss keeps the record", srv.call("GET", f"/api/opportunities/{dismiss['id']}")["user_state"] == "dismissed")

        ids = [o["id"] for o in chosen] + [dismiss["id"]]
        before = fingerprint(srv, ids)

        # 7a. Repeated import.
        total_before = len(srv.call("GET", "/api/opportunities?state=all"))
        d2 = srv.call("POST", "/api/discovery/run")
        after_import = fingerprint(srv, ids)
        total_after = len(srv.call("GET", "/api/opportunities?state=all"))
        rep.check("Repeated discovery: no duplicates", total_after - total_before == d2["new"],
                  f"{d2['new']} genuinely new, {d2['unchanged']} unchanged, {d2['updated']} updated")
        rep.check("Repeated discovery: saved state, notes and applications untouched",
                  all(after_import[i]["user_state"] == before[i]["user_state"] and after_import[i]["notes"] == before[i]["notes"]
                      and after_import[i]["application"] == before[i]["application"] for i in ids))

        # 7b. Restart.
        srv.stop()
        restart_s = srv.start()
        rep.check("Everything survives a server restart", fingerprint(srv, ids) == after_import, f"restart {restart_s:.1f} s")

        # 7c. Backup, change things, restore.
        b = srv.call("POST", "/api/backups")
        app_id = srv.call("GET", "/api/applications?status=all")[0]["id"]
        srv.call("PATCH", f"/api/applications/{app_id}", {"status": "rejected", "event_note": "change after backup"})
        srv.call("PATCH", f"/api/opportunities/{ids[0]}", {"user_state": "dismissed"})
        blocked = srv.call("POST", f"/api/backups/{b['name']}/restore", {"confirm": b["name"]}, expect=(409,))
        rep.check("Restore refuses to overwrite newer changes without confirmation", "newer" in blocked)
        res = srv.call("POST", f"/api/backups/{b['name']}/restore", {"confirm": b["name"], "allow_newer": True})
        restored = fingerprint(srv, ids)
        rep.check("Backup restore brings every record back exactly", restored == after_import, f"safety copy {res['safety_backup']}")
        pdfs_ok = all(hashlib.sha256(srv.call("GET", f"/api/resumes/{rid}/pdf")["bytes"]).hexdigest() == sha
                      for rid, sha in restored["_resumes"])
        rep.check("Restored resumes still open, byte for byte", pdfs_ok, f"{len(restored['_resumes'])} resumes")
        srv.stop()
        srv.start()
        rep.check("Restored data survives another restart", fingerprint(srv, ids) == after_import)

        rep.rows, rep.timings, rep.discovery = rows, timings, (d1, d2)
        return rep
    finally:
        srv.stop()
        if args.keep:
            print(f"Data kept in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def write(rep: Report, out: Path | None) -> int:
    passed = sum(ok for _, ok, _ in rep.steps)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# End-to-end validation, {now}", "",
             f"**{passed}/{len(rep.steps)} checks passed.** A real Apogee server, a throwaway data folder, live discovery, "
             "and a fictional sample student. Your data was not used.", "",
             "## Checks", "", "| | Check | Detail |", "|---|---|---|"]
    lines += [f"| {'✅' if ok else '❌'} | {name} | {detail} |" for name, ok, detail in rep.steps]
    rows = getattr(rep, "rows", [])
    if rows:
        res = [r for r in rows if r["resume"]]
        t = rep.timings
        lines += ["", "## Opportunities", "",
                  "| Opportunity | Employer | Source | Eligibility | Fit | Resume | Statements dropped to fit | Application |",
                  "|---|---|---|---|---|---|---|---|"]
        lines += [f"| {r['title']} | {r['employer']} | {r['source']} | {r['eligibility']} | {r['score']} | "
                  f"{'valid PDF + .tex' if r['resume'] and r['files'] else 'FAILED: ' + str(r['error'])} | "
                  f"{r['dropped'] if r['dropped'] is not None else ''} | {'submitted, resume attached' if r['app'] else 'FAILED'} |"
                  for r in rows]
        lines += ["", "## Measurements", "",
                  f"- Resume generation: {len(res)}/{len(rows)} succeeded and validated; "
                  f"{min(t['resume']):.1f}–{max(t['resume']):.1f} s each (median {sorted(t['resume'])[len(t['resume']) // 2]:.1f} s).",
                  f"- System time per opportunity (inspect, save, tailor resume, check files, track application): "
                  f"median {sorted(t['per_opportunity'])[len(t['per_opportunity']) // 2]:.1f} s. "
                  "This is machine time only; your own time to review and apply has to be measured with real use.",
                  f"- Discovery: first run {rep.discovery[0]['new']} new postings; the repeat run added {rep.discovery[1]['new']} "
                  f"and left {rep.discovery[1]['unchanged']} unchanged (no duplicates)."]
    report = "\n".join(lines) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"Report: {out}")
    return 0 if passed == len(rep.steps) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--keep", action="store_true", help="keep the throwaway data folder for inspection")
    args = ap.parse_args()
    try:
        rep = run(args)
    except Fail as exc:
        print(f"ABORTED: {exc}")
        return 2
    return write(rep, args.out)


if __name__ == "__main__":
    sys.exit(main())
