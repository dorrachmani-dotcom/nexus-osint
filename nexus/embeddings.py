"""Optional semantic 'find similar' — local embeddings via Ollama.

Finds items whose *meaning* is close to a given item, not just shared keywords.
Embeddings are computed by a LOCAL Ollama model (POST /api/embeddings), so there
is no cloud call and no heavy ML dependency (torch / sentence-transformers). When
Ollama isn't reachable the feature simply reports itself unavailable — graceful
degradation, exactly like the rest of the AI layer.

Vectors are cached in ``item_embeddings`` so each item is embedded at most once;
candidates are limited to items sharing an entity (via the entity index) so a
single click never embeds the whole archive.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import sqlite3

from nexus.config import Settings, get_settings
from nexus.storage import feed_item

logger = logging.getLogger("nexus.embeddings")

_MAX_CANDIDATES = 40
_TIMEOUT = 30.0


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
            json={"model": settings.effective_ollama_model(), "prompt": text[:4000]},
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
    for r in conn.execute(
        f"SELECT item_id, vec FROM item_embeddings WHERE item_id IN ({ph})", ids  # noqa: S608 (only a ?-placeholder list is interpolated)
    ).fetchall():
        with contextlib.suppress(ValueError, TypeError):  # corrupt row: recompute below
            out[int(r["item_id"])] = json.loads(r["vec"])
    model = settings.effective_ollama_model()
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
