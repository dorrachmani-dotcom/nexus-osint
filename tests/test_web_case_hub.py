"""Integration tests for the unified Case hub web UI (Phase 2).

The Case is the single hub: its detail page has Live-feed / Pinned / Questions /
Sub-cases tabs, and routes to manage tracking words, questions and sub-cases,
run a per-case scan, and export only that case's tracked items ("only Neymar").
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from nexus import storage as s
from nexus.models import RawItem


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def test_all_case_tabs_render(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        s.add_case_term(conn, cid, "Neymar")
        s.add_requirement(conn, "Injury status?", case_id=cid)

    c = _client()
    for tab, marker in [
        ("feed", "Tracking words"),
        ("pinned", "Pinned items"),
        ("questions", "Intelligence questions"),
        ("subcases", "Sub-cases"),
    ]:
        r = c.get(f"/cases/{cid}", params={"tab": tab})
        assert r.status_code == 200
        assert marker in r.text


def test_terms_drive_live_feed(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        s.upsert_item(conn, RawItem(source="rss", title="Neymar scores again", content="x"))
        s.upsert_item(conn, RawItem(source="rss", title="Weather report", content="x"))

    c = _client()
    # Add a tracking word -> live feed now shows only the matching item.
    r = c.post(f"/cases/{cid}/terms", data={"terms": "Neymar"})
    assert r.status_code == 200
    assert "Neymar scores again" in r.text
    assert "Weather report" not in r.text


def test_add_and_delete_question(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")

    c = _client()
    r = c.post(f"/cases/{cid}/questions", data={"question": "Where will he transfer?"})
    assert r.status_code == 200 and "Where will he transfer?" in r.text

    with temp_db() as conn:
        rid = s.list_requirements(conn, case_id=cid)[0]["id"]
    r2 = c.post(f"/cases/{cid}/questions/{rid}/delete")
    assert r2.status_code == 200 and "Where will he transfer?" not in r2.text


def test_create_subcase_one_level(temp_db):
    with temp_db() as conn:
        parent = s.create_case(conn, "Neymar")

    c = _client()
    r = c.post(f"/cases/{parent}/subcases", data={"name": "Transfer rumors"})
    assert r.status_code == 200 and "Transfer rumors" in r.text

    with temp_db() as conn:
        subs = s.list_cases(conn, parent_id=parent)
        assert any(x["name"] == "Transfer rumors" for x in subs)
        sub_id = subs[0]["id"]
        # The sub-case page hides the Sub-cases tab (no nesting).
    r2 = c.get(f"/cases/{sub_id}")
    assert r2.status_code == 200
    assert "?tab=subcases" not in r2.text


def test_live_feed_has_one_click_pin(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        s.add_case_term(conn, cid, "Neymar")
        s.upsert_item(conn, RawItem(source="rss", title="Neymar news", content="x"))

    page = _client().get(f"/cases/{cid}", params={"tab": "feed"})
    assert page.status_code == 200
    assert "Pin to this case" in page.text
    # The same item on the main feed has no current-case pin button.
    assert "Pin to this case" not in _client().get("/", params={"scope": "all"}).text


def test_work_in_this_case_toggle(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")

    c = TestClient(__import__("nexus.web.app", fromlist=["app"]).app,
                   base_url="http://127.0.0.1", follow_redirects=False)
    r = c.post(f"/cases/{cid}/activate")
    assert r.status_code == 303 and r.headers["location"] == f"/cases/{cid}"
    with temp_db() as conn:
        assert s.get_active_case(conn)["id"] == cid

    # The case header reflects the active state.
    page = _client().get(f"/cases/{cid}")
    assert "Working here" in page.text

    c.post(f"/cases/{cid}/deactivate")
    with temp_db() as conn:
        assert s.get_active_case(conn) is None


def test_edit_tracking_word(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        s.add_case_term(conn, cid, "Neymar")
        tid = s.case_terms(conn, cid)[0]["id"]

    r = _client().post(f"/cases/{cid}/terms/{tid}/edit", data={"term": "Neymar Jr"})
    assert r.status_code == 200
    with temp_db() as conn:
        assert [t["term"] for t in s.case_terms(conn, cid)] == ["Neymar Jr"]


def test_foreign_language_word_drives_search(temp_db):
    # A non-Latin tracking word is stored and broadcast to sources as a query,
    # so the case's search covers that language too.
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        s.add_case_term(conn, cid, "نيمار")  # Arabic
        assert "نيمار" in s.all_case_terms(conn)
    from nexus.config import get_settings
    get_settings.cache_clear()
    assert "نيمار" in get_settings().investigation_query_targets()
    get_settings.cache_clear()


def test_edit_term_merges_on_clash(temp_db):
    # Editing a word to equal another existing word removes the duplicate row.
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        s.add_case_term(conn, cid, "Neymar")
        s.add_case_term(conn, cid, "World Cup")
        terms = s.case_terms(conn, cid)
        wc_id = next(t["id"] for t in terms if t["term"] == "World Cup")
        s.update_case_term(conn, cid, wc_id, "Neymar")  # clash
        names = [t["term"] for t in s.case_terms(conn, cid)]
        assert names == ["Neymar"]


def test_investigation_redirects_into_case(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")

    c = TestClient(__import__("nexus.web.app", fromlist=["app"]).app,
                   base_url="http://127.0.0.1", follow_redirects=False)
    r = c.get("/investigation", params={"name": "neymar"})
    assert r.status_code == 302 and r.headers["location"] == f"/cases/{cid}?tab=feed"
    r2 = c.get("/investigation", params={"name": "does-not-exist"})
    assert r2.status_code == 302 and r2.headers["location"] == "/cases"


def test_export_only_this_case(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        s.add_case_term(conn, cid, "Neymar")
        s.upsert_item(conn, RawItem(source="rss", title="Neymar headline", content="x"))
        s.upsert_item(conn, RawItem(source="rss", title="Unrelated", content="x"))

    c = _client()
    r = c.get(f"/cases/{cid}/report", params={"scope": "live", "format": "json"})
    assert r.status_code == 200
    rows = json.loads(r.text)
    titles = [x["title"] for x in rows]
    assert "Neymar headline" in titles
    assert "Unrelated" not in titles


def test_export_tracked_pdf_and_html(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        s.add_case_term(conn, cid, "Neymar")
        s.upsert_item(conn, RawItem(source="rss", title="Neymar tracked headline", content="x"))

    c = _client()
    h = c.get(f"/cases/{cid}/report", params={"scope": "live", "format": "html"})
    assert h.status_code == 200
    assert "Neymar tracked headline" in h.text  # the tracked item is in the report

    p = c.get(f"/cases/{cid}/report", params={"scope": "live", "format": "pdf"})
    assert p.status_code == 200
    ctype = p.headers.get("content-type", "")
    # A PDF backend yields application/pdf; with none it gracefully serves HTML.
    assert ctype.startswith("application/pdf") or ctype.startswith("text/html")
