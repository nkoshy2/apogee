import sqlite3

import pytest

from aims import db
from tests.conftest import make_client


@pytest.fixture
def opp(client):
    r = client.post("/api/opportunities", json={
        "employer": "Acme Aero", "title": "GNC Intern", "url": "https://acme.com/gnc", "deadline": "2026-12-15"})
    return r.json()


def start(client, opp_id, **kw):
    r = client.post("/api/applications", json={"opportunity_id": opp_id, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_defaults_and_saves_opportunity(client, opp):
    a = start(client, opp["id"])
    assert a["status"] == "preparing" and a["deadline"] == "2026-12-15"
    assert a["submitted_on"] is None  # never assumed
    assert a["employer"] == "Acme Aero" and a["title"] == "GNC Intern"
    assert [(e["from_status"], e["to_status"]) for e in a["events"]] == [(None, "preparing")]
    o = client.get(f"/api/opportunities/{opp['id']}").json()
    assert o["user_state"] == "saved" and o["application_id"] == a["id"] and o["application_status"] == "preparing"


def test_one_application_per_opportunity(client, opp):
    a = start(client, opp["id"])
    r = client.post("/api/applications", json={"opportunity_id": opp["id"]})
    assert r.status_code == 409 and r.json()["existing_id"] == a["id"]


def test_unknown_opportunity(client):
    assert client.post("/api/applications", json={"opportunity_id": 999}).status_code == 404


def test_status_changes_recorded_in_history(client, opp):
    a = start(client, opp["id"])
    path = f"/api/applications/{a['id']}"
    client.patch(path, json={"status": "submitted", "submitted_on": "2026-11-01", "event_note": "Applied via portal"})
    client.patch(path, json={"status": "interviewing"})  # skipping "assessment" is allowed
    client.patch(path, json={"status": "interviewing", "event_note": "Second round scheduled"})  # note only
    client.patch(path, json={"notes": "Prep orbital mechanics"})  # no history entry
    got = client.get(path).json()
    assert got["status"] == "interviewing" and got["submitted_on"] == "2026-11-01"
    assert got["notes"] == "Prep orbital mechanics"
    assert [(e["from_status"], e["to_status"], e["note"]) for e in got["events"]] == [
        (None, "preparing", "Application created"),
        ("preparing", "submitted", "Applied via portal"),
        ("submitted", "interviewing", ""),
        ("interviewing", "interviewing", "Second round scheduled"),
    ]


def test_backwards_transition_allowed(client, opp):
    a = start(client, opp["id"])
    path = f"/api/applications/{a['id']}"
    client.patch(path, json={"status": "rejected"})
    assert client.patch(path, json={"status": "interviewing", "event_note": "Reopened"}).json()["status"] == "interviewing"


def test_note_endpoint(client, opp):
    a = start(client, opp["id"])
    got = client.post(f"/api/applications/{a['id']}/events", json={"note": "Recruiter call"}).json()
    assert got["events"][-1] == {**got["events"][-1], "from_status": "preparing", "to_status": "preparing", "note": "Recruiter call"}
    assert client.post(f"/api/applications/{a['id']}/events", json={"note": "  "}).status_code == 422


def test_invalid_updates(client, opp):
    a = start(client, opp["id"])
    path = f"/api/applications/{a['id']}"
    assert client.patch(path, json={"status": "hired"}).status_code == 422
    assert client.patch(path, json={"opportunity_id": 5}).status_code == 422
    assert client.patch("/api/applications/999", json={"status": "offer"}).status_code == 404


def test_list_filters_and_order(client):
    made = []
    for i, deadline in enumerate(["2027-01-10", None, "2026-12-01"]):
        o = client.post("/api/opportunities", json={"employer": "E", "title": f"T{i}", "url": f"https://e.com/{i}",
                                                     "deadline": deadline}).json()
        made.append(start(client, o["id"]))
    client.patch(f"/api/applications/{made[0]['id']}", json={"status": "withdrawn"})
    active = client.get("/api/applications", params={"status": "active"}).json()
    assert [a["id"] for a in active] == [made[2]["id"], made[1]["id"]]  # by deadline, none last
    closed = client.get("/api/applications", params={"status": "closed"}).json()
    assert [a["id"] for a in closed] == [made[0]["id"]]
    assert len(client.get("/api/applications").json()) == 3


def test_history_is_append_only_and_link_is_fixed(client, config, opp):
    a = start(client, opp["id"])
    other = client.post("/api/opportunities", json={"employer": "X", "title": "Y", "url": "https://x.com/1"}).json()
    conn = db.connect(config.db_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE application_events SET note = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM application_events")
    with pytest.raises(sqlite3.IntegrityError, match="different opportunity"):
        conn.execute("UPDATE applications SET opportunity_id = ? WHERE id = ?", (other["id"], a["id"]))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM opportunities WHERE id = ?", (opp["id"],))
    conn.close()


def test_applications_survive_restart(config):
    with make_client(config) as c:
        o = c.post("/api/opportunities", json={"employer": "E", "title": "T", "url": "https://e.com/1"}).json()
        a = c.post("/api/applications", json={"opportunity_id": o["id"]}).json()
        c.patch(f"/api/applications/{a['id']}", json={"status": "submitted", "submitted_on": "2026-11-02"})
    with make_client(config) as c:
        got = c.get(f"/api/applications/{a['id']}").json()
        assert got["status"] == "submitted" and len(got["events"]) == 2
