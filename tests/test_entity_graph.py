"""Tests for the topic entity graph aggregation (storage.topic_entity_graph).

Locks in the "who is talked about the most / who is most connected" feature: it
must aggregate AI-extracted entities across stored items into mention counts and
a co-occurrence network, de-duplicate names case-insensitively, respect the feed
filters, and cap the rendered node set. No AI / network needed — analyses are
written directly with save_analysis.
"""

from __future__ import annotations

from nexus.models import Analysis, RawItem
from nexus.storage import save_analysis, topic_entity_graph, upsert_item


def _add(conn, *, title, content, source="rss", groups=None):
    item_id, _ = upsert_item(
        conn, RawItem(source=source, title=title, content=content)
    )
    save_analysis(
        conn,
        item_id,
        Analysis(entity_groups=groups or {}, entities=[]),
    )
    return item_id


def test_topic_entity_graph_counts_mentions_and_connections(temp_db):
    with temp_db() as conn:
        # Alice appears in two items; she co-occurs with Bob and with Acme.
        _add(
            conn,
            title="one",
            content="first",
            groups={"people": ["Alice", "Bob"], "organizations": ["Acme"]},
        )
        _add(
            conn,
            title="two",
            content="second",
            groups={"people": ["Alice"], "organizations": ["Acme"]},
        )
        data = topic_entity_graph(conn)

    by_name = {n["id"]: n for n in data["nodes"]}
    assert by_name["Alice"]["mentions"] == 2
    assert by_name["Acme"]["mentions"] == 2
    assert by_name["Bob"]["mentions"] == 1
    # Alice co-occurs with Bob and Acme -> 2 distinct partners.
    assert by_name["Alice"]["connections"] == 2

    # Ranked tables: the two-mention entities (Alice, Acme) lead the
    # most-talked-about list; Bob (one mention) trails.
    top_two = {e["name"] for e in data["most_mentioned"][:2]}
    assert top_two == {"Alice", "Acme"}
    assert data["most_mentioned"][-1]["name"] == "Bob"
    assert data["item_count"] == 2
    assert data["entity_count"] == 3  # Alice, Bob, Acme


def test_topic_entity_graph_ranks_strongest_relationships(temp_db):
    with temp_db() as conn:
        # Alice+Bob co-occur in two items; Alice+Acme in only one. So the
        # Alice<->Bob pair must outrank Alice<->Acme by co-occurrence weight.
        _add(conn, title="1", content="x", groups={"people": ["Alice", "Bob"]})
        _add(
            conn, title="2", content="y",
            groups={"people": ["Alice", "Bob"], "organizations": ["Acme"]},
        )
        data = topic_entity_graph(conn)

    rels = data["top_relationships"]
    assert rels, "expected at least one relationship"
    top = rels[0]
    assert {top["source"], top["target"]} == {"Alice", "Bob"}
    assert top["weight"] == 2
    # Every weaker pair must have a weight no greater than the leader's.
    assert all(r["weight"] <= top["weight"] for r in rels)


def test_topic_entity_graph_has_no_relationships_without_co_occurrence(temp_db):
    with temp_db() as conn:
        # A lone entity per item -> no pairs -> no relationships.
        _add(conn, title="a", content="x", groups={"people": ["Alice"]})
        _add(conn, title="b", content="y", groups={"people": ["Bob"]})
        data = topic_entity_graph(conn)
    assert data["top_relationships"] == []


def test_topic_entity_graph_dedups_case_insensitively(temp_db):
    with temp_db() as conn:
        _add(conn, title="a", content="x", groups={"people": ["Alice"]})
        _add(conn, title="b", content="y", groups={"people": ["alice"]})
        data = topic_entity_graph(conn)

    # "Alice" and "alice" collapse into a single node (first-seen casing wins).
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["id"].lower() == "alice"
    assert data["nodes"][0]["mentions"] == 2
    assert data["entity_count"] == 1


def test_topic_entity_graph_merges_punctuation_variants(temp_db):
    with temp_db() as conn:
        # The AI extraction left quote/punctuation noise around the same name in
        # different items; these must collapse into a single node, not three.
        _add(conn, title="1", content="x", groups={"people": ["Alice"]})
        _add(conn, title="2", content="y", groups={"people": ["Alice."]})
        _add(conn, title="3", content="z", groups={"people": ['"Alice"']})
        data = topic_entity_graph(conn)

    assert data["entity_count"] == 1
    assert len(data["nodes"]) == 1
    node = data["nodes"][0]
    assert node["mentions"] == 3
    # The display form is tidied (no surrounding quotes/punctuation).
    assert node["label"] == "Alice"


def test_topic_entity_graph_counts_each_item_once_after_merge(temp_db):
    with temp_db() as conn:
        # Both variants appear in ONE item — that's a single mention, not two.
        _add(
            conn, title="1", content="x",
            groups={"people": ["Alice", "Alice."]},
        )
        data = topic_entity_graph(conn)

    assert data["entity_count"] == 1
    assert data["nodes"][0]["mentions"] == 1


def test_topic_entity_graph_respects_source_filter(temp_db):
    with temp_db() as conn:
        _add(conn, title="a", content="x", source="rss", groups={"people": ["Alice"]})
        _add(conn, title="b", content="y", source="reddit", groups={"people": ["Bob"]})
        only_rss = topic_entity_graph(conn, source="rss")

    names = [n["id"] for n in only_rss["nodes"]]
    assert names == ["Alice"]


def test_topic_entity_graph_empty_when_no_entities(temp_db):
    with temp_db() as conn:
        data = topic_entity_graph(conn)
    assert data["entity_count"] == 0
    assert data["nodes"] == []
    assert data["edges"] == []
