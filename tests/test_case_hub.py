"""Tests for the unified Case hub data model (Phase 1).

A Case is now the single hub for a subject: it owns tracking words (its "word
capsule", driving a live feed), per-case questions (PIRs), pinned items + notes,
and one level of sub-cases. These tests lock in the storage layer and the
one-time migration that folds legacy capsules/topics into cases.
"""

from __future__ import annotations

from nexus import storage as s
from nexus.models import RawItem


def test_case_terms_crud_and_live_feed(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        s.add_case_term(conn, cid, "Neymar")
        s.add_case_term(conn, cid, "World Cup")
        s.add_case_term(conn, cid, "Neymar")  # duplicate ignored
        terms = [t["term"] for t in s.case_terms(conn, cid)]
        assert terms == ["Neymar", "World Cup"]

        # An item mentioning a term shows in the case live feed; an unrelated one does not.
        s.upsert_item(conn, RawItem(source="rss", title="Neymar injury update", content="..."))
        s.upsert_item(conn, RawItem(source="rss", title="Stock market today", content="..."))
        live = s.case_live_items(conn, cid)
        titles = [it["title"] for it in live]
        assert "Neymar injury update" in titles
        assert "Stock market today" not in titles

        # Remove a term.
        tid = s.case_terms(conn, cid)[0]["id"]
        s.remove_case_term(conn, cid, tid)
        assert [t["term"] for t in s.case_terms(conn, cid)] == ["World Cup"]


def test_case_live_feed_empty_without_terms(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Empty case")
        s.upsert_item(conn, RawItem(source="rss", title="Anything", content="x"))
        assert s.case_live_items(conn, cid) == []  # no terms -> no live feed


def test_subcases_one_level(temp_db):
    with temp_db() as conn:
        parent = s.create_case(conn, "Neymar")
        sub = s.create_case(conn, "Transfer rumors", parent_id=parent)
        # A sub-case of a sub-case flattens to the top-level parent.
        deeper = s.create_case(conn, "Deeper", parent_id=sub)
        assert s.get_case(conn, deeper)["parent_id"] == parent

        subs = [c["name"] for c in s.list_cases(conn, parent_id=parent)]
        assert "Transfer rumors" in subs and "Deeper" in subs

        # Top-level listing excludes sub-cases and reports a sub-case count.
        tops = s.list_cases(conn, parent_id=None)
        names = {c["name"]: c for c in tops}
        assert "Neymar" in names and "Transfer rumors" not in names
        assert names["Neymar"]["subcase_count"] == 2


def test_requirements_global_vs_case(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        s.add_requirement(conn, "Any injury news?", case_id=cid)
        s.add_requirement(conn, "Global watch question")  # case_id NULL = global

        case_qs = [r["question"] for r in s.list_requirements(conn, case_id=cid)]
        global_qs = [r["question"] for r in s.list_requirements(conn, case_id=None)]
        assert case_qs == ["Any injury news?"]
        assert "Global watch question" in global_qs
        assert "Any injury news?" not in global_qs
        # Omitting the filter returns both.
        all_qs = [r["question"] for r in s.list_requirements(conn)]
        assert len(all_qs) == 2


def test_case_question_items_scoped_to_case(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Neymar")
        rid = s.add_requirement(conn, "Injury status?", case_id=cid)
        gid = s.add_requirement(conn, "Unrelated global Q")

        iid_a, _ = s.upsert_item(conn, RawItem(source="rss", title="Neymar hurt", content="..."))
        iid_b, _ = s.upsert_item(conn, RawItem(source="rss", title="Other thing", content="..."))
        # A scores against the case question; B only against the global one.
        conn.execute(
            "INSERT INTO requirement_hits (item_id, requirement_id, score) VALUES (?,?,?)",
            (iid_a, rid, 90),
        )
        conn.execute(
            "INSERT INTO requirement_hits (item_id, requirement_id, score) VALUES (?,?,?)",
            (iid_b, gid, 80),
        )
        titles = [it["title"] for it in s.case_question_items(conn, cid)]
        assert "Neymar hurt" in titles
        assert "Other thing" not in titles


def test_cross_source_corroboration(temp_db):
    # Same story (one cluster) from two sources -> corroborated; solo -> not.
    with temp_db() as conn:
        conn.execute("INSERT INTO clusters (id, shared_count) VALUES ('k1', 2)")
        conn.execute("INSERT INTO clusters (id, shared_count) VALUES ('k3', 1)")
        conn.execute("INSERT INTO items (content_hash,dedup_key,source,title,content,cluster_id)"
                     " VALUES ('h1','k1','rss','Story A','x','k1')")
        conn.execute("INSERT INTO items (content_hash,dedup_key,source,title,content,cluster_id)"
                     " VALUES ('h2','k1','gnews','Story A','x','k1')")
        conn.execute("INSERT INTO items (content_hash,dedup_key,source,title,content,cluster_id)"
                     " VALUES ('h3','k3','rss','Solo','y','k3')")
        rows = s.search_items(conn, limit=10)
        s.enrich_feed_rows(conn, rows)
        by_title = {r["title"]: r for r in rows}
        assert by_title["Story A"]["source_count"] == 2
        assert by_title["Story A"]["corroborated"] is True
        assert by_title["Solo"]["source_count"] == 1
        assert by_title["Solo"]["corroborated"] is False


def test_case_terms_map_is_one_query_and_correct(temp_db):
    with temp_db() as conn:
        a = s.create_case(conn, "A")
        s.add_case_term(conn, a, "x")
        s.add_case_term(conn, a, "y")
        b = s.create_case(conn, "B")
        s.add_case_term(conn, b, "z")
        c = s.create_case(conn, "C")  # no terms
        m = s.case_terms_map(conn, [a, b, c])
        assert m[a] == ["x", "y"]
        assert m[b] == ["z"]
        assert m[c] == []
        # case_new_count accepts preloaded terms (board path) and matches the lazy path.
        assert s.case_new_count(conn, a, terms=m[a]) == s.case_new_count(conn, a)


def test_capsule_to_case_migration(temp_db):
    from nexus.db import _migrate_capsules_to_cases

    with temp_db() as conn:
        # Reset the run-once flag and seed legacy capsule + topic-question data.
        conn.execute("DELETE FROM meta WHERE key = 'migrated_capsules_to_cases'")
        for term in ("Neymar", "World Cup"):
            conn.execute(
                "INSERT INTO subscriptions (source, value, label) VALUES ('query', ?, 'Neymar')",
                (term,),
            )
        conn.execute(
            "INSERT INTO requirements (question, priority, enabled, topic) "
            "VALUES ('Injury?', 1, 1, 'Neymar')"
        )

        _migrate_capsules_to_cases(conn)

        row = conn.execute("SELECT id FROM cases WHERE name = 'Neymar'").fetchone()
        assert row is not None
        cid = int(row["id"])
        assert {t["term"] for t in s.case_terms(conn, cid)} == {"Neymar", "World Cup"}
        assert any("Injury" in r["question"] for r in s.list_requirements(conn, case_id=cid))
        # Flag set -> a second run is a no-op (no duplicate case).
        _migrate_capsules_to_cases(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM cases WHERE name = 'Neymar'").fetchone()["c"]
        assert n == 1
