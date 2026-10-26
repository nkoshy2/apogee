from tests.conftest import make_client

JOB = {
    "employer": "Example Aerospace",
    "title": "Propulsion Engineering Intern",
    "url": "https://boards.example.com/jobs/123?utm_source=x",
    "location": "Hawthorne, CA",
    "deadline": "2026-12-01",
    "description": "Test-stand work.",
    "user_notes": "Ask about summer 2027.",
}


def test_opportunity_and_settings_survive_restart(config):
    with make_client(config) as c:
        created = c.post("/api/opportunities", json=JOB)
        assert created.status_code == 201, created.text
        opp_id = created.json()["id"]
        assert c.put("/api/settings", json={"preferred_locations": ["  Hawthorne, CA ", "Remote"]}).status_code == 200

    with make_client(config) as c:  # restart
        got = c.get(f"/api/opportunities/{opp_id}").json()
        assert got["title"] == JOB["title"]
        assert got["user_notes"] == JOB["user_notes"]
        assert got["deadline"] == "2026-12-01"
        assert got["source"] == "manual" and got["user_state"] == "new"
        assert [o["id"] for o in c.get("/api/opportunities").json()] == [opp_id]
        assert c.get("/api/settings").json()["preferred_locations"] == ["Hawthorne, CA", "Remote"]


def test_duplicate_url_rejected_without_creating_record(client):
    first = client.post("/api/opportunities", json=JOB).json()
    variant = dict(JOB, url="http://BOARDS.example.com/jobs/123/#apply", title="Different title")
    dup = client.post("/api/opportunities", json=variant)
    assert dup.status_code == 409
    assert dup.json()["existing_id"] == first["id"]
    assert len(client.get("/api/opportunities").json()) == 1


def test_different_query_identifies_different_posting(client):
    assert client.post("/api/opportunities", json=dict(JOB, url="https://x.com/apply?id=1")).status_code == 201
    assert client.post("/api/opportunities", json=dict(JOB, url="https://x.com/apply?id=2")).status_code == 201


def test_invalid_input_rejected(client):
    for bad in (
        dict(JOB, url="javascript:alert(1)"),
        dict(JOB, url="file:///C:/Windows"),
        dict(JOB, employer="   "),
        dict(JOB, opportunity_type="bogus"),
        dict(JOB, deadline="not-a-date"),
        dict(JOB, unexpected="x"),
    ):
        assert client.post("/api/opportunities", json=bad).status_code == 422, bad
    assert client.get("/api/opportunities").json() == []
    assert client.put("/api/settings", json={"nope": []}).status_code == 422


def test_missing_opportunity_is_404(client):
    assert client.get("/api/opportunities/999").status_code == 404


def test_health(client):
    h = client.get("/api/health").json()
    assert h["app"] == "aims" and h["database"] == "ok" and h["schema_version"] >= 1
