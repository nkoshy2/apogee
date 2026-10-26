from aims.web.app import STATIC_DIR
from tests.conftest import BASE_URL, make_client

JOB = {"employer": "E", "title": "T", "url": "https://e.com/j/1"}


def test_rejects_foreign_host_header(config):
    with make_client(config) as c:
        assert c.get("/api/health", headers={"Host": "evil.example:8765"}).status_code == 400


def test_mutation_requires_custom_header(config):
    with make_client(config) as c:
        assert c.post("/api/opportunities", json=JOB, headers={"X-AIMS-Request": ""}).status_code == 403
        assert c.get("/api/opportunities").json() == []


def test_mutation_rejects_cross_origin(config):
    with make_client(config, Origin="https://evil.example") as c:
        assert c.post("/api/opportunities", json=JOB).status_code == 403
    with make_client(config, Origin=BASE_URL) as c:
        assert c.post("/api/opportunities", json=JOB).status_code == 201


def test_no_cors_grant(client):
    r = client.options(
        "/api/opportunities",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in r.headers


def test_security_headers_and_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Internship Manager" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_shutdown_without_server_is_conflict(client):
    assert client.post("/api/shutdown").status_code == 409


def test_frontend_never_assigns_innerhtml():
    assert ".innerHTML" not in (STATIC_DIR / "app.js").read_text(encoding="utf-8")
