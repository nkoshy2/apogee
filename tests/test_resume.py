import hashlib
import json
from pathlib import Path

import pytest

from aims import db
from aims.resume import build, latex, service
from aims.resume.plan import MAX_BULLETS, format_dates

needs_latex = pytest.mark.skipif(build.find_pdflatex() is None, reason="pdflatex not installed")


# ---------------------------------------------------------------- escaping

def test_escape_specials_and_unicode():
    assert latex.escape("R&D 50% $5 #1 a_b {x} ~^\\") == r"R\&D 50\% \$5 \#1 a\_b \{x\} \textasciitilde{}\textasciicircum{}\textbackslash{}"
    assert latex.escape("±5 °C — 3–4 “q” café") == r"$\pm$5 \textdegree{}C --- 3--4 ``q'' café"
    assert latex.escape("  two\n lines ") == "two lines"


def test_escape_rejects_unprintable_characters():
    with pytest.raises(latex.UnsupportedText, match="U\\+1F680"):
        latex.escape("Launched 🚀")


def test_links_are_sanitized():
    assert latex.link("github.com/me") == r"\href{https://github.com/me}{github.com/me}"
    assert latex.link("https://www.me.dev/a%20b#x") == r"\href{https://www.me.dev/a\%20b\#x}{me.dev/a\%20b\#x}"
    with pytest.raises(latex.UnsupportedText):
        latex.link("evil}\\input{x")
    with pytest.raises(latex.UnsupportedText):
        latex.email_link("not an email")


def test_render_never_injects_raw_latex():
    content = {"header": {"name": r"\input{secret}", "links": []},
               "sections": [{"kind": "project", "title": "Projects", "entries": [
                   {"title": "X", "organization": "", "location": "", "dates": "",
                    "items": [{"statement_id": 1, "text": r"\write18{rm -rf}"}]}]}]}
    tex = latex.render(content)
    assert r"\input{secret}" not in tex and r"\write18{" not in tex
    assert r"\textbackslash{}input\{secret\}" in tex


def test_template_version_includes_hash():
    v = latex.template_version()
    assert v.startswith(latex.TEMPLATE_NAME + "+") and len(v.split("+")[1]) == 12


def test_format_dates():
    assert format_dates({"kind": "employment", "start_date": "2024-09", "end_date": None, "is_current": 1}) == "Sep 2024 – Present"
    assert format_dates({"kind": "project", "start_date": "2025-01", "end_date": "2025-05", "is_current": 0}) == "Jan 2025 – May 2025"
    assert format_dates({"kind": "education", "start_date": "2024-08", "end_date": "2028-05", "is_current": 1}) == "Expected May 2028"
    assert format_dates({"kind": "achievement", "start_date": "2025-05", "end_date": None, "is_current": 0}) == "May 2025"
    assert format_dates({"kind": "project", "start_date": None, "end_date": None, "is_current": 0}) == ""


# ---------------------------------------------------------------- seeding helpers

def approve(client, exp_id, text, protected=False):
    e = client.post(f"/api/experiences/{exp_id}/statements", json={"text": text, "protected": protected}).json()
    sid = e["statements"][-1]["id"]
    client.patch(f"/api/statements/{sid}", json={"status": "verified"})
    assert client.patch(f"/api/statements/{sid}", json={"status": "approved"}).status_code == 200
    return sid


def seed(client, header=True):
    if header:
        client.put("/api/settings", json={"resume_header": {"name": "Test Person", "email": "t@example.com"}})
    opp = client.post("/api/opportunities", json={
        "employer": "Acme Aero", "title": "Propulsion Intern", "url": "https://acme.com/1",
        "description": "Hot fire testing and data acquisition with MATLAB. Thermal analysis a plus."}).json()
    edu = client.post("/api/experiences", json={"kind": "education", "title": "B.S. Aerospace Engineering",
        "organization": "State University", "end_date": "2028-05", "is_current": True}).json()
    job = client.post("/api/experiences", json={"kind": "employment", "title": "Rocket Team", "organization": "Club",
        "start_date": "2024-09", "is_current": True}).json()
    proj = client.post("/api/experiences", json={"kind": "project", "title": "Bridge Model", "start_date": "2024-01",
        "end_date": "2024-05"}).json()
    ids = {
        "edu": approve(client, edu["id"], "GPA: 3.6/4.0"),
        "matlab": approve(client, job["id"], "Wrote MATLAB scripts to reduce hot fire test data"),
        "cad": approve(client, job["id"], "Designed brackets in a CAD tool"),
        "proj": approve(client, proj["id"], "Built a balsa bridge for a class contest"),
    }
    draft = client.post(f"/api/experiences/{job['id']}/statements", json={"text": "Unapproved claim"}).json()
    ids["draft"] = draft["statements"][-1]["id"]
    client.post("/api/skills", json={"name": "MATLAB", "category": "Software"})
    client.post("/api/skills", json={"name": "Excel", "category": "Software"})
    return opp, ids


