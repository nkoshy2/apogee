import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aims import db
from aims.discovery import greenhouse, importer, lever, model, publish, run

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- helpers

def fake_http(responses: dict):
    """http_get stand-in: maps URL -> JSON value, or an Exception instance to raise."""
    def get(url):
        value = responses.get(url, model.FetchError(f"HTTP 404 from {url}"))
        if isinstance(value, Exception):
            raise value
        return value
    return get


GH = greenhouse.API


def gh_board(key, jobs, total=None):
    listing = {"jobs": [{"id": j["id"], "title": j["title"]} for j in jobs],
               "meta": {"total": len(jobs) if total is None else total}}
    responses = {f"{GH}/{key}/jobs": listing}
    for j in jobs:
        responses[f"{GH}/{key}/jobs/{j['id']}"] = {
            "id": j["id"], "title": j["title"], "absolute_url": j.get("url", f"https://boards.greenhouse.io/{key}/jobs/{j['id']}"),
            "location": {"name": "Hawthorne, CA"}, "content": "&lt;p&gt;Build &amp;amp; test.&lt;/p&gt;",
            "application_deadline": j.get("deadline"), "updated_at": "2026-10-01T00:00:00-04:00", "departments": []}
    return responses


def posting(pid, title="Propulsion Intern", **kw):
    return {"source_posting_id": str(pid), "title": title, "url": kw.pop("url", f"https://boards.greenhouse.io/acme/jobs/{pid}"),
            "location": "Hawthorne, CA", "opportunity_type": "internship", "description": "Build things.", "deadline": None, **kw}


def check(postings=(), status="ok", complete=True, at="2026-10-26T12:00:00Z", key="acme", error=None):
    return {"source": "greenhouse", "source_key": key, "employer": "Acme Aero", "status": status, "complete": complete,
            "error": error, "fetched_at": at, "total_on_board": 100, "postings": list(postings)}


_counter = iter(range(10**6))


def feed(*checks, at="2026-10-26T12:00:00Z", origin="cloud"):
    return {"schema": "aims-feed", "version": 1, "run_id": f"20261026T120000Z-{next(_counter):06x}", "origin": origin,
            "generated_at": at, "checks": list(checks)}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c, tmp_path / "b")
    yield c
    c.close()


