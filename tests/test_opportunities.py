import json

from tests.conftest import make_client


def job(n=1, **kw):
    base = {"employer": f"Employer {n}", "title": f"Engineering Intern {n}", "url": f"https://jobs.example.com/{n}"}
    return {**base, **kw}


def add(client, **kw):
    r = client.post("/api/opportunities", json=job(**kw))
    assert r.status_code == 201, r.text
    return r.json()


def ids(client, **params):
    r = client.get("/api/opportunities", params=params)
    assert r.status_code == 200, r.text
    return [o["id"] for o in r.json()]


# ---------------------------------------------------------------- search & filters

def test_search_matches_all_words_across_fields(client):
    a = add(client, n=1, employer="Blue Origin", location="Kent, WA", description="Turbopump testing")
    b = add(client, n=2, employer="Rocket Lab", location="Long Beach, CA", description="Avionics")
    assert ids(client, q="origin") == [a["id"]]
    assert ids(client, q="turbopump kent") == [a["id"]]
    assert ids(client, q="turbopump beach") == []
    assert set(ids(client, q="")) == {a["id"], b["id"]}


def test_search_treats_like_wildcards_literally(client):
    add(client, n=1, title="100% remote")
    add(client, n=2, title="Onsite")
    assert len(ids(client, q="%")) == 1
    assert ids(client, q="_") == []


def test_state_and_type_filters(client):
    a = add(client, n=1, opportunity_type="co-op")
    b = add(client, n=2)
    client.patch(f"/api/opportunities/{b['id']}", json={"user_state": "dismissed"})
    assert ids(client) == [a["id"]]  # default "active" hides dismissed
    assert ids(client, state="dismissed") == [b["id"]]
    assert set(ids(client, state="all")) == {a["id"], b["id"]}
    assert ids(client, state="all", opportunity_type="co-op") == [a["id"]]


def test_eligibility_filter_and_rank_sort(client):
    client.put("/api/settings", json={"profile": {"us_citizen": False}, "disciplines": ["Aerospace"]})
    ineligible = add(client, n=1, description="Must be a U.S. citizen. Aerospace.")
    eligible = add(client, n=2, description="Aerospace design work.")
    uncertain = add(client, n=3, description="")
    assert ids(client, sort="rank") == [eligible["id"], uncertain["id"], ineligible["id"]]
    assert ids(client, eligibility="ineligible") == [ineligible["id"]]
    # Ineligible is ranked lower, never hidden by default.
    assert ineligible["id"] in ids(client)


def test_deadline_sort_puts_missing_last(client):
    late = add(client, n=1, deadline="2027-03-01")
    none = add(client, n=2)
    soon = add(client, n=3, deadline="2026-11-15")
    assert ids(client, sort="deadline") == [soon["id"], late["id"], none["id"]]


def test_list_includes_ranking_but_not_description(client):
    add(client, n=1, description="secret long text")
    row = client.get("/api/opportunities").json()[0]
    assert "description" not in row
    assert {"eligibility", "score", "relevance", "preference"} <= row.keys()


def test_detail_includes_explanation(client):
    o = add(client, n=1, description="Minimum GPA of 3.0 required.")
    r = client.get(f"/api/opportunities/{o['id']}").json()["ranking"]
    assert r["eligibility"]["status"] == "uncertain"
    assert r["eligibility"]["findings"][0]["rule"] == "gpa"


def test_invalid_filter_rejected(client):
    assert client.get("/api/opportunities", params={"state": "bogus"}).status_code == 422


# ---------------------------------------------------------------- save / dismiss / edit

def test_save_dismiss_restore_and_notes(client):
    o = add(client, n=1)
    path = f"/api/opportunities/{o['id']}"
    assert client.patch(path, json={"user_state": "saved"}).json()["user_state"] == "saved"
    assert client.patch(path, json={"user_state": "dismissed"}).json()["user_state"] == "dismissed"
    assert client.patch(path, json={"user_state": "new"}).json()["user_state"] == "new"
    got = client.patch(path, json={"user_notes": "Referral from Sam"}).json()
    assert got["user_notes"] == "Referral from Sam" and got["user_state"] == "new"


def test_patch_only_changes_given_fields(client):
    o = add(client, n=1, location="Denver, CO", deadline="2026-12-01", user_notes="keep me")
    got = client.patch(f"/api/opportunities/{o['id']}", json={"title": "Renamed"}).json()
    assert got["title"] == "Renamed" and got["location"] == "Denver, CO"
    assert got["deadline"] == "2026-12-01" and got["user_notes"] == "keep me"
    cleared = client.patch(f"/api/opportunities/{o['id']}", json={"deadline": None}).json()
    assert cleared["deadline"] is None


def test_edit_url_rechecks_duplicates(client):
    a = add(client, n=1)
    b = add(client, n=2)
    r = client.patch(f"/api/opportunities/{b['id']}", json={"url": a["url"] + "/"})
    assert r.status_code == 409 and r.json()["existing_id"] == a["id"]
    assert client.get(f"/api/opportunities/{b['id']}").json()["url"] == b["url"]
    # Changing to its own URL variant is fine.
    assert client.patch(f"/api/opportunities/{b['id']}", json={"url": b["url"] + "?utm_source=x"}).status_code == 200


