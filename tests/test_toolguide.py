"""The OSINT-tool catalogue: powers the install guide and Sherlock's tool tips."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nexus.toolguide import TOOL_CATALOG, recommend_tools, tools_brief


def test_catalogue_entries_are_complete():
    assert len(TOOL_CATALOG) >= 12
    for t in TOOL_CATALOG:
        for field in ("key", "title", "what", "who", "install", "example", "triggers"):
            assert t.get(field) or field == "note", f"{t.get('key')} missing {field}"


def test_recommend_maps_question_to_tool():
    assert "holehe" in [t["key"] for t in recommend_tools("which sites is this email on")]
    assert {"sherlock", "maigret"} & {t["key"] for t in recommend_tools("look up this username")}
    assert "phoneinfoga" in [t["key"] for t in recommend_tools("who owns this phone number")]
    assert "onionsearch" in [t["key"] for t in recommend_tools("check the dark web for this brand")]
    assert recommend_tools("what's the weather") == []  # unrelated -> no match


def test_tools_brief_lists_install_steps():
    brief = tools_brief()
    assert "pipx install holehe" in brief
    assert "Maigret" in brief


def test_tools_page_shows_install_guide(temp_db):
    with temp_db():
        body = TestClient(__import__("nexus.web.app", fromlist=["app"]).app,
                          base_url="http://127.0.0.1").get("/tools").text
    assert "How to install these tools" in body
    assert "pipx install holehe" in body
    assert "Docker" in body  # the easiest-path callout


def test_sherlock_knows_about_tools():
    from nexus.assistant import _tools_reference
    ref = _tools_reference()
    assert "Maigret" in ref and "open_page" in ref
