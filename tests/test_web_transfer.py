"""Integration tests for the air-gap transfer web routes.

These exercise the real FastAPI app end-to-end (export download, import merge)
and, most importantly, lock in the OpSec safety gate on import: an uploaded file
that the local scanner flags as *dangerous* must be refused before it is ever
read as a bundle, so a poisoned USB stick can't smuggle an executable onto the
air-gapped analysis station. The export must stream a ``.nexusbundle`` ZIP, and a
non-bundle upload must fail gracefully (a friendly error, never a 500).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from nexus.models import Analysis, RawItem, ThreatLevel
from nexus.storage import save_analysis, upsert_item
from nexus.transfer import export_bundle


def _seed(conn) -> None:
    iid, _ = upsert_item(
        conn,
        RawItem(
            source="rss",
            title="Breaking development",
            content="Something significant happened in the region today.",
            url="https://example.com/story",
        ),
    )
    save_analysis(
        conn,
        iid,
        Analysis(
            threat_level=ThreatLevel.HIGH,
            summary="Concise AI summary of the event.",
            translation="English translation of the item.",
            entity_groups={"people": ["Alice"]},
            entities=[],
        ),
    )


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def test_export_route_streams_a_bundle_download(temp_db):
    with temp_db() as conn:
        _seed(conn)

    resp = _client().post("/transfer/export", data={"scope": "all"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    disp = resp.headers["content-disposition"]
    assert "attachment" in disp and ".nexusbundle" in disp
    # The body is a real ZIP (PK signature).
    assert resp.content[:2] == b"PK"


def test_import_route_merges_a_real_bundle(temp_db):
    # Build the bundle, then stand in for the offline machine by clearing the
    # collected rows. The block closes (committing + releasing its write lock)
    # before the request, so the route's own connection isn't blocked.
    with temp_db() as conn:
        _seed(conn)
        blob, _ = export_bundle(conn, scope="all")
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM clusters")

    resp = _client().post(
        "/transfer/import",
        files={"bundle": ("day.nexusbundle", blob, "application/zip")},
    )
    assert resp.status_code == 200

    # The item really landed in the local DB.
    with temp_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM items WHERE title = ?", ("Breaking development",)
        ).fetchone()["c"]
    assert n == 1


def test_import_route_blocks_a_dangerous_upload(temp_db):
    # A poisoned "bundle" that is actually an executable script. The local
    # safety scan must flag it as dangerous and the route must NOT import it.
    poisoned = b"#!/bin/sh\nrm -rf /\n"
    with temp_db() as conn:
        resp = _client().post(
            "/transfer/import",
            files={"bundle": ("evil.nexusbundle", poisoned, "application/octet-stream")},
        )
        assert resp.status_code == 200
        assert "blocked" in resp.text.lower() or "not imported" in resp.text.lower()
        # Nothing was written to the database.
        n = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
        assert n == 0


def test_import_route_handles_non_bundle_gracefully(temp_db):
    # A harmless but non-bundle file: passes the safety scan, then fails the
    # bundle parse with a friendly error rather than a 500.
    with temp_db() as conn:
        resp = _client().post(
            "/transfer/import",
            files={"bundle": ("notes.txt", b"just some plain notes", "text/plain")},
        )
        assert resp.status_code == 200
        assert "import" in resp.text.lower()  # the page re-renders with a message
        n = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
        assert n == 0
