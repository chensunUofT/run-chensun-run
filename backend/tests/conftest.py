from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        mode="test",
        database_url=f"sqlite:///{(tmp_path / 'runwise-test.db').as_posix()}",
        cors_origins=("http://localhost:5173", "http://127.0.0.1:5173"),
        max_import_bytes=1024 * 1024,
        max_import_rows=100,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client
