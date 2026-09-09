import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fake_stac import FakeStacServer  # noqa: E402


@pytest.fixture
def stac_server():
    server = FakeStacServer()
    url = server.start()
    server.url = url
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "catalog.sqlite"
