"""Per-case relationship graph + Obsidian vault export.

The graph can be scoped to one case's tracked items (not the whole river), and a
case exports as an Obsidian Markdown vault (one note per item with [[wikilinks]]
to entities) so Obsidian's graph view shows the case's web of connections.
"""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from nexus import storage as s
from nexus.models import Analysis, RawItem, ThreatLevel


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def _seed_case_with_entities(conn) -> int:
    cid = s.create_case(conn, "Neymar", "Transfer watch", "high")
    s.add_case_term(conn, cid, "Neymar")
    s.add_requirement(conn, "Transfer status?", case_id=cid)
    iid, _ = s.upsert_item(
        conn, RawItem(source="rss", title="Neymar joins Mbappe at PSG",
                      url="https://e/x", content="body")
    )
    s.save_analysis(conn, iid, Analysis(
        threat_level=ThreatLevel.HIGH, summary="Big move.",
        entity_groups={"people": ["Neymar", "Mbappe"], "organizations": ["PSG"]},
        entities=[]))
    # An unrelated item that must NOT show up in the case graph/vault.
    jid, _ = s.upsert_item(conn, RawItem(source="rss", title="Local weather", content="z"))
    s.save_analysis(conn, jid, Analysis(
        threat_level=ThreatLevel.LOW, summary="rain",
        entity_groups={"other": ["Tornado"]}, entities=[]))
    return cid


# --- per-case graph --------------------------------------------------------

def test_case_graph_is_scoped(temp_db):
    with temp_db() as conn:
        cid = _seed_case_with_entities(conn)

    page = _client().get("/graph", params={"case": cid})
    assert page.status_code == 200
    assert "Neymar" in page.text and "Mbappe" in page.text
    assert "Tornado" not in page.text  # unrelated entity excluded


def test_case_with_no_terms_has_empty_graph(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Empty")
        s.upsert_item(conn, RawItem(source="rss", title="Anything", content="x"))

    page = _client().get("/graph", params={"case": cid})
    assert page.status_code == 200  # renders, just no nodes


def test_case_page_links_to_its_graph(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
    # The per-case relationship graph is now a tab in the case hub; the case page
    # links to it via ?tab=graph (the tab then lazy-loads /graph/build?case=...).
    assert f"/cases/{cid}?tab=graph" in _client().get(f"/cases/{cid}").text


# --- Obsidian vault --------------------------------------------------------

def test_obsidian_vault_export(temp_db):
    with temp_db() as conn:
        cid = _seed_case_with_entities(conn)

    r = _client().get(f"/cases/{cid}/obsidian")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "Neymar.md" in names  # the case index note
    assert any(n.startswith("items/") for n in names)

    index = zf.read("Neymar.md").decode("utf-8")
    assert "Tracking words" in index and "Neymar" in index
    assert "Transfer status?" in index  # the question

    item_note = zf.read(next(n for n in names if n.startswith("items/"))).decode("utf-8")
    # Entities are [[wikilinks]] -> Obsidian builds the graph from these.
    assert "[[Neymar]]" in item_note
    assert "[[Mbappe]]" in item_note
    assert "[[PSG]]" in item_note


def test_obsidian_export_unknown_case_404(temp_db):
    with temp_db():
        assert _client().get("/cases/99999/obsidian").status_code == 404


def test_obsidian_vault_carries_no_secrets(temp_db):
    with temp_db() as conn:
        cid = _seed_case_with_entities(conn)
    blob = _client().get(f"/cases/{cid}/obsidian").content
    for needle in (b"GEMINI_API_KEY", b"api_key", b"SERPAPI"):
        assert needle not in blob
