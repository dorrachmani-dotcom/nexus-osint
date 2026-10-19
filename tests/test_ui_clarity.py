"""UI clarity pass: calmer header, simpler feed cards, clearer counts."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def _load_demo(conn) -> None:
    from nexus.demodata import load_demo_data

    load_demo_data(conn)


# --- redundant body text ---------------------------------------------------

def test_redundant_with_detects_headline_only_bodies():
    from nexus.web.common import _redundant_with

    title = "Google Helps Huntington Bank Deploy AI System - PYMNTS.com"
    assert _redundant_with("Google Helps Huntington Bank Deploy AI System PYMNTS.com", title)
    assert _redundant_with("Google Helps Huntington Bank", title)
    assert _redundant_with(title + " Reuters", title)
    assert not _redundant_with("A completely different, longer summary of events.", title)
    assert not _redundant_with("", title)
    assert not _redundant_with("Body text", "")


# --- "What's connected" summary --------------------------------------------

def test_sorted_status_puts_active_sources_first():
    from nexus.web.source_labels import sorted_status, source_label, source_needs

    status = {"ai": "off", "rss": "off", "gnews": "on", "holehe": "off", "reddit": "on"}
    names = [n for n, _ in sorted_status(status)]
    assert names[:2] == ["gnews", "reddit"]
    assert set(names) == set(status)
    assert source_label("gnews") == "Google News"
    assert "key" in source_needs("serpapi").lower()
    # Unknown ids degrade to the id itself, never an error.
    assert source_label("brand-new-source") == "brand-new-source"
    assert source_needs("brand-new-source") == ""


def test_header_shows_one_connected_summary_not_a_chip_row(temp_db):
    with temp_db():
        html = _client().get("/cases").text
    assert 'id="connected-menu"' in html
    assert "sources</span>" in html
    assert "Google News" in html          # human label inside the disclosure
    assert "What's connected:" not in html  # the old always-visible chip row


# --- one scan label ----------------------------------------------------------

def test_scan_button_label_is_consistent(temp_db):
    with temp_db() as conn:
        _load_demo(conn)
    with temp_db():
        c = _client()
        for path in ("/", "/brief", "/intel"):
            page = c.get(path).text
            assert "Manual scan" not in page, path
        assert "Scan now" in c.get("/").text


# --- feed card -----------------------------------------------------------------

def test_feed_card_keeps_three_visible_actions_and_a_more_menu(temp_db):
    with temp_db() as conn:
        _load_demo(conn)
    with temp_db():
        html = _client().get("/").text
    assert "&#8599; Open" in html
    assert "&#10005; Dismiss" in html
    assert 'aria-label="More actions"' in html
    # Secondary actions moved into the menu with clearer labels.
    assert "Capture evidence" in html and "Add to a list" in html
    assert ">Single source<" not in html and ">1 source<" in html


def _render_card(**over):
    from nexus.web.common import TEMPLATES

    it = {
        "id": 7, "source": "gnews", "title": "Acme ships a widget - Example News",
        "content": "Acme ships a widget Example News", "reliability": "aggregator",
        "threat_level": "none", "fetched_at": "2026-10-01T10:00:00", "cases": [],
        "lists": [], "url": "https://example.com/a",
    }
    it.update(over)
    return TEMPLATES.get_template("_feed_item.html").render(it=it, all_lists=[], all_cases=[])


def test_feed_card_hides_aggregator_badge_and_headline_echo():
    html = _render_card()
    assert ">aggregator<" not in html
    # The body only repeats the title, so it is not printed a second time.
    assert html.count("Acme ships a widget") == 1


def test_feed_card_keeps_meaningful_body_and_other_reliability():
    html = _render_card(reliability="official", content="Acme confirmed the launch date today.")
    assert ">official<" in html
    assert "Acme confirmed the launch date today." in html


# --- case board counts -----------------------------------------------------------

def test_case_cards_show_collected_and_pinned(temp_db):
    with temp_db() as conn:
        _load_demo(conn)
    with temp_db():
        html = _client().get("/cases").text
    assert "collected &middot;" in html and "pinned" in html
    assert "item(s)" not in html


# --- settings ------------------------------------------------------------------

def test_gemini_key_is_listed_first():
    from nexus.web.settings_context import SECRET_FIELDS

    assert SECRET_FIELDS[0]["key"] == "GEMINI_API_KEY"
