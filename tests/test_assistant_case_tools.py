"""Sherlock's case-hub actions (Phase 4): add tracking words, add a case
question, and create a sub-case — all constructive, local, whitelist-dispatched.
"""

from __future__ import annotations

from nexus import storage as s
from nexus.assistant import (
    _ALLOWED_TOOLS,
    _do_add_case_question,
    _do_add_case_term,
    _do_create_subcase,
)


def _ctx():
    return {"last_case_id": None, "by_name": {}}


def test_new_case_tools_are_whitelisted():
    for tool in ("add_case_term", "add_case_question", "create_subcase"):
        assert tool in _ALLOWED_TOOLS


def test_add_case_term_action(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        r = _do_add_case_term(conn, _ctx(), {"case": "Neymar", "terms": "Neymar, World Cup"})
        assert r["type"] == "case_term"
        assert {t["term"] for t in s.case_terms(conn, cid)} == {"Neymar", "World Cup"}


def test_add_case_question_action(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        r = _do_add_case_question(conn, _ctx(), {"case": "Neymar", "question": "Injury status?"})
        assert r["type"] == "requirement"
        qs = [q["question"] for q in s.list_requirements(conn, case_id=cid)]
        assert "Injury status?" in qs


def test_create_subcase_action(temp_db):
    with temp_db() as conn:
        pid = s.create_case(conn, "Neymar")
        r = _do_create_subcase(conn, _ctx(), {"parent": "Neymar", "name": "Transfer rumors"})
        assert r["type"] == "case_created"
        subs = s.list_cases(conn, parent_id=pid)
        assert any(c["name"] == "Transfer rumors" for c in subs)


def test_case_tools_need_a_target(temp_db):
    # With no case and no name/terms, the tools return a friendly error, not a crash.
    with temp_db() as conn:
        s.create_case(conn, "Existing")  # active/last not set in fresh ctx
        assert _do_add_case_term(conn, _ctx(), {"case": "Existing", "terms": ""})["type"] == "error"
        assert _do_add_case_question(conn, _ctx(), {"case": "Existing", "question": ""})["type"] == "error"
        assert _do_create_subcase(conn, _ctx(), {"parent": "Existing", "name": ""})["type"] == "error"
