"""Optional semantic 'find similar' — local embeddings via Ollama.

Finds items whose *meaning* is close to a given item, not just shared keywords.
Embeddings are computed by a LOCAL Ollama model (POST /api/embeddings), so there
is no cloud call and no heavy ML dependency (torch / sentence-transformers). When
Ollama isn't reachable the feature simply reports itself unavailable — graceful
degradation, exactly like the rest of the AI layer.

Vectors are cached in ``item_embeddings`` so each item is embedded at most once;
candidates are limited to items sharing an entity (via the entity index) so a
single click never embeds the whole archive.

Knowledge-pool search is hybrid: full-text and embedding rankings are combined
with reciprocal rank fusion, so a question finds passages that share its words
and passages that share its meaning. Chunks are embedded in a bounded step after
each scan (:func:`embed_pool`). With no embedder, or nothing embedded yet, search
is the plain full-text one and no network call is made.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import sqlite3
from urllib.parse import urlsplit

from nexus.config import Settings, get_settings
from nexus.storage import feed_item

logger = logging.getLogger("nexus.embeddings")

_MAX_CANDIDATES = 40
_TIMEOUT = 30.0
_POOL_BATCH = 200          # chunks embedded after one scan
_POOL_VECTORS = 3000       # newest embedded chunks compared with a question
_MIN_SIMILARITY = 0.3      # a meaning match below this is noise, not a result
_RRF_K = 60                # the usual reciprocal-rank-fusion constant
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def embed_model(settings: Settings) -> str:
    return (settings.ollama_embed_model or "").strip() or settings.effective_ollama_model()


def embeddings_available(settings: Settings | None = None) -> bool:
    """True if a local Ollama server answers an embeddings request."""
    settings = settings or get_settings()
    return _embed_one("ping", settings) is not None


def _embed_one(text: str, settings: Settings) -> list[float] | None:
    """Embed one short text via Ollama; None on any failure (server down, etc.)."""
    try:
        import httpx

        resp = httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/embeddings",
            json={"model": embed_model(settings), "prompt": text[:4000]},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        vec = resp.json().get("embedding")
        return vec if isinstance(vec, list) and vec else None
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _item_text(row: dict) -> str:
    return " ".join(filter(None, [row.get("title"), row.get("summary") or row.get("content")]))


def _ensure_vectors(
    conn: sqlite3.Connection, ids: list[int], settings: Settings
) -> dict[int, list[float]]:
    """Return {item_id: vector} for the ids, computing+caching any that are missing."""
    out: dict[int, list[float]] = {}
    if not ids:
        return out
    ph = ",".join("?" * len(ids))
    model = embed_model(settings)
    for r in conn.execute(
        f"SELECT item_id, vec FROM item_embeddings WHERE model = ? AND item_id IN ({ph})",  # noqa: S608 (only a ?-placeholder list is interpolated)
        [model, *ids],
    ).fetchall():
        with contextlib.suppress(ValueError, TypeError):  # corrupt row: recompute below
            out[int(r["item_id"])] = json.loads(r["vec"])
    for iid in ids:
        if iid in out:
            continue
        row = feed_item(conn, iid)
        if not row:
            continue
        vec = _embed_one(_item_text(row), settings)
        if not vec:
            continue
        out[iid] = vec
        conn.execute(
            "INSERT OR REPLACE INTO item_embeddings (item_id, model, vec) VALUES (?, ?, ?)",
            (iid, model, json.dumps(vec)),
        )
    conn.commit()
    return out


def find_similar(
    conn: sqlite3.Connection, item_id: int, settings: Settings | None = None, limit: int = 5
) -> dict:
    """Items semantically closest to ``item_id``.

    Returns ``{"available": bool, "items": [...]}``. ``available`` is False when no
    local embedding backend is reachable (the UI then shows a hint), so the caller
    never has to handle an exception.
    """
    settings = settings or get_settings()
    target = feed_item(conn, item_id)
    if target is None:
        return {"available": True, "items": []}
    if not embeddings_available(settings):
        return {"available": False, "items": []}

    norms = [
        r["name_norm"]
        for r in conn.execute(
            "SELECT name_norm FROM item_entities WHERE item_id = ?", (item_id,)
        ).fetchall()
    ]
    cand: set[int] = set()
    if norms:
        ph = ",".join("?" * len(norms))
        cand = {
            int(r["item_id"])
            for r in conn.execute(
                f"SELECT DISTINCT item_id FROM item_entities "  # noqa: S608 (only a ?-placeholder list is interpolated)
                f"WHERE name_norm IN ({ph}) AND item_id != ?",
                [*norms, item_id],
            ).fetchall()
        }
    if not cand:  # fall back to recent items when the target has no entities
        cand = {
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM items WHERE id != ? ORDER BY id DESC LIMIT ?",
                (item_id, _MAX_CANDIDATES),
            ).fetchall()
        }
    cand_ids = list(cand)[:_MAX_CANDIDATES]

    vecs = _ensure_vectors(conn, [item_id, *cand_ids], settings)
    tv = vecs.get(item_id)
    if not tv:
        return {"available": True, "items": []}
    scored = sorted(
        ((_cosine(tv, vecs[c]), c) for c in cand_ids if c in vecs),
        reverse=True,
    )[:limit]
    items = []
    for score, cid in scored:
        r = feed_item(conn, cid)
        if r:
            items.append({
                "id": cid, "title": r.get("title"), "url": r.get("url"),
                "source": r.get("source"), "score": round(score, 3),
            })
    return {"available": True, "items": items}


# ------------------------------------------------------- knowledge-pool search
def _ollama_is_local(settings: Settings) -> bool:
    host = (urlsplit(settings.ollama_base_url or "").hostname or "").lower()
    return host in _LOCAL_HOSTS


def embed_pool(conn: sqlite3.Connection, settings: Settings | None = None,
               limit: int = _POOL_BATCH) -> dict:
    """Embed knowledge-pool and case document chunks that have no vector yet.

    Commits after each chunk, so no write lock is held while waiting for the
    model. Stops at the first failure (no embedder: nothing to do until the next
    scan). "No AI" documents are embedded only by an Ollama on this machine.
    """
    settings = settings or get_settings()
    model = embed_model(settings)
    no_ai = "" if _ollama_is_local(settings) else "AND d.no_ai = 0"
    rows = conn.execute(
        f"""
        SELECT DISTINCT i.id, i.title, i.content
        FROM items i
        JOIN document_items di ON di.item_id = i.id
        JOIN documents d ON d.id = di.document_id
        LEFT JOIN item_embeddings e ON e.item_id = i.id AND e.model = ?
        WHERE e.item_id IS NULL {no_ai}
        ORDER BY i.id DESC LIMIT ?
        """,  # noqa: S608 (only a fixed clause is interpolated)
        (model, limit),
    ).fetchall()
    done = 0
    for row in rows:
        vec = _embed_one(_item_text(dict(row)), settings)
        if not vec:
            return {"embedded": done, "stopped": True}
        conn.execute(
            "INSERT OR REPLACE INTO item_embeddings (item_id, model, vec) VALUES (?, ?, ?)",
            (row["id"], model, json.dumps(vec)),
        )
        conn.commit()
        done += 1
    return {"embedded": done, "stopped": False}


def rrf(rankings: list[list[int]], k: int = _RRF_K) -> list[int]:
    """Reciprocal rank fusion: ids ranked well by either list come first."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, 1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda i: (-scores[i], i))