def opp(conn, pid="1", key="acme"):
    row = conn.execute("SELECT * FROM opportunities WHERE source = 'greenhouse' AND source_key = ? AND source_posting_id = ?",
                       (key, pid)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- text helpers

def test_html_to_text_handles_escaped_html_and_strips_markup():
    text = model.html_to_text("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;MATLAB&lt;/li&gt;&lt;/ul&gt;")
    assert text == "Hello & welcome\n\n• MATLAB"
    assert model.html_to_text("<script>alert(1)</script><b>Bold</b> <img src=x onerror=y>") == "Bold"


def test_student_role_filter_and_type():
    assert model.is_student_role("Propulsion Intern (Summer 2027)")
    assert model.is_student_role("Winter 2027 Software Engineer Co-op")
    assert model.is_student_role("Engineer", ["Intern"])
    assert not model.is_student_role("Internal Tools Engineer")
    assert not model.is_student_role("Apprentice Controls Technician")
    assert model.classify("Software Co-op") == "co-op" and model.classify("GNC Intern") == "internship"


# ---------------------------------------------------------------- adapters

def test_greenhouse_complete_check_keeps_only_student_roles():
    jobs = [{"id": 1, "title": "Propulsion Intern", "deadline": "2026-12-01T23:59:00Z"}, {"id": 2, "title": "Senior Engineer"}]
    c = greenhouse.fetch("acme", "Acme", fake_http(gh_board("acme", jobs)))
    assert c.status == "ok" and c.complete and c.total_on_board == 2
    [p] = c.postings
    assert p.source_posting_id == "1" and p.description == "Build & test." and p.deadline == "2026-12-01"
    assert p.source_api_url.endswith("/acme/jobs/1") and len(p.raw_sha256) == 64


def test_greenhouse_count_mismatch_is_incomplete():
    c = greenhouse.fetch("acme", "Acme", fake_http(gh_board("acme", [{"id": 1, "title": "GNC Intern"}], total=5)))
    assert c.status == "ok" and not c.complete and "reports 5" in c.error


def test_greenhouse_detail_failure_is_incomplete_not_failed():
    responses = gh_board("acme", [{"id": 1, "title": "A Intern"}, {"id": 2, "title": "B Intern"}])
    responses[f"{GH}/acme/jobs/2"] = model.FetchError("HTTP 500")
    c = greenhouse.fetch("acme", "Acme", fake_http(responses))
    assert c.status == "ok" and not c.complete and [p.source_posting_id for p in c.postings] == ["1"]


def test_greenhouse_unsafe_url_is_skipped_as_incomplete():
    c = greenhouse.fetch("acme", "Acme", fake_http(gh_board("acme", [{"id": 1, "title": "Intern", "url": "javascript:alert(1)"}])))
    assert not c.complete and c.postings == []


@pytest.mark.parametrize("listing", [model.FetchError("HTTP 404"), {"oops": True}, {"jobs": "nope"}])
def test_greenhouse_listing_failure_fails_check(listing):
    c = greenhouse.fetch("acme", "Acme", fake_http({f"{GH}/acme/jobs": listing}))
    assert c.status == "failed" and not c.complete and c.postings == []


def test_lever_adapter():
    data = [
        {"id": "a", "text": "Avionics Intern", "hostedUrl": "https://jobs.lever.co/acme/a", "categories": {"location": "Atlanta, GA"},
         "descriptionPlain": "Intro", "lists": [{"text": "Requirements:", "content": "<li>C++</li>"}], "additionalPlain": "Pay"},
        {"id": "b", "text": "Engineer", "hostedUrl": "https://jobs.lever.co/acme/b", "categories": {"commitment": "Intern"}},
        {"id": "c", "text": "Senior Engineer", "hostedUrl": "https://jobs.lever.co/acme/c", "categories": {}},
    ]
    c = lever.fetch("acme", "Acme", fake_http({f"{lever.API}/acme?mode=json": data}))
    assert c.complete and [p.source_posting_id for p in c.postings] == ["a", "b"]
    assert c.postings[0].description == "Intro\n\nRequirements:\n• C++\n\nPay" and c.postings[0].location == "Atlanta, GA"
    failed = lever.fetch("acme", "Acme", fake_http({f"{lever.API}/acme?mode=json": {"ok": False}}))
    assert failed.status == "failed"


def test_run_records_adapter_crash_as_failed_and_keeps_others(monkeypatch):
    def boom(key, employer, http_get):
        raise RuntimeError("bug")
    monkeypatch.setitem(run.ADAPTERS, "lever", boom)
    sources = [{"source": "greenhouse", "key": "acme", "employer": "Acme"}, {"source": "lever", "key": "x", "employer": "X"}]
    f = run.run(sources, "local", fake_http(gh_board("acme", [{"id": 1, "title": "Intern"}])))
    states = {c["source_key"]: c["status"] for c in f["checks"]}
    assert states == {"acme": "ok", "x": "failed"} and "RuntimeError" in f["checks"][1]["error"]


def test_feed_contains_only_public_posting_fields(monkeypatch):
    f = run.run([{"source": "greenhouse", "key": "acme", "employer": "Acme"}], "cloud",
                fake_http(gh_board("acme", [{"id": 1, "title": "Intern"}])))
    assert set(f) == {"schema", "version", "run_id", "origin", "generated_at", "checks"}
    assert set(f["checks"][0]["postings"][0]) == set(model.Posting.__dataclass_fields__)


def test_cloud_code_cannot_reach_private_data():
    """The modules the cloud runs must not import the database, settings, or anything outside the stdlib."""
    code = ("import sys, aims.discovery.run, aims.discovery.publish; "
            "bad = [m for m in ('aims.db', 'aims.settings_store', 'aims.opportunities', 'aims.experience', "
            "'aims.applications', 'sqlite3', 'pydantic', 'fastapi') if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_sources_file_is_valid():
    sources = run.load_sources()
    assert len(sources) >= 3 and {s["source"] for s in sources} == {"greenhouse", "lever"}
    assert len({(s["source"], s["key"]) for s in sources}) == len(sources)


# ---------------------------------------------------------------- publish

def test_publish_indexes_hashes_and_prunes(tmp_path):
    fd = tmp_path / "feed"
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    for day in (1, 2, 3, 4, 5, 18, 19):
        f = feed(check([posting(1)]), at=f"2026-11-{day:02d}T05:00:00Z")
        f["run_id"] = f"202611{day:02d}T050000Z-{day:06x}"
        p = tmp_path / f"r{day}.json"
        p.write_text(json.dumps(f))
        index = publish.publish(p, fd, keep_days=14, keep_min=4, now=now)
    kept = [e["run_id"][:8] for e in index["runs"]]
    assert kept == ["20261104", "20261105", "20261118", "20261119"]  # >14 days old pruned, newest 4 kept
    assert sorted(x.name[:8] for x in (fd / "runs").iterdir()) == kept
    e = index["runs"][-1]
    assert hashlib.sha256((fd / e["file"]).read_bytes()).hexdigest() == e["sha256"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "nope"}))
    with pytest.raises(ValueError):
        publish.publish(bad, fd)


