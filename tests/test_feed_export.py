"""Tests for the full-feed export (/export) — HTML and PDF.

The export must reflect "what you see is what you export": it runs the same
filtered query as the home feed and renders every matching item (with its AI
analysis) into a self-contained report. PDF generation degrades gracefully to
HTML when no PDF backend is installed, so the route must never hard-fail.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from nexus.models import Analysis, RawItem, ThreatLevel
from nexus.storage import save_analysis, upsert_item


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
            entity_groups={"people": ["Alice"], "organizations": ["Acme"]},
            entities=[],
        ),
    )


def test_export_html_includes_items_and_analysis(temp_db):
    with temp_db() as conn:
        _seed(conn)

    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.get("/export", params={"format": "html", "scope": "all"})
    assert resp.status_code == 200
    body = resp.text
    assert "Breaking development" in body      # the item title
    assert "Concise AI summary" in body         # AI summary
    assert "English translation" in body        # translation
    assert "Alice" in body and "Acme" in body   # entities
    assert "Intelligence feed export" in body   # report heading


def test_export_pdf_or_html_fallback(temp_db):
    with temp_db() as conn:
        _seed(conn)

    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.get("/export", params={"scope": "all"})
    assert resp.status_code == 200
    ctype = resp.headers.get("content-type", "")
    # A PDF backend produces application/pdf; with none installed the route
    # falls back to serving the HTML report. Both are acceptable.
    assert ctype.startswith("application/pdf") or ctype.startswith("text/html")
    if ctype.startswith("application/pdf"):
        assert resp.content[:4] == b"%PDF"


def test_export_respects_source_filter(temp_db):
    with temp_db() as conn:
        _seed(conn)
        upsert_item(
            conn,
            RawItem(source="reddit", title="Unrelated chatter", content="noise"),
        )

    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.get(
        "/export", params={"format": "html", "scope": "all", "source": "rss"}
    )
    assert resp.status_code == 200
    assert "Breaking development" in resp.text
    assert "Unrelated chatter" not in resp.text


def test_export_empty_feed_does_not_crash(temp_db):
    with temp_db() as conn:
        assert conn is not None  # fresh empty schema

    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.get("/export", params={"format": "html", "scope": "all"})
    assert resp.status_code == 200
    assert "No items match" in resp.text


# --- machine-readable data exports (CSV / JSON) ----------------------------

def test_export_csv_is_a_spreadsheet_download(temp_db):
    with temp_db() as conn:
        _seed(conn)

    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.get("/export", params={"format": "csv", "scope": "all"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disp = resp.headers["content-disposition"]
    assert "attachment" in disp and ".csv" in disp
    body = resp.text
    # Header row + the seeded item's data.
    assert "title" in body.splitlines()[0]
    assert "Breaking development" in body
    assert "Concise AI summary" in body


def test_export_json_is_a_parseable_array(temp_db):
    import json

    with temp_db() as conn:
        _seed(conn)

    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.get("/export", params={"format": "json", "scope": "all"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = json.loads(resp.text)
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["title"] == "Breaking development"
    assert data[0]["threat_level"] == "high"


def test_case_report_csv_and_json(temp_db):
    import json

    from nexus.storage import add_bookmark, create_case

    with temp_db() as conn:
        iid, _ = upsert_item(
            conn, RawItem(source="rss", title="Case item", content="body", url="https://e/x")
        )
        case_id = create_case(conn, "Op Test")
        add_bookmark(conn, iid, case_id)

    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    csv_resp = client.get(f"/cases/{case_id}/report", params={"format": "csv"})
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "Case item" in csv_resp.text

    json_resp = client.get(f"/cases/{case_id}/report", params={"format": "json"})
    assert json_resp.status_code == 200
    rows = json.loads(json_resp.text)
    assert any(r["title"] == "Case item" for r in rows)


def test_intel_export_csv_includes_relevance(temp_db):
    # Seed an item scored against a requirement, then export the Intel view.
    with temp_db() as conn:
        iid, _ = upsert_item(
            conn, RawItem(source="rss", title="Border movement", content="troops seen")
        )
        conn.execute(
            "INSERT INTO requirements (question, priority, enabled) VALUES (?, 1, 1)",
            ("Any movement near the border?",),
        )
        rid = conn.execute("SELECT id FROM requirements").fetchone()["id"]
        conn.execute(
            "INSERT INTO requirement_hits (item_id, requirement_id, score, rationale) "
            "VALUES (?, ?, ?, ?)",
            (iid, rid, 92, "Direct mention."),
        )

    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.get("/intel/export", params={"format": "csv", "min_score": 1})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.text
    assert "rel_score" in body.splitlines()[0]   # the relevance column
    assert "Border movement" in body
    assert "92" in body
