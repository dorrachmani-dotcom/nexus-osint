"""The UI must render with no internet: no CDN scripts, styles or fonts.

Every page loads its stylesheet and htmx from /static, the CSP only allows our
own origin, and the relationship graph embeds vis-network instead of linking it.
"""

import re

from fastapi.testclient import TestClient

from nexus.web.app import app

_REMOTE_ASSET = re.compile(r'<(?:script|link)\b[^>]*\b(?:src|href)="https?://', re.I)


def _client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def test_pages_load_no_remote_assets(temp_db) -> None:
    c = _client()
    for path in ("/", "/settings", "/brief", "/cases", "/graph", "/guide"):
        html = c.get(path).text
        assert not _REMOTE_ASSET.search(html), f"{path} loads a remote asset"
        assert "/static/css/app.css" in html
        assert "/static/vendor/htmx.min.js" in html


def test_bundled_assets_are_served(temp_db) -> None:
    c = _client()
    css = c.get("/static/css/app.css")
    assert css.status_code == 200 and ".htmx-indicator" in css.text
    js = c.get("/static/vendor/htmx.min.js")
    assert js.status_code == 200 and "htmx" in js.text


def test_csp_allows_only_self(temp_db) -> None:
    csp = _client().get("/").headers["content-security-policy"]
    assert "https://" not in csp


def test_graph_html_is_self_contained() -> None:
    from nexus.graph import render_entity_graph_html

    html = render_entity_graph_html({
        "nodes": [{"id": "a", "label": "Acme Corp", "kind": "organization",
                   "mentions": 1, "connections": 0}],
        "edges": [],
    })
    if html is None:  # pyvis not installed: graph is an optional feature
        return
    assert not _REMOTE_ASSET.search(html)
    assert "new vis.Network" in html