# ---------------------------------------------------------------- import invariants

def test_repeated_imports_never_duplicate(conn):
    f = feed(check([posting(1), posting(2)]))
    assert importer.import_feed(conn, f)["new"] == 2
    assert importer.import_feed(conn, f) == {"run_id": f["run_id"], "skipped": True}
    later = importer.import_feed(conn, feed(check([posting(1), posting(2)], at="2026-10-27T00:00:00Z"), at="2026-10-27T00:00:00Z"))
    assert later["new"] == 0 and later["unchanged"] == 2
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM opportunity_snapshots").fetchone()[0] == 2  # unchanged: no new snapshot


def test_same_posting_twice_in_one_check_counts_once(conn):
    s = importer.import_feed(conn, feed(check([posting(1), posting(1)])))
    assert s["new"] == 1 and conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1


def test_update_preserves_user_data_and_applications(conn):
    importer.import_feed(conn, feed(check([posting(1, deadline="2026-12-01")])))
    oid = opp(conn)["id"]
    conn.execute("UPDATE opportunities SET user_state = 'saved', user_notes = 'my notes' WHERE id = ?", (oid,))
    conn.execute("INSERT INTO applications (opportunity_id, status, created_at, updated_at) VALUES (?, 'submitted', 'n', 'n')", (oid,))
    s = importer.import_feed(conn, feed(check([posting(1, title="Propulsion Intern (Summer 2027)", deadline=None)],
                                              at="2026-10-27T00:00:00Z"), at="2026-10-27T00:00:00Z"))
    o = opp(conn)
    assert s["updated"] == 1 and o["title"] == "Propulsion Intern (Summer 2027)"
    assert o["user_state"] == "saved" and o["user_notes"] == "my notes"
    assert o["deadline"] == "2026-12-01"  # the source stopped stating it; keep the last known value
    assert conn.execute("SELECT opportunity_id FROM applications").fetchone()[0] == oid
    assert conn.execute("SELECT COUNT(*) FROM opportunity_snapshots WHERE opportunity_id = ?", (oid,)).fetchone()[0] == 2


@pytest.mark.parametrize("bad_check", [
    check([], status="failed", error="HTTP 503"),
    check([posting(2)], complete=False, error="detail failed"),
])
def test_failed_or_partial_checks_never_close_postings(conn, bad_check):
    importer.import_feed(conn, feed(check([posting(1), posting(2)])))
    bad_check["fetched_at"] = "2026-10-27T00:00:00Z"
    importer.import_feed(conn, feed(bad_check, at="2026-10-27T00:00:00Z"))
    assert {opp(conn, p)["listing_status"] for p in ("1", "2")} == {"active"}


def test_complete_check_marks_absent_postings_not_seen_and_they_can_return(conn):
    importer.import_feed(conn, feed(check([posting(1), posting(2)])))
    s = importer.import_feed(conn, feed(check([posting(1)], at="2026-10-27T00:00:00Z"), at="2026-10-27T00:00:00Z"))
    assert s["not_seen"] == 1
    gone = opp(conn, "2")
    assert gone["listing_status"] == "not_seen" and gone["missing_since"] == "2026-10-27T00:00:00Z"
    importer.import_feed(conn, feed(check([posting(1)], at="2026-10-28T00:00:00Z"), at="2026-10-28T00:00:00Z"))
    assert opp(conn, "2")["missing_since"] == "2026-10-27T00:00:00Z"  # first-missed date is kept
    importer.import_feed(conn, feed(check([posting(1), posting(2)], at="2026-10-29T00:00:00Z"), at="2026-10-29T00:00:00Z"))
    back = opp(conn, "2")
    assert back["listing_status"] == "active" and back["missing_since"] is None
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 2  # nothing ever deleted


