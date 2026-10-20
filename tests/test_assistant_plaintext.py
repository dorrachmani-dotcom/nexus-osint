"""Sherlock answers read as clean plain text, and stay inert text (never HTML)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from nexus.textclean import markdown_to_plain

_BASE = Path(__file__).resolve().parents[1] / "nexus" / "web" / "templates" / "base.html"


def test_bold_and_bullets_become_plain_text():
    out = markdown_to_plain("**Announcements:**\n- **Gemini 4** shipped (Item 2)\n* Second point")
    assert out == "Announcements:\n• Gemini 4 shipped (Item 2)\n• Second point"


def test_headings_code_links_and_underscores():
    out = markdown_to_plain("## Summary\nUse `nexus scan` and __this__. See [the docs](https://example.org/x).")
    assert out == "Summary\nUse nexus scan and this. See the docs (https://example.org/x)."


def test_leaves_plain_arithmetic_and_identifiers_alone():
    text = "a * b * c and snake_case_name and 2*3*4"
    assert markdown_to_plain(text) == text


def test_fences_rules_and_blank_runs():
    out = markdown_to_plain("```python\nx = 1\n```\n---\n\n\n\nEnd")
    assert out == "x = 1\n\nEnd"


def test_empty_and_none():
    assert markdown_to_plain("") == ""
    assert markdown_to_plain(None) == ""


def test_html_is_preserved_verbatim_never_interpreted():
    evil = '<img src=x onerror="alert(1)"><script>alert(1)</script>'
    assert markdown_to_plain("**Note:** " + evil) == "Note: " + evil


def test_ask_route_returns_normalised_answer_with_inert_html(temp_db, monkeypatch):
    evil = "<script>alert(1)</script> <img src=x onerror=alert(1)>"

    def fake_act(question, **kwargs):
        return {"ok": True, "answer": "**Hi**\n- one " + evil, "provider": "fake",
                "actions": [], "error": ""}

    monkeypatch.setattr("nexus.assistant.act", fake_act)
    from nexus.web.app import app

    r = TestClient(app, base_url="http://127.0.0.1").post(
        "/assistant/ask", json={"question": "hi"})
    assert r.status_code == 200
    assert r.json()["answer"] == "Hi\n• one " + evil


def test_bot_text_is_rendered_with_text_nodes_only():
    # The client must keep building bot bubbles from text nodes: renderBotText
    # may create <a> for "Item N" but never assigns innerHTML.
    src = _BASE.read_text(encoding="utf-8")
    m = re.search(r"function renderBotText\(.*?\n      }\n", src, re.S)
    assert m, "renderBotText not found"
    body = m.group(0)
    assert "createTextNode" in body
    assert "innerHTML" not in body


def _script() -> str:
    return _BASE.read_text(encoding="utf-8")


def test_chat_is_kept_for_the_tab_session_and_cleared_on_end():
    src = _script()
    assert "window.sessionStorage.setItem(STORE_KEY" in src
    assert "localStorage.setItem(STORE_KEY" not in src  # per-tab only
    # End chat (resetChat) wipes the saved conversation.
    reset = re.search(r"function resetChat\(\) \{.*?\n      }\n", src, re.S).group(0)
    assert "transcript = []" in reset and "persist()" in reset


def test_restored_messages_use_the_text_only_render_path():
    src = _script()
    body = re.search(r"function restoreChat\(\) \{.*?\n      }\n", src, re.S).group(0)
    assert "addBubble(" in body and "renderSources(" in body
    assert "replayableActions(" in body  # replayed actions never auto-open
    assert "innerHTML" not in body
    # Restore is skipped (and leftovers dropped) when no AI model is set up.
    assert "assistant-needs-ai" in body and "clearStored()" in body


def test_action_links_only_accept_app_paths_and_http():
    src = _script()
    body = re.search(r"function renderActions\(actions\) \{.*?\n      }\n", src, re.S).group(0)
    assert r"/^(\/(?!\/)|https?:\/\/)/i.test(a.url)" in body
    assert "a.url;" not in body and "window.open(url" in body