def test_discovered_postings_not_editable(client, config):
    from aims import db

    conn = db.connect(config.db_path)
    conn.execute(
        "INSERT INTO opportunities (source, source_key, source_posting_id, employer, title, url, url_key, "
        "first_seen_at, last_seen_at, created_at, updated_at) "
        "VALUES ('greenhouse', 'board', '42', 'E', 'T', 'https://x/1', 'https://x/1', 'n', 'n', 'n', 'n')"
    )
    conn.close()
    oid = client.get("/api/opportunities").json()[0]["id"]
    assert client.patch(f"/api/opportunities/{oid}", json={"title": "Hacked"}).status_code == 409
    # User-owned fields stay editable.
    assert client.patch(f"/api/opportunities/{oid}", json={"user_state": "saved", "user_notes": "x"}).status_code == 200


def test_patch_rejects_unknown_and_invalid(client):
    o = add(client, n=1)
    path = f"/api/opportunities/{o['id']}"
    assert client.patch(path, json={"source": "greenhouse"}).status_code == 422
    assert client.patch(path, json={"url": "javascript:alert(1)"}).status_code == 422
    assert client.patch(path, json={"user_state": "deleted"}).status_code == 422
    assert client.patch("/api/opportunities/999", json={"user_state": "saved"}).status_code == 404


# ---------------------------------------------------------------- import

def test_csv_import_reports_each_row(client):
    existing = add(client, n=1)
    csv_text = (
        "﻿employer,title,url,location,deadline\n"
        "Acme,Intern A,https://acme.com/a,\"Austin, TX\",2026-12-01\n"
        f"Dup,Dup,{existing['url']}/,,\n"
        "Bad,Bad,ftp://nope,,\n"
        "Acme,Intern B,https://acme.com/b,,\n"
    )
    res = client.post("/api/opportunities/import", json={"format": "csv", "content": csv_text}).json()
    assert res["counts"] == {"created": 2, "duplicate": 1, "invalid": 1}
    assert [r["result"] for r in res["rows"]] == ["created", "duplicate", "invalid", "created"]
    assert res["rows"][1]["id"] == existing["id"]
    created = client.get(f"/api/opportunities/{res['rows'][0]['id']}").json()
    assert created["location"] == "Austin, TX" and created["deadline"] == "2026-12-01"


def test_reimport_is_idempotent(client):
    content = json.dumps([job(n=i) for i in range(5)])
    first = client.post("/api/opportunities/import", json={"format": "json", "content": content}).json()
    second = client.post("/api/opportunities/import", json={"format": "json", "content": content}).json()
    assert first["counts"]["created"] == 5 and second["counts"] == {"created": 0, "duplicate": 5, "invalid": 0}
    assert len(ids(client, state="all")) == 5


def test_import_never_modifies_existing_record(client):
    o = add(client, n=1, user_notes="mine")
    client.patch(f"/api/opportunities/{o['id']}", json={"user_state": "saved"})
    content = json.dumps([job(n=1, title="Changed", user_notes="theirs")])
    client.post("/api/opportunities/import", json={"format": "json", "content": content})
    got = client.get(f"/api/opportunities/{o['id']}").json()
    assert got["title"] == o["title"] and got["user_notes"] == "mine" and got["user_state"] == "saved"


def test_import_rejects_bad_format(client):
    post = lambda fmt, content: client.post("/api/opportunities/import", json={"format": fmt, "content": content})
    assert post("csv", "employer,title,url,salary\nA,B,https://a.com,1\n").status_code == 400
    assert post("json", "{not json").status_code == 400
    assert post("json", '{"employer": "A"}').status_code == 400
    assert post("csv", "employer,title,url\nA,B,https://a.com,extra\n").status_code == 400
    assert ids(client, state="all") == []


def test_ranking_survives_restart(config):
    with make_client(config) as c:
        c.put("/api/settings", json={"profile": {"us_citizen": True}})
        c.post("/api/skills", json={"name": "MATLAB"})
        o = add(c, n=1, description="Must be a U.S. citizen. MATLAB.")
    with make_client(config) as c:
        r = c.get(f"/api/opportunities/{o['id']}").json()["ranking"]
        assert r["eligibility"]["status"] == "eligible" and r["relevance"]["matched_skills"] == ["MATLAB"]


def test_profile_validation(client):
    assert client.put("/api/settings", json={"profile": {"us_citizen": True, "us_person": False}}).status_code == 422
    assert client.put("/api/settings", json={"profile": {"gpa": 4.5}}).status_code == 422
    assert client.put("/api/settings", json={"profile": {"graduation": "2027-13"}}).status_code == 422
    assert client.put("/api/settings", json={"profile": {"graduation": "2027-05"}}).status_code == 200
