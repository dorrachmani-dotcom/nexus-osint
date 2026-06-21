"""Smoke + unit tests for the investigator features built on the entity index:
entity dossier, contextual pivots, 'needs your eyes' triage, claim verification,
the court-ready evidence manifest, and the multi-case relationship graph.

The route-200 checks specifically guard against the "query selects a column that
doesn't exist / template references a missing var" class of bug that previously
slipped through (it turned the page into a 500).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from nexus import storage as s
from nexus.models import Analysis, RawItem, ThreatLevel


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def _seed(conn) -> tuple[int, int]:
    """A case with one analysed, pinned, evidence-backed item. Returns (case_id, item_id)."""
    cid = s.create_case(conn, "Messi", "Transfer watch", "high")
    s.add_case_term(conn, cid, "Messi")
    iid, _ = s.upsert_item(
        conn,
        RawItem(source="rss", title="Messi and Mbappe at PSG",
                url="https://example.com/messi", content="body text"),
    )
    s.save_analysis(conn, iid, Analysis(
        threat_level=ThreatLevel.HIGH, summary="A big transfer move.",
        entity_groups={
            "people": ["Lionel Messi", "Mbappe"],
            "organizations": ["PSG"],
            "identifiers": ["scout@example.com"],
        },
        entities=[]))
    s.add_bookmark(conn, iid, cid)  # pin into the case (case graph + evidence)
    conn.execute(
        "INSERT INTO evidence (item_id, screenshot, sha256) VALUES (?, ?, ?)",
        (iid, "shots/messi.png", "deadbeef1234"),
    )
    conn.commit()
    return cid, iid


# --- Entity dossier --------------------------------------------------------

def test_entity_dossier_routes(temp_db):
    with temp_db() as conn:
        _seed(conn)
    c = _client()
    assert c.get("/entity", params={"name": "Lionel Messi"}).status_code == 200
    assert c.get("/entity/items", params={"name": "Lionel Messi"}).status_code == 200
    # Unknown entity must degrade to a graceful 200 empty state, not 404/500.
    assert c.get("/entity", params={"name": "Nobody At All"}).status_code == 200


def test_entity_profile_unit(temp_db):
    with temp_db() as conn:
        _seed(conn)
        p = s.entity_profile(conn, "Lionel Messi")
        assert p and p["item_count"] >= 1
        # Co-occurs with Mbappe / PSG in the same item.
        assert p["connections"], "expected co-occurring entities"


# --- 'Needs your eyes' triage ----------------------------------------------

def test_attention_routes(temp_db):
    with temp_db() as conn:
        _seed(conn)
    c = _client()
    assert c.get("/attention").status_code == 200
    assert c.get("/attention/count").status_code == 200


# --- Claim verification ----------------------------------------------------

def test_verify_route_degrades_without_ai(temp_db, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "off")
    from nexus.config import get_settings

    get_settings.cache_clear()
    with temp_db() as conn:
        _, iid = _seed(conn)
    c = _client()
    # No model configured -> a clean 200 with a "needs AI" message, never a 500.
    assert c.post(f"/items/{iid}/verify").status_code == 200
    get_settings.cache_clear()


# --- Evidence manifest -----------------------------------------------------

def test_evidence_manifest_route(temp_db):
    with temp_db() as conn:
        cid, _ = _seed(conn)
    r = _client().get(f"/cases/{cid}/evidence-manifest")
    assert r.status_code == 200
    assert "EVIDENCE MANIFEST" in r.text
    assert "deadbeef1234" in r.text  # the SHA-256 is in the manifest


def test_case_evidence_unit(temp_db):
    with temp_db() as conn:
        cid, _ = _seed(conn)
        ev = s.case_evidence(conn, cid)
        assert len(ev) == 1 and ev[0]["sha256"] == "deadbeef1234"


# --- Multi-case relationship graph -----------------------------------------

def test_graph_routes_and_case_scope(temp_db):
    with temp_db() as conn:
        cid, _ = _seed(conn)
    c = _client()
    assert c.get("/graph").status_code == 200
    assert c.get("/graph/build", params={"case": [cid]}).status_code == 200
    assert c.get(f"/cases/{cid}", params={"tab": "graph"}).status_code == 200


def test_topic_graph_includes_pinned_items(temp_db):
    with temp_db() as conn:
        cid, iid = _seed(conn)
        # A pinned-only scope (no terms) must graph the pinned item, not the feed.
        g = s.topic_entity_graph(conn, extra_item_ids=[iid])
        assert g["item_count"] == 1 and len(g["nodes"]) >= 2


# --- Contextual pivots -----------------------------------------------------

# --- Demo data -------------------------------------------------------------

def test_demo_data_unit(temp_db):
    from nexus.demodata import demo_loaded, load_demo_data

    with temp_db() as conn:
        assert not demo_loaded(conn)
        r = load_demo_data(conn)
        assert r["loaded"] and r["items"] >= 8
    with temp_db() as conn:  # same temp db, fresh connection
        assert demo_loaded(conn)
        assert load_demo_data(conn)["loaded"] is False  # idempotent
        # the demo entities are indexed, so dossiers/graph work immediately
        p = s.entity_profile(conn, "Acme Corp")
        assert p and p["item_count"] >= 1


def test_demo_load_route(temp_db):
    c = _client()
    assert c.post("/demo/load").status_code == 200
    assert c.post("/demo/load").status_code == 200  # idempotent, still 200


# --- Background scan -------------------------------------------------------

def test_scan_status_route(temp_db):
    # The status chip endpoint must always answer (idle when nothing has run).
    assert _client().get("/scan/status").status_code == 200


def test_scanstate_tracker_unit():
    import time

    from nexus import scanstate

    class _FakeCollector:
        def scan(self):
            return {"_update": {"new": 3}}

    scanstate.start(_FakeCollector())
    for _ in range(60):  # background thread finishes near-instantly
        if scanstate.state()["status"] != "running":
            break
        time.sleep(0.05)
    st = scanstate.state()
    assert st["status"] == "done" and st["new_items"] == 3


def test_pivot_mapping_unit():
    from nexus.toolguide import detect_identifier_type, pivot_tools_for

    assert detect_identifier_type("scout@example.com") == "email"
    assert detect_identifier_type("example.com") == "domain"
    assert detect_identifier_type("+15551234567") == "phone"
    assert pivot_tools_for("scout@example.com")[0] == "email"
    # A plain org/person name offers no pivot unless typed as an identifier.
    assert pivot_tools_for("Google", "organization") == (None, [])
