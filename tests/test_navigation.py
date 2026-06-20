"""Information-architecture guardrails: the reorganized navigation and the
unified Sources area stay coherent (workflow-grouped nav, no dead links)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client(redirects: bool = True) -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1", follow_redirects=redirects)


def test_sources_canonical_redirects_to_feeds_tab(temp_db):
    with temp_db():
        r = _client(redirects=False).get("/sources")
    assert r.status_code == 302
    assert r.headers["location"] == "/topics"


def test_nav_is_grouped_with_more_and_setup(temp_db):
    with temp_db():
        home = _client().get("/").text
    # Primary flow items present...
    for label in (">Brief", ">Feed<", ">Cases<", ">Graph<", ">Sources<"):
        assert label in home, label
    # ...and the decluttering dropdowns exist.
    assert "More" in home and "Setup" in home
    # The old colliding "Feeds" top-level label is gone (now 'Sources').
    assert ">Feeds<" not in home


def test_more_menu_targets_all_resolve(temp_db):
    # Everything tucked under More/Setup is still reachable (no dead links).
    with temp_db():
        c = _client()
        for path in ("/lists", "/watchlists", "/tools", "/requirements",
                     "/security", "/transfer", "/settings"):
            assert c.get(path).status_code == 200, path


def test_feed_and_intel_are_one_feed_two_views(temp_db):
    # Intel is folded into the Feed as a 'Most relevant' view, not a separate
    # destination — the two cross-link and Intel is out of the More menu.
    with temp_db():
        c = _client()
        feed = c.get("/").text
        assert "Most relevant" in feed and "Latest" in feed   # view toggle
        intel = c.get("/intel").text
        assert "most relevant" in intel.lower()
        assert "(most relevant)" not in feed  # the old More-menu Intel entry is gone


def test_sources_tabs_cross_link(temp_db):
    with temp_db():
        c = _client()
        assert "Custom APIs" in c.get("/topics").text       # Feeds tab links to APIs
        assert "Feeds &amp; presets" in c.get("/sources/custom").text


def test_guide_explains_the_flow(temp_db):
    with temp_db():
        assert "How it all fits together" in _client().get("/guide").text
