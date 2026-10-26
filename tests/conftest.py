import pytest
from fastapi.testclient import TestClient

from aims.config import Config
from aims.web.app import create_app

PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "data", port=PORT)


def make_client(config, **headers):
    """A fresh app on the given data dir; building a second one simulates a restart."""
    return TestClient(create_app(config), base_url=BASE_URL, headers={"X-AIMS-Request": "1", **headers})


@pytest.fixture
def client(config):
    with make_client(config) as c:
        yield c
