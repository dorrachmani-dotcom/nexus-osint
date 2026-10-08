"""The app serves a real .ico favicon so the desktop app window/taskbar shows
the Nexus mark instead of a generic browser glyph. The icon ships inside the
(bundled) templates dir, so this works in dev and in the frozen installer.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def test_favicon_is_a_real_ico():
    r = _client().get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"
    # ICO files start with the 00 00 01 00 magic.
    assert r.content[:4] == b"\x00\x00\x01\x00"


def test_pages_reference_the_ico_favicon(temp_db):
    # temp_db gives the page render an initialised schema; without it the "/"
    # route reads an empty data/nexus.db (no tables) on a fresh checkout.
    assert '/favicon.ico' in _client().get("/").text


def test_health_reports_started_at():
    # The desktop launcher uses /health.started_at (UTC ISO) to detect a stale
    # server and restart it. It must be present and ISO-parseable.
    import datetime

    h = _client().get("/health").json()
    assert h["status"] == "ok"
    datetime.datetime.fromisoformat(h["started_at"])  # raises if malformed
