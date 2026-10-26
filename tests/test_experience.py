import sqlite3

import pytest

from aims import db
from tests.conftest import make_client


def exp(client, **kw):
    body = {"kind": "employment", "title": "Propulsion Intern", "organization": "Acme", **kw}
    r = client.post("/api/experiences", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def stmt(client, exp_id, text="Built a test stand"):
    r = client.post(f"/api/experiences/{exp_id}/statements", json={"text": text})
    assert r.status_code == 201, r.text
    return r.json()["statements"][-1]


def set_status(client, sid, status):
    return client.patch(f"/api/statements/{sid}", json={"status": status})


def test_statement_lifecycle_requires_verification_before_approval(client):
    e = exp(client)
    s = stmt(client, e["id"])
    assert s["status"] == "draft"
    r = set_status(client, s["id"], "approved")
    assert r.status_code == 409 and "Verify" in r.json()["detail"]
    assert set_status(client, s["id"], "verified").status_code == 200
    got = set_status(client, s["id"], "approved").json()
    assert got["statements"][0]["status"] == "approved"


def test_editing_text_sends_statement_back_to_draft(client):
    e = exp(client)
    s = stmt(client, e["id"])
    set_status(client, s["id"], "verified")
    set_status(client, s["id"], "approved")
    got = client.patch(f"/api/statements/{s['id']}", json={"text": "Built a bigger test stand"}).json()["statements"][0]
    assert got["text"] == "Built a bigger test stand" and got["status"] == "draft"
    # Can't sneak an approval in with new wording.
    r = client.patch(f"/api/statements/{s['id']}", json={"text": "Different again", "status": "approved"})
    assert r.status_code == 409
    # Same text is not a change.
    set_status(client, s["id"], "verified")
    got = client.patch(f"/api/statements/{s['id']}", json={"text": "Built a bigger test stand"}).json()["statements"][0]
    assert got["status"] == "verified"


def test_protect_and_tags(client):
    e = exp(client)
    s = stmt(client, e["id"])
    got = client.patch(f"/api/statements/{s['id']}", json={"protected": True, "tags": ["GNC"]}).json()["statements"][0]
    assert got["protected"] is True and got["tags"] == ["GNC"] and got["status"] == "draft"


def test_experience_validation(client):
    assert client.post("/api/experiences", json={"kind": "job", "title": "x"}).status_code == 422
    assert client.post("/api/experiences", json={"kind": "project", "title": " "}).status_code == 422
    assert client.post("/api/experiences", json={"kind": "project", "title": "x", "start_date": "2025-13"}).status_code == 422
    r = client.post("/api/experiences", json={"kind": "project", "title": "x", "start_date": "2025-05", "end_date": "2025-01"})
    assert r.status_code == 422
    e = exp(client, start_date="2025-01")
    assert client.patch(f"/api/experiences/{e['id']}", json={"end_date": "2024-12"}).status_code == 409


def test_experience_patch_and_delete_rules(client):
    e = exp(client, start_date="2025-01", end_date="2025-05")
    got = client.patch(f"/api/experiences/{e['id']}", json={"location": "Mojave, CA", "end_date": None}).json()
    assert got["location"] == "Mojave, CA" and got["end_date"] is None and got["start_date"] == "2025-01"
    s = stmt(client, e["id"])
    assert client.delete(f"/api/experiences/{e['id']}").status_code == 409  # has statements
    assert client.delete(f"/api/statements/{s['id']}").status_code == 204
    assert client.delete(f"/api/experiences/{e['id']}").status_code == 204
    assert client.get("/api/experiences").json() == []


def test_statement_used_in_resume_cannot_be_deleted(client, config):
    e = exp(client)
    s = stmt(client, e["id"])
    conn = db.connect(config.db_path)
    conn.execute(
        "INSERT INTO resume_documents (template_version, page_limit, page_count, content_json, validation_json, folder, "
        "tex_sha256, pdf_sha256, created_at) VALUES ('t', 1, 1, '{}', '{}', 'f', 'a', 'b', 'n')")
    conn.execute("INSERT INTO resume_statements VALUES (1, ?, 0)", (s["id"],))
    conn.close()
    r = client.delete(f"/api/statements/{s['id']}")
    assert r.status_code == 409 and "kept for that record" in r.json()["detail"]
    assert client.get("/api/experiences").json()[0]["statements"][0]["used_in_resume"] is True


def test_generated_documents_are_immutable(client, config):
    conn = db.connect(config.db_path)
    conn.execute(
        "INSERT INTO resume_documents (template_version, page_limit, page_count, content_json, validation_json, folder, "
        "tex_sha256, pdf_sha256, created_at) VALUES ('t', 1, 1, '{}', '{}', 'f', 'a', 'b', 'n')")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE resume_documents SET page_count = 2")
    conn.close()


def test_skills_crud_and_ranking_uses_them(client):
    r = client.post("/api/skills", json={"name": "MATLAB", "category": "Software"})
    assert r.status_code == 201
    assert client.post("/api/skills", json={"name": "matlab"}).status_code == 409  # case-insensitive
    sid = r.json()[0]["id"]
    assert client.patch(f"/api/skills/{sid}", json={"category": "Analysis"}).json()[0]["category"] == "Analysis"
    assert client.get("/api/settings").json()["profile"]["skills"] == ["MATLAB"]
    o = client.post("/api/opportunities", json={"employer": "E", "title": "T", "url": "https://e.com/1",
                                                "description": "Use MATLAB daily."}).json()
    assert o["ranking"]["relevance"]["matched_skills"] == ["MATLAB"]
    assert client.delete(f"/api/skills/{sid}").status_code == 204
    assert client.get("/api/skills").json() == []


def test_profile_skills_field_is_ignored_on_save(client):
    client.post("/api/skills", json={"name": "Python"})
    client.put("/api/settings", json={"profile": {"gpa": 3.5, "skills": ["Injected"]}})
    s = client.get("/api/settings").json()
    assert s["profile"]["skills"] == ["Python"] and s["profile"]["gpa"] == 3.5


def test_settings_put_replaces_only_sent_sections(client):
    client.put("/api/settings", json={"preferred_employers": ["NASA"], "resume_header": {"name": "Ada"}})
    client.put("/api/settings", json={"resume_header": {"name": "Ada L.", "email": "ada@example.com"}})
    s = client.get("/api/settings").json()
    assert s["preferred_employers"] == ["NASA"]
    assert s["resume_header"]["name"] == "Ada L." and s["resume_header"]["email"] == "ada@example.com"
    client.put("/api/settings", json={"preferred_employers": []})
    assert client.get("/api/settings").json()["resume_header"]["name"] == "Ada L."


def test_migration_moves_profile_skills_into_table(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    migrations = db.available_migrations()
    db.migrate(conn, tmp_path / "b", migrations[:2])
    conn.execute("INSERT INTO settings VALUES ('profile', '{\"gpa\": 3.2, \"skills\": [\"MATLAB\", \" \", \"Python\"]}', 'n')")
    db.migrate(conn, tmp_path / "b", migrations)
    assert [r[0] for r in conn.execute("SELECT name FROM skills ORDER BY name")] == ["MATLAB", "Python"]
    from aims import settings_store
    s = settings_store.load(conn)
    assert s.profile.gpa == 3.2 and s.profile.skills == ["MATLAB", "Python"]


def test_experience_survives_restart(config):
    with make_client(config) as c:
        e = exp(c)
        stmt(c, e["id"], "Ran 12 hot fires")
    with make_client(config) as c:
        assert c.get("/api/experiences").json()[0]["statements"][0]["text"] == "Ran 12 hot fires"