def plan(client, **body):
    r = client.post("/api/resumes/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def statements_in(p):
    return [s for sec in p["sections"] for e in sec["entries"] for s in e["statements"]]


# ---------------------------------------------------------------- planning

def test_plan_uses_only_approved_and_orders_by_relevance(client):
    opp, ids = seed(client)
    p = plan(client, opportunity_id=opp["id"])
    texts = [s["text"] for s in statements_in(p)]
    assert "Unapproved claim" not in texts
    assert [s["title"] for s in p["sections"]][0] == "Education"
    job_entry = next(e for sec in p["sections"] for e in sec["entries"] if e["title"] == "Rocket Team")
    assert job_entry["statements"][0]["id"] == ids["matlab"]  # most relevant first
    assert "MATLAB" in job_entry["statements"][0]["matched"]
    assert p["skills"][0]["items"][0] == {"name": "MATLAB", "relevant": True}


def test_plan_include_exclude_and_protected(client):
    opp, ids = seed(client)
    p = plan(client, opportunity_id=opp["id"], exclude_ids=[ids["cad"]], include_ids=[ids["draft"]])
    by_id = {s["id"]: s for s in statements_in(p)}
    assert by_id[ids["cad"]]["included"] is False and by_id[ids["cad"]]["reason"] == "excluded by you"
    assert any("not approved" in w for w in p["warnings"])
    client.patch(f"/api/statements/{ids['cad']}", json={"protected": True})
    p = plan(client, opportunity_id=opp["id"], exclude_ids=[ids["cad"]])
    assert {s["id"]: s for s in statements_in(p)}[ids["cad"]]["included"] is True
    assert any("protected" in w for w in p["warnings"])


def test_plan_caps_bullets_per_entry(client):
    client.put("/api/settings", json={"resume_header": {"name": "T"}})
    e = client.post("/api/experiences", json={"kind": "employment", "title": "Job"}).json()
    sids = [approve(client, e["id"], f"Did thing number {i}") for i in range(MAX_BULLETS + 2)]
    p = plan(client)
    included = [s["id"] for s in statements_in(p) if s["included"]]
    assert len(included) == MAX_BULLETS and included == sids[:MAX_BULLETS]  # general resume keeps user order
    p = plan(client, include_ids=[sids[-1]])
    assert sids[-1] in [s["id"] for s in statements_in(p) if s["included"]]


def test_entry_without_approved_statements_is_left_out_with_warning(client):
    client.post("/api/experiences", json={"kind": "employment", "title": "Empty Job"})
    p = plan(client)
    assert any("Empty Job" in w for w in p["warnings"])
    assert not any(e["included"] for sec in p["sections"] for e in sec["entries"])


def test_plan_unknown_opportunity_404(client):
    assert client.post("/api/resumes/plan", json={"opportunity_id": 999}).status_code == 404


# ---------------------------------------------------------------- generation (real LaTeX)

@needs_latex
def test_generate_valid_pdf_and_reproducible_record(client, config):
    opp, ids = seed(client)
    r = client.post("/api/resumes", json={"opportunity_id": opp["id"]})
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["page_count"] == 1 and doc["validation"]["ok"]
    assert {c["name"] for c in doc["validation"]["checks"]} >= {"Compiles", "Page count", "Extractable text", "Reading order"}
    folder = config.documents_dir / doc["folder"]
    pdf_bytes = (folder / "resume.pdf").read_bytes()
    assert hashlib.sha256(pdf_bytes).hexdigest() == doc["pdf_sha256"]
    assert pdf_bytes.startswith(b"%PDF")

    f = client.get(f"/api/resumes/{doc['id']}/pdf?download=true")
    assert f.status_code == 200 and f.headers["content-type"] == "application/pdf"
    assert 'attachment; filename="Test-Person_Resume_Acme-Aero.pdf"' in f.headers["content-disposition"]
    tex = client.get(f"/api/resumes/{doc['id']}/tex")
    assert tex.content == (folder / "resume.tex").read_bytes()
    assert hashlib.sha256(tex.content).hexdigest() == doc["tex_sha256"]

    # Later edits don't change the stored record or files.
    client.patch(f"/api/statements/{ids['matlab']}", json={"text": "Changed wording"})
    again = client.get(f"/api/resumes/{doc['id']}").json()
    texts = [i["text"] for s in again["content"]["sections"] if s["kind"] != "skills" for e in s["entries"] for i in e["items"]]
    assert "Wrote MATLAB scripts to reduce hot fire test data" in texts and "Changed wording" not in texts
    assert (folder / "resume.pdf").read_bytes() == pdf_bytes
    # Statements used by the document can't be deleted.
    assert client.delete(f"/api/statements/{ids['matlab']}").status_code == 409


@needs_latex
def test_each_generation_creates_a_new_document(client, config):
    opp, _ = seed(client)
    a = client.post("/api/resumes", json={"opportunity_id": opp["id"]}).json()
    b = client.post("/api/resumes", json={}).json()
    assert a["folder"] != b["folder"] and (config.documents_dir / a["folder"] / "resume.pdf").exists()
    assert [d["id"] for d in client.get("/api/resumes").json()] == [b["id"], a["id"]]
    assert [d["id"] for d in client.get(f"/api/resumes?opportunity_id={opp['id']}").json()] == [a["id"]]
    assert b["opportunity_id"] is None


@needs_latex
def test_page_fit_drops_lowest_scoring_optional_statements(client):
    opp, ids = seed(client)
    long = "Performed detailed structural and thermal analysis of flight hardware brackets, documenting every load case in a formal report " * 2
    e = client.post("/api/experiences", json={"kind": "project", "title": "Filler Project"}).json()
    filler = [approve(client, e["id"], f"{i}: {long}") for i in range(4)]
    for i in range(10):
        e2 = client.post("/api/experiences", json={"kind": "project", "title": f"Filler {i}"}).json()
        filler += [approve(client, e2["id"], f"{i}-{j}: {long}") for j in range(4)]
    r = client.post("/api/resumes", json={"opportunity_id": opp["id"], "page_limit": 1})
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["page_count"] == 1 and doc["dropped"]
    kept = {i["statement_id"] for s in doc["content"]["sections"] if s["kind"] != "skills" for e in s["entries"] for i in e["items"]}
    assert ids["matlab"] in kept  # the most relevant statement survives
    # Nothing dropped scores higher than anything kept.
    scores = {s["id"]: s["score"] for s in statements_in(plan(client, opportunity_id=opp["id"]))}
    assert max(d["score"] for d in doc["dropped"]) <= min(scores[i] for i in kept)
    assert not {d["id"] for d in doc["dropped"]} & kept


@needs_latex
def test_protected_overflow_fails_without_saving(client, config):
    client.put("/api/settings", json={"resume_header": {"name": "T"}})
    long = "Protected statement that the user insists must always appear on every resume no matter what happens " * 3
    for i in range(14):
        e = client.post("/api/experiences", json={"kind": "employment", "title": f"Job {i}", "protected": True}).json()
        for j in range(3):
            approve(client, e["id"], f"{i}.{j} {long}", protected=True)
    r = client.post("/api/resumes", json={"page_limit": 1})
    assert r.status_code == 422 and "Protected" in r.json()["detail"]
    assert client.get("/api/resumes").json() == []
    assert list(config.documents_dir.iterdir()) == [] and list(config.tmp_dir.iterdir()) == []


# ---------------------------------------------------------------- failure handling (no LaTeX needed)

def test_generation_requires_header_name(client):
    opp, _ = seed(client, header=False)
    r = client.post("/api/resumes", json={"opportunity_id": opp["id"]})
    assert r.status_code == 422 and "name" in r.json()["detail"]


def test_unsupported_character_reported_and_nothing_saved(client, config):
    seed(client)
    e = client.post("/api/experiences", json={"kind": "project", "title": "Emoji 🚀 Project"}).json()
    approve(client, e["id"], "Something")
    r = client.post("/api/resumes", json={})
    assert r.status_code == 422 and "U+1F680" in r.json()["detail"]
    assert client.get("/api/resumes").json() == []


def test_failed_compile_keeps_previous_documents(client, config, monkeypatch):
    seed(client)

    def fake_ok(tex, workdir):
        # A tiny valid one-page PDF stands in for LaTeX output.
        from pypdf import PdfWriter
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "resume.tex").write_text(tex, encoding="utf-8")
        w = PdfWriter()
        w.add_blank_page(612, 792)
        with open(workdir / "resume.pdf", "wb") as f:
            w.write(f)
        return build.CompileResult(True, workdir / "resume.pdf", "")

    def fake_fail(tex, workdir):
        return build.CompileResult(False, None, "", "LaTeX could not compile the resume:\n! Undefined control sequence.")

    # A blank PDF has no extractable text, so validation must reject it and save nothing.
    monkeypatch.setattr(build, "compile_tex", fake_ok)
    conn = db.connect(config.db_path)
    with pytest.raises(service.GenerationError, match="failed validation") as exc:
        service.generate(conn, config, __import__("aims.settings_store", fromlist=["load"]).load(conn), None, compiler=fake_ok)
    assert any(c["name"] == "Extractable text" and c["status"] == "fail" for c in exc.value.details["checks"])

    with pytest.raises(service.GenerationError, match="Undefined control sequence"):
        service.generate(conn, config, __import__("aims.settings_store", fromlist=["load"]).load(conn), None, compiler=fake_fail)
    conn.close()
    assert client.get("/api/resumes").json() == []
    assert list(config.documents_dir.iterdir()) == [] and list(config.tmp_dir.iterdir()) == []


