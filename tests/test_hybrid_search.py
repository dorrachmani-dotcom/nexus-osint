"""Hybrid knowledge-pool search and clickable document citations (F4).

The embedder is faked (a word-bag over a tiny vocabulary), so tests are
deterministic and need no Ollama. All names are fictional.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus import embeddings as E
from nexus.config import get_settings
from nexus.ingest.upload import ingest_upload
from nexus.sources.base import RawItem

GROUPS = (("car", "vehicle", "truck"), ("river", "water", "flood"), ("money", "funds", "payment"))
APP_JS = Path(__file__).resolve().parents[1] / "nexus" / "web" / "static" / "js" / "app.js"


def _vector(text: str) -> list[float]:
    words = text.lower().replace(".", " ").split()
    return [float(sum(w in group for w in words)) + 0.001 for group in GROUPS]


@pytest.fixture
def embedder(monkeypatch):
    calls: list[str] = []

    def fake(text, settings):
        calls.append(text)
        return _vector(text)

    monkeypatch.setattr(E, "_embed_one", fake)
    return calls


def _document(name: str, text: str, **kwargs) -> int:
    res = ingest_upload(text.encode(), name, **{"in_pool": True, **kwargs})
    assert res.get("document_id"), res
    return int(res["document_id"])


def _chunks(conn) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT item_id FROM document_items")]


def test_rrf_rewards_agreement():
    assert E.rrf([[1, 2, 3], [3, 4]])[0] == 3
    assert set(E.rrf([[1], [2]])) == {1, 2}
    assert E.rrf([[], []]) == []


def test_no_vectors_means_fulltext_and_no_network(temp_db, monkeypatch):
    monkeypatch.setattr(E, "_embed_one", lambda *_a: pytest.fail("no call expected"))
    with temp_db() as conn:
        _document("a.txt", "The vehicle stopped at the depot.")
        rows = E.hybrid_chunks(conn, "vehicle depot", ["vehicle"])
    assert [r["filename"] for r in rows] == ["a.txt"]


def test_meaning_finds_what_words_miss(temp_db, embedder):
    with temp_db() as conn:
        _document("vehicles.txt", "A truck left the depot at dawn.")
        _document("rivers.txt", "The river rose after the storm.")
        assert E.embed_pool(conn)["embedded"] == 2
        rows = E.hybrid_chunks(conn, "car seen where", ["car"])
    assert [r["filename"] for r in rows] == ["vehicles.txt"]  # no shared word


def test_both_rankings_are_combined(temp_db, embedder):
    with temp_db() as conn:
        _document("words.txt", "The funds report mentions the depot once.")
        _document("meaning.txt", "A payment of money went through, money again.")
        E.embed_pool(conn)
        names = {r["filename"] for r in E.hybrid_chunks(conn, "funds", ["funds"])}
    assert names == {"words.txt", "meaning.txt"}


def test_failing_embedder_falls_back_to_fulltext(temp_db, embedder, monkeypatch):
    with temp_db() as conn:
        _document("a.txt", "The vehicle stopped at the depot.")
        E.embed_pool(conn)
        monkeypatch.setattr(E, "_embed_one", lambda *_a: None)
        rows = E.hybrid_chunks(conn, "vehicle", ["vehicle"])
    assert [r["filename"] for r in rows] == ["a.txt"]


def test_low_similarity_is_not_a_result(temp_db, monkeypatch):
    with temp_db() as conn:
        _document("a.txt", "The vehicle stopped at the depot.")
        monkeypatch.setattr(E, "_embed_one", lambda *_a: [1.0, 0.0])
        E.embed_pool(conn)
        monkeypatch.setattr(E, "_embed_one", lambda *_a: [0.0, 1.0])
        assert E.hybrid_chunks(conn, "unrelated", ["unrelated"]) == []


def test_case_documents_stay_in_their_case(temp_db, embedder):
    from nexus.storage import create_case

    with temp_db() as conn:
        case_id = create_case(conn, "Field work")
        conn.commit()
        _document("secret.txt", "A truck left at dawn.", in_pool=False, case_id=case_id)
        E.embed_pool(conn)
        assert E.hybrid_chunks(conn, "car", ["car"]) == []
        rows = E.hybrid_chunks(conn, "car", ["car"], case_id=case_id)
    assert [r["filename"] for r in rows] == ["secret.txt"]


def test_embed_pool_is_incremental_and_stops_on_failure(temp_db, embedder, monkeypatch):
    with temp_db() as conn:
        _document("a.txt", "A truck left at dawn.")
        assert E.embed_pool(conn) == {"embedded": 1, "stopped": False}
        assert E.embed_pool(conn) == {"embedded": 0, "stopped": False}
        _document("b.txt", "The river rose.")
        _document("c.txt", "Money moved.")
        monkeypatch.setattr(E, "_embed_one", lambda *_a: None)
        assert E.embed_pool(conn) == {"embedded": 0, "stopped": True}


def test_embed_pool_holds_no_transaction_while_embedding(temp_db, monkeypatch):
    with temp_db() as conn:
        _document("a.txt", "A truck left at dawn.")
        _document("b.txt", "The river rose.")
        states: list[bool] = []

        def fake(text, settings):
            states.append(conn.in_transaction)
            return _vector(text)

        monkeypatch.setattr(E, "_embed_one", fake)
        E.embed_pool(conn)
    assert states == [False, False]


def test_no_ai_documents_stay_off_a_remote_ollama(temp_db, embedder, monkeypatch):
    with temp_db() as conn:
        _document("private.txt", "A truck left at dawn.", no_ai=True)
        settings = get_settings().model_copy(update={"ollama_base_url": "http://gpu.example:11434"})
        assert E.embed_pool(conn, settings)["embedded"] == 0
        local = get_settings().model_copy(update={"ollama_base_url": "http://127.0.0.1:11434"})
        assert E.embed_pool(conn, local)["embedded"] == 1


def test_changing_the_model_does_not_mix_vectors(temp_db, embedder):
    with temp_db() as conn:
        _document("a.txt", "A truck left at dawn.")
        E.embed_pool(conn)
        other = get_settings().model_copy(update={"ollama_embed_model": "nomic-embed-text"})
        assert E.embed_model(other) == "nomic-embed-text"
        assert E._pool_vectors(conn, E.embed_model(other), None) == {}
        assert E.embed_pool(conn, other)["embedded"] == 1
        assert len(E._pool_vectors(conn, "nomic-embed-text", None)) == 1


def test_sherlock_context_cites_a_document_with_its_name_and_url(temp_db, embedder):
    from nexus import assistant as A
    from nexus.storage import upsert_item

    with temp_db() as conn:
        upsert_item(conn, RawItem(source="rss", track="news", external_id="x1", title="Unrelated",
                                  content="Weather today.", url="https://news.example/1"))
        conn.commit()
        doc = _document("fleet.txt", "A truck left at dawn.")
        item = _chunks(conn)[0]
        E.embed_pool(conn)
        conn.execute("UPDATE items SET url = 'https://wiki.example/fleet' WHERE id = ?", (item,))
        text, items = A._data_context(conn, "where did the car go")
    cited = next(i for i in items if i["id"] == item)
    assert cited["source"] == "fleet.txt" and cited["url"] == "https://wiki.example/fleet"
    assert f"Item {cited['n']}. (document: fleet.txt)" in text and doc


def test_a_keyword_hit_is_cited_as_a_document_once(temp_db, embedder):
    from nexus import assistant as A

    with temp_db() as conn:
        _document("fleet.txt", "A truck left at dawn.")
        E.embed_pool(conn)
        text, items = A._data_context(conn, "which truck left")
    assert [i["source"] for i in items] == ["fleet.txt"]
    assert text.count("A truck left at dawn.") == 1


def test_cloud_model_still_never_sees_no_ai_chunks(temp_db, embedder, monkeypatch):
    from nexus import assistant as A

    with temp_db() as conn:
        _document("private.txt", "A truck left at dawn.", no_ai=True)
        E.embed_pool(conn)
        assert A._document_context(conn, ["car"], True, "car") == []
        assert len(A._document_context(conn, ["car"], False, "car")) == 1


def test_chat_opens_uncited_url_items_in_the_app_never_a_scheme():
    js = APP_JS.read_text(encoding="utf-8")
    body = js.split("function itemLink(it, className) {", 1)[1].split("\n  }\n", 1)[0]
    assert r"/^https?:\/\//i.test(it.url)" in body
    assert "parseInt(it.id, 10)" in body and "'/items/' + id + '/detail'" in body
    assert "innerHTML =" not in body
