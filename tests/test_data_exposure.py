"""The data folder (database, uploads, logs, backups) is never served over HTTP.

Only evidence screenshots are reachable, by one explicit route that accepts a
flat image file name inside ``data/evidence``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus.config import get_settings


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def data_dir(temp_db) -> Path:
    root = Path(get_settings().data_dir)
    (root / "evidence").mkdir(parents=True, exist_ok=True)
    (root / "uploads").mkdir(parents=True, exist_ok=True)
    (root / "uploads" / "a.bin").write_bytes(b"secret upload")
    (root / ".env").write_text("GEMINI_API_KEY=x", encoding="utf-8")
    (root / "server.log").write_text("log", encoding="utf-8")
    (root / "evidence" / "notes.txt").write_text("not an image", encoding="utf-8")
    (root / "evidence" / "item1_20260101000000.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return root


@pytest.mark.parametrize(
    "path",
    [
        "/data/nexus.db",
        "/data/test.db",
        "/data/.env",
        "/data/server.log",
        "/data/uploads/a.bin",
        "/data/",
        "/data/evidence/",
        "/data/evidence/notes.txt",
        "/data/evidence/../test.db",
        "/data/evidence/..%2ftest.db",
        "/data/evidence/..%5ctest.db",
        "/data/evidence/%2e%2e%2ftest.db",
        "/data/evidence/sub%2fitem1_20260101000000.png",
        "/data/evidence/.hidden.png",
        "/data/evidence/missing.png",
    ],
)
def test_data_folder_not_served(data_dir, path):
    r = _client().get(path)
    assert r.status_code == 404, path
    assert b"secret upload" not in r.content
    assert b"SQLite format" not in r.content


def test_evidence_screenshot_still_served(data_dir):
    r = _client().get("/data/evidence/item1_20260101000000.png")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_static_assets_still_load(temp_db):
    c = _client()
    assert c.get("/static/vendor/htmx.min.js").status_code == 200
    assert c.get("/static/css/app.css").status_code == 200