def test_absence_only_affects_the_checked_source(conn):
    importer.import_feed(conn, feed(check([posting(1)], key="acme"), check([posting(9)], key="other")))
    importer.import_feed(conn, feed(check([], key="acme", at="2026-10-27T00:00:00Z"), at="2026-10-27T00:00:00Z"))
    assert opp(conn, "1", "acme")["listing_status"] == "not_seen"
    assert opp(conn, "9", "other")["listing_status"] == "active"


def test_user_closed_listing_stays_closed(conn):
    importer.import_feed(conn, feed(check([posting(1)])))
    conn.execute("UPDATE opportunities SET listing_status = 'closed'")
    importer.import_feed(conn, feed(check([posting(1)], at="2026-10-27T00:00:00Z"), at="2026-10-27T00:00:00Z"))
    assert opp(conn)["listing_status"] == "closed"


def test_older_run_never_overwrites_newer_data(conn):
    importer.import_feed(conn, feed(check([posting(1, title="New title")], at="2026-10-28T00:00:00Z"), at="2026-10-28T00:00:00Z"))
    s = importer.import_feed(conn, feed(check([posting(2)], at="2026-10-26T00:00:00Z"), at="2026-10-26T00:00:00Z"))
    s2 = importer.import_feed(conn, feed(check([posting(1, title="Old title")], at="2026-10-27T00:00:00Z"), at="2026-10-27T00:00:00Z"))
    assert opp(conn)["title"] == "New title" and s2["stale"] == 1
    assert opp(conn)["listing_status"] == "active"  # the older complete run must not mark it missing


def test_discovered_posting_adopts_matching_manual_entry(conn):
    from aims import opportunities
    oid = opportunities.create_manual(conn, opportunities.OpportunityIn(
        employer="Acme", title="My copy", url="https://boards.greenhouse.io/acme/jobs/1?utm_source=x", user_notes="keep"))
    s = importer.import_feed(conn, feed(check([posting(1)])))
    o = opp(conn)
    assert s["adopted"] == 1 and s["new"] == 0 and o["id"] == oid
    assert o["user_notes"] == "keep" and o["title"] == "Propulsion Intern"
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1


@pytest.mark.parametrize("mutate", [
    lambda f: f["checks"][0]["postings"].append(posting(3, url="javascript:alert(1)")),
    lambda f: f.update(schema="something-else"),
    lambda f: f.update(run_id="../../etc"),
    lambda f: f["checks"][0].update(source="workday"),
    lambda f: f["checks"][0].update(fetched_at="yesterday"),
])
def test_invalid_feed_is_rejected_whole(conn, mutate):
    f = feed(check([posting(1), posting(2)]))
    mutate(f)
    with pytest.raises(importer.FeedError, match="nothing imported"):
        importer.import_feed(conn, f)
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM imported_feed_runs").fetchone()[0] == 0


def test_source_checks_recorded_including_failures(conn):
    importer.import_feed(conn, feed(check([posting(1)]), check([], key="down", status="failed", error="HTTP 503")))
    rows = conn.execute("SELECT s.source_key, c.status, c.complete, c.error FROM source_checks c JOIN sources s ON s.id = c.source_id "
                        "ORDER BY s.source_key").fetchall()
    assert [tuple(r) for r in rows] == [("acme", "ok", 1, None), ("down", "failed", 0, "HTTP 503")]


# ---------------------------------------------------------------- remote sync

def remote(runs: list[dict], tamper: str | None = None):
    files, entries = {}, []
    for f in runs:
        data = json.dumps(f).encode()
        name = f"runs/{f['run_id']}.json"
        files[f"https://feed.example/v1/{name}"] = data
        entries.append({"run_id": f["run_id"], "file": name, "generated_at": f["generated_at"], "sha256": hashlib.sha256(data).hexdigest()})
    if tamper:
        files[f"https://feed.example/v1/runs/{tamper}.json"] = b'{"tampered": true}'
    files["https://feed.example/v1/latest.json"] = json.dumps({"runs": entries[::-1]}).encode()

    def get(url):
        if url not in files:
            raise importer.FeedError(f"HTTP 404 from {url}")
        return files[url]
    return get


