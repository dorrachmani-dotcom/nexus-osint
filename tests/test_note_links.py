"""Obsidian wikilinks: note_links table, resolution, and the graph overlay (F1).

Vaults are generated in a tmp dir; names are fictional.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus.sources import obsidian as O
from nexus.storage import note_links as NL


@pytest.fixture
def vault(tmp_path) -> Path:
    root = tmp_path / "Field vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "Projects").mkdir()
    (root / "Projects" / "Alpha.md").write_text(
        "# Alpha\n\nMeeting with Example Corp about [[Bravo]], [[Charlie|the C team]] "
        "and [[Bravo#Plan]]. Not Example Corporation.\n", encoding="utf-8")
    (root / "Bravo.md").write_text("# Bravo\n\nBack to [[Projects/Alpha]].\n", encoding="utf-8")
    (root / "Lonely.md").write_text("# Lonely\n\nNo links here.\n", encoding="utf-8")
    return root


def _scan(temp_db, path) -> O.ObsidianSource:
    from nexus.storage.obsidian_vaults import create_obsidian_vault, get_obsidian_vault

    with temp_db() as conn:
        vid = create_obsidian_vault(conn, {"path": str(path), "name": "Field"})
        src = O.ObsidianSource(get_obsidian_vault(conn, vid))
    src.ingest()
    return src


def _rescan(temp_db, src) -> None:
    from nexus.storage.obsidian_vaults import get_obsidian_vault

    with temp_db() as conn:
        O.ObsidianSource(get_obsidian_vault(conn, src.id)).ingest()


def _links(temp_db) -> set[tuple[str, str, str | None]]:
    with temp_db() as conn:
        return {(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT src_key, target, dst_key FROM note_links")}


def _touch(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))


def test_resolve_like_obsidian():
    by_path, by_name = NL._resolver(["Projects/Alpha.md", "Bravo.md", "Archive/Old/Bravo.md"])
    assert NL.resolve("Alpha", by_path, by_name) == "Projects/Alpha.md"
    assert NL.resolve("projects/alpha.md", by_path, by_name) == "Projects/Alpha.md"
    assert NL.resolve("Bravo", by_path, by_name) == "Bravo.md"  # shortest path wins
    assert NL.resolve("Archive/Old/Bravo", by_path, by_name) == "Archive/Old/Bravo.md"
    assert NL.resolve("Missing", by_path, by_name) is None
    assert NL.resolve("  ", by_path, by_name) is None


def test_scan_stores_resolved_and_dangling_links(temp_db, vault):
    _scan(temp_db, vault)
    assert _links(temp_db) == {
        ("Projects/Alpha.md", "Bravo", "Bravo.md"),
        ("Projects/Alpha.md", "Charlie", None),
        ("Bravo.md", "Projects/Alpha", "Projects/Alpha.md"),
    }


def test_rescan_resolves_new_notes_and_drops_removed(temp_db, vault):
    src = _scan(temp_db, vault)
    _touch(vault / "Charlie.md", "# Charlie\n\nSee [[Lonely]].\n")
    (vault / "Bravo.md").unlink()
    _rescan(temp_db, src)
    links = _links(temp_db)
    assert ("Projects/Alpha.md", "Charlie", "Charlie.md") in links
    assert ("Charlie.md", "Lonely", "Lonely.md") in links
    assert ("Projects/Alpha.md", "Bravo", None) in links  # now dangling
    assert not any(src_key == "Bravo.md" for src_key, _t, _d in links)


def test_forgetting_the_vault_removes_its_links(temp_db, vault):
    from nexus.ingest.connector import forget_source

    src = _scan(temp_db, vault)
    with temp_db() as conn:
        forget_source(conn, "obsidian", src.id, "Field")
    assert _links(temp_db) == set()


def test_backfill_for_vaults_scanned_before_the_table(temp_db, vault):
    from nexus.db import init_db

    _scan(temp_db, vault)
    with temp_db() as conn:
        conn.execute("DELETE FROM note_links")
    init_db()
    assert len(_links(temp_db)) == 3


def test_note_graph_links_notes_and_entities(temp_db, vault):
    src = _scan(temp_db, vault)
    with temp_db() as conn:
        g = NL.note_graph(conn, ["Example Corp", "Nobody Here"])
    alpha, bravo = NL.note_id(src.id, "Projects/Alpha.md"), NL.note_id(src.id, "Bravo.md")
    assert [n["id"] for n in g["notes"]] == [alpha, bravo]  # Bravo joins as a link neighbour
    assert g["notes"][0]["entities"] == ["Example Corp"] and g["notes"][0]["vault"] == "Field"
    assert {(e["source"], e["target"]) for e in g["note_edges"]} == {(alpha, bravo), (bravo, alpha)}
    assert g["entity_edges"] == [{"source": alpha, "target": "Example Corp"}]


def test_note_graph_without_entities_shows_linked_notes(temp_db, vault):
    _scan(temp_db, vault)
    with temp_db() as conn:
        g = NL.note_graph(conn, [])
    labels = {n["label"] for n in g["notes"]}
    assert labels == {"Alpha", "Bravo"}  # the unlinked note stays off the map
    assert g["entity_edges"] == []


def test_note_graph_empty_without_vaults(temp_db):
    with temp_db() as conn:
        assert NL.note_graph(conn, ["Example Corp"]) == {
            "notes": [], "note_edges": [], "entity_edges": []}


def test_renderer_draws_notes_even_without_entities():
    from nexus.graph import render_entity_graph_html

    html = render_entity_graph_html({"nodes": [], "edges": [], "notes": {
        "notes": [{"id": "note:1:A.md", "label": "Alpha note", "vault": "Field",
                   "path": "A.md", "entities": [], "links": 1},
                  {"id": "note:1:B.md", "label": "Bravo note", "vault": "Field",
                   "path": "B.md", "entities": [], "links": 0}],
        "note_edges": [{"source": "note:1:A.md", "target": "note:1:B.md"}],
        "entity_edges": [{"source": "note:1:A.md", "target": "Not On Map"}]}})
    assert html is not None
    nodes = json.loads(html.split("nodes = new vis.DataSet(", 1)[1].split(");", 1)[0])
    edges = json.loads(html.split("edges = new vis.DataSet(", 1)[1].split(");", 1)[0])
    assert {n["label"] for n in nodes} == {"Alpha note", "Bravo note"}
    assert len(edges) == 1  # the edge to an entity not on the map is dropped


def test_graph_route_toggle(temp_db, vault):
    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    with temp_db():
        assert 'name="notes"' not in client.get("/graph").text  # no vault yet
    _scan(temp_db, vault)
    with temp_db():
        page = client.get("/graph").text
        assert 'name="notes"' in page
        part = client.get("/graph/build?notes=true").text
        assert "Obsidian notes on this map" in part and "Projects/Alpha.md" in part
        assert "/graph/frame/" in part
        assert "Obsidian notes on this map" not in client.get("/graph/build").text
