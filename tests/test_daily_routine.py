"""The analyst daily-routine pack: 'new since last visit' per case, the Daily
Brief, mark-all-read, and the set-up readiness checklist.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from nexus import storage as s
from nexus.models import RawItem


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def _seed(conn) -> int:
    cid = s.create_case(conn, "Neymar")
    s.add_case_term(conn, cid, "Neymar")
    s.upsert_item(conn, RawItem(source="rss", title="Neymar breaking", content="x"))
    s.upsert_item(conn, RawItem(source="rss", title="Neymar transfer", content="y"))
    return cid


# --- storage: new-since-visit ----------------------------------------------

def test_new_count_then_reset_on_visit(temp_db):
    with temp_db() as conn:
        cid = _seed(conn)
        assert s.case_new_count(conn, cid) == 2          # nothing seen yet
        assert len(s.case_new_items(conn, cid)) == 2
        s.touch_case_visit(conn, cid)                    # "opened the case"
        assert s.case_new_count(conn, cid) == 0          # caught up
        # A fresh item after the visit counts as new again.
        s.upsert_item(conn, RawItem(source="rss", title="Neymar update", content="z"))
        assert s.case_new_count(conn, cid) == 1


def test_mark_case_read(temp_db):
    with temp_db() as conn:
        cid = _seed(conn)
        assert s.count_matching_items(conn, terms=["Neymar"], unread_only=True) == 2
        n = s.mark_case_read(conn, cid)
        assert n == 2
        assert s.count_matching_items(conn, terms=["Neymar"], unread_only=True) == 0


# --- routes: board badge, brief, read-all ----------------------------------

def test_board_shows_new_badge(temp_db):
    with temp_db() as conn:
        _seed(conn)
    assert "+2 new" in _client().get("/cases").text


def test_brief_lists_new_then_caught_up(temp_db):
    with temp_db() as conn:
        cid = _seed(conn)

    c = _client()
    brief = c.get("/brief").text
    assert "Daily brief" in brief
    assert "Neymar breaking" in brief
    assert "new item" in brief

    c.get(f"/cases/{cid}")  # opening the case marks it seen
    assert "caught up" in c.get("/brief").text.lower()


def test_read_all_route(temp_db):
    with temp_db() as conn:
        cid = _seed(conn)
    r = _client().post(f"/cases/{cid}/read-all")
    assert r.status_code == 200
    with temp_db() as conn:
        assert s.count_matching_items(conn, terms=["Neymar"], unread_only=True) == 0


def test_brief_readiness_checklist(temp_db):
    # With no cases at all, the brief surfaces a 'Create a case' set-up item.
    with temp_db():
        body = _client().get("/brief").text
    assert "Set-up checklist" in body
    assert "Create a case" in body


def test_brief_nav_link_present(temp_db):
    with temp_db():
        assert "/brief" in _client().get("/").text


def test_brief_count_endpoint(temp_db):
    with temp_db() as conn:
        _seed(conn)  # 2 tracked items, unseen
    c = _client()
    r = c.get("/brief/count")
    assert r.status_code == 200 and r.json()["total"] == 2
    # The nav badge + poll are wired into every page.
    page = c.get("/").text
    assert 'id="brief-badge"' in page and "/brief/count" in page