def _pool_vectors(conn: sqlite3.Connection, model: str, case_id: int | None) -> dict[int, list[float]]:
    from nexus.storage.documents import chunk_scope

    scope, params = chunk_scope(case_id)
    rows = conn.execute(
        f"""
        SELECT DISTINCT e.item_id, e.vec
        FROM item_embeddings e
        JOIN document_items di ON di.item_id = e.item_id
        JOIN documents d ON d.id = di.document_id
        WHERE e.model = ? AND {scope}
        ORDER BY e.item_id DESC LIMIT ?
        """,  # noqa: S608 (a fixed scope clause; values are bound)
        [model, *params, _POOL_VECTORS],
    ).fetchall()
    out: dict[int, list[float]] = {}
    for row in rows:
        with contextlib.suppress(ValueError, TypeError):
            out[int(row["item_id"])] = json.loads(row["vec"])
    return out


def hybrid_chunks(conn: sqlite3.Connection, question: str, terms: list[str],
                  case_id: int | None = None, limit: int = 6,
                  settings: Settings | None = None) -> list[dict]:
    """Best document chunks for a question: full-text and meaning combined.

    Falls back to full-text alone, without any network call, when nothing has
    been embedded with the current model; and after a failed call when the
    embedder is unreachable.
    """
    from nexus.storage.documents import chunks_by_id, search_document_chunks

    settings = settings or get_settings()
    text_rows = search_document_chunks(conn, terms, case_id=case_id, limit=max(limit * 4, 20))
    vectors = _pool_vectors(conn, embed_model(settings), case_id)
    query = _embed_one(question, settings) if vectors else None
    if not query:
        return text_rows[:limit]
    scored = sorted(((_cosine(query, vec), iid) for iid, vec in vectors.items()), reverse=True)
    meaning = [iid for score, iid in scored[:limit * 4] if score >= _MIN_SIMILARITY]
    ids = rrf([[int(r["id"]) for r in text_rows], meaning])[:limit]
    known = {int(r["id"]): r for r in text_rows}
    missing = [i for i in ids if i not in known]
    known.update({int(r["id"]): r for r in chunks_by_id(conn, missing, case_id)})
    return [known[i] for i in ids if i in known]
