"""Opening a case auto-creates its tracking words (the capsule), so a new case
has a working live feed with no separate step — in the form and via Sherlock."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nexus import storage as s
from nexus.casesetup import _split_name, auto_setup_case


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def test_split_name_into_search_terms():
    assert _split_name("Messi & World Cup") == ["Messi", "World Cup", "Messi & World Cup"]
    assert "injury" in _split_name("Neymar transfer and injury")
    assert _split_name("") == []


def test_auto_setup_seeds_terms_without_ai(temp_db):
    # No AI provider in the test env -> at least the name-derived words are added.
    with temp_db() as conn:
        cid = s.create_case(conn, "Messi & World Cup")
        summary = auto_setup_case(conn, cid, "Messi & World Cup")
        terms = {t["term"] for t in s.case_terms(conn, cid)}
        assert "Messi" in terms and "World Cup" in terms
        assert summary["terms"] >= 2


def test_creating_a_case_via_form_auto_links_words(temp_db):
    with temp_db():
        _client().post("/cases", data={"name": "Mbappe", "priority": "medium"})
    with temp_db() as conn:
        cid = next(c["id"] for c in s.list_cases(conn) if c["name"] == "Mbappe")
        assert any(t["term"] == "Mbappe" for t in s.case_terms(conn, cid))


def test_sherlock_create_case_auto_links_words(temp_db):
    from nexus.assistant import _do_create_case

    with temp_db() as conn:
        r = _do_create_case(conn, {"last_case_id": None, "by_name": {}},
                            {"name": "Ronaldo"})
        assert r["type"] == "case_created"
        cid = r["case_id"]
        assert any(t["term"] == "Ronaldo" for t in s.case_terms(conn, cid))