def test_sync_imports_new_runs_in_order_and_skips_known(conn):
    a = feed(check([posting(1, title="v1")], at="2026-10-26T00:00:00Z"), at="2026-10-26T00:00:00Z")
    b = feed(check([posting(1, title="v2")], at="2026-10-27T00:00:00Z"), at="2026-10-27T00:00:00Z")
    r = importer.sync_remote(conn, "https://feed.example/v1/", remote([a, b]))
    assert [x["run_id"] for x in r["imported"]] == [a["run_id"], b["run_id"]] and not r["errors"]
    assert opp(conn)["title"] == "v2"
    assert importer.sync_remote(conn, "https://feed.example/v1", remote([a, b]))["imported"] == []


def test_sync_rejects_tampered_run_but_imports_the_rest(conn):
    a = feed(check([posting(1)]), at="2026-10-26T00:00:00Z")
    b = feed(check([posting(2)], at="2026-10-27T00:00:00Z"), at="2026-10-27T00:00:00Z")
    r = importer.sync_remote(conn, "https://feed.example/v1", remote([a, b], tamper=a["run_id"]))
    assert len(r["errors"]) == 1 and "checksum" in r["errors"][0]
    assert [x["run_id"] for x in r["imported"]] == [b["run_id"]]


def test_sync_refuses_unsafe_urls_and_paths(conn):
    with pytest.raises(importer.FeedError):
        importer.sync_remote(conn, "http://feed.example/v1", remote([]))
    f = feed(check([posting(1)]))
    get = remote([f])
    index = json.loads(get("https://feed.example/v1/latest.json"))
    index["runs"][0]["file"] = "../../secrets.json"
    r = importer.sync_remote(conn, "https://feed.example/v1",
                             lambda u: json.dumps(index).encode() if u.endswith("latest.json") else get(u))
    assert r["imported"] == [] and "unexpected file name" in r["errors"][0]


# ---------------------------------------------------------------- health

def test_health_states(conn):
    importer.import_feed(conn, feed(
        check([posting(1)], key="spacex"), check([], key="rocketlab", status="failed", error="HTTP 503"),
        check([posting(2)], key="astranis", complete=False, error="partial"), check([posting(3)], key="relativity", at="2026-10-01T00:00:00Z")))
    h = importer.health(conn, now="2026-10-26T13:00:00Z")
    states = {s["source_key"]: s["state"] for s in h["sources"]}
    assert states["spacex"] == "ok" and states["rocketlab"] == "failing" and states["astranis"] == "partial"
    assert states["relativity"] == "stale" and states["hermeus"] == "never checked"
    assert next(s for s in h["sources"] if s["source_key"] == "rocketlab")["last_error"] == "HTTP 503"


# ---------------------------------------------------------------- API

def test_api_run_here_and_imported_postings_appear(client, monkeypatch):
    f = feed(check([posting(1, title="Test Stand Intern")]), origin="local")
    monkeypatch.setattr(importer.run, "run", lambda sources, origin, http_get: f)
    r = client.post("/api/discovery/run")
    assert r.status_code == 200 and r.json()["new"] == 1
    [o] = client.get("/api/opportunities").json()
    assert o["title"] == "Test Stand Intern" and o["source"] == "greenhouse"
    # Discovered postings are read-only except user fields.
    assert client.patch(f"/api/opportunities/{o['id']}", json={"title": "x"}).status_code == 409
    assert client.patch(f"/api/opportunities/{o['id']}", json={"user_state": "saved"}).status_code == 200


def test_api_sync_requires_configured_https_feed(client):
    assert client.post("/api/discovery/sync").status_code == 409
    assert client.put("/api/settings", json={"discovery_feed_url": "http://evil.example/feed"}).status_code == 422
    assert client.put("/api/settings", json={"discovery_feed_url": "https://raw.githubusercontent.com/me/r/discovery-feed/feed/v1"}).status_code == 200


def test_api_import_file(client):
    f = feed(check([posting(1)]))
    r = client.post("/api/discovery/import", json={"content": json.dumps(f)})
    assert r.status_code == 200 and r.json()["new"] == 1
    assert client.post("/api/discovery/import", json={"content": "{not json"}).status_code == 422
    health = client.get("/api/discovery/health").json()
    assert health["runs_imported"] == 1