def test_document_file_refuses_paths_outside_documents(client, config):
    conn = db.connect(config.db_path)
    conn.execute(
        "INSERT INTO resume_documents (template_version, page_limit, page_count, content_json, validation_json, folder, "
        "tex_sha256, pdf_sha256, created_at) VALUES ('t', 1, 1, ?, '{}', '..', 'a', 'b', 'n')",
        (json.dumps({"header": {"name": "x"}, "sections": []}),))
    conn.close()
    (config.data_dir / "resume.pdf").write_bytes(b"%PDF secret")  # sits right outside documents/
    assert client.get("/api/resumes/1/pdf").status_code == 404
    assert client.get("/api/resumes/1/exe").status_code == 422


@needs_latex
def test_attach_resume_to_application(client):
    opp, _ = seed(client)
    doc = client.post("/api/resumes", json={"opportunity_id": opp["id"]}).json()
    app = client.post("/api/applications", json={"opportunity_id": opp["id"]}).json()
    got = client.patch(f"/api/applications/{app['id']}", json={"resume_document_id": doc["id"]}).json()
    assert got["resume_document_id"] == doc["id"] and got["events"][-1]["note"] == f"Resume #{doc['id']} attached"
    assert client.patch(f"/api/applications/{app['id']}", json={"resume_document_id": 999}).status_code == 404
    # Re-sending the same value adds no history noise.
    n = len(got["events"])
    assert len(client.patch(f"/api/applications/{app['id']}", json={"resume_document_id": doc["id"]}).json()["events"]) == n


def test_job_title_topic_leads_the_resume(client):
    """Regression from validation: a thermal internship listing many other topics should still lead
    with thermal work, not with the entry that matches the most keywords."""
    client.put("/api/settings", json={"resume_header": {"name": "T"}})
    club = client.post("/api/experiences", json={"kind": "employment", "title": "Rocket Club", "start_date": "2025-01"}).json()
    for text in ("Wrote Python scripts for test data", "Designed fluid plumbing in a CAD tool", "Ran static fire testing"):
        approve(client, club["id"], text)
    lab = client.post("/api/experiences", json={"kind": "research", "title": "Thermal Lab", "start_date": "2024-01"}).json()
    approve(client, lab["id"], "Built a thermal model in MATLAB")
    opp = client.post("/api/opportunities", json={"employer": "E", "title": "Thermal Engineering Intern", "url": "https://e.example/t",
        "description": "Thermal analysis with MATLAB. Also Python, testing, CAD and fluid systems."}).json()
    p = plan(client, opportunity_id=opp["id"])
    middle = [s["title"] for s in p["sections"] if s["kind"] in ("employment", "research")]
    assert middle == ["Research", "Experience"]

