"""Claim verification — does the rest of the local data back up an item's claim?

Given one item, this finds OTHER collected items that mention the same entities
(via the entity index — independent stories, not echoes of the same one) and asks
the configured AI whether they corroborate or contradict the item's main claim.
Read-only and fail-soft: with no AI provider, or no comparable items, it returns a
clear, honest verdict instead of guessing. Nothing here runs anything offensive —
it only reasons over already-collected open-source items.
"""

from __future__ import annotations

import logging
import sqlite3

from nexus.config import Settings, get_settings
from nexus.storage import feed_item

logger = logging.getLogger("nexus.verify")

_MAX_CANDIDATES = 10


def _truncate(text, n: int) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n].rstrip() + "…"


def _candidates(conn: sqlite3.Connection, item: dict) -> list[dict]:
    """Independent items that mention the same entities as ``item``.

    Uses the entity index. Excludes the item itself and any item in the same
    cluster (those are echoes of the *same* story, not independent corroboration).
    Ranked by how many entities they share.
    """
    iid = int(item["id"])
    norms = [
        r["name_norm"]
        for r in conn.execute(
            "SELECT name_norm FROM item_entities WHERE item_id = ?", (iid,)
        ).fetchall()
    ]
    if not norms:
        return []
    ph = ",".join("?" * len(norms))
    rows = conn.execute(
        f"""
        SELECT i.id, i.source, i.url, i.title, i.cluster_id, i.content,
               a.summary, COUNT(*) AS shared
        FROM item_entities ie
        JOIN items i ON i.id = ie.item_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE ie.name_norm IN ({ph}) AND ie.item_id != ?
        GROUP BY i.id
        ORDER BY shared DESC, COALESCE(i.published_at, i.fetched_at) DESC
        LIMIT ?
        """,  # noqa: S608 (only a ?-placeholder list is interpolated)
        [*norms, iid, _MAX_CANDIDATES * 3],
    ).fetchall()
    cluster = item.get("cluster_id")
    out: list[dict] = []
    for r in rows:
        rd = dict(r)
        if cluster and rd.get("cluster_id") == cluster:
            continue  # same story (echo), not independent corroboration
        out.append(rd)
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


def verify_item(
    conn: sqlite3.Connection, item_id: int, settings: Settings | None = None
) -> dict:
    """Cross-check one item's claim against other collected items.

    Returns ``{ok, verdict, note, corroborating[], contradicting[], provider}`` or
    ``{ok: False, error}``. ``verdict`` is one of: ``corroborated`` /
    ``disputed`` / ``uncorroborated`` / ``single-source``. Never raises.
    """
    settings = settings or get_settings()
    item = feed_item(conn, item_id)
    if item is None:
        return {"ok": False, "error": "Item not found."}
    if settings.active_provider() == "off":
        return {
            "ok": False,
            "error": "Verification needs an AI model — add one in Settings, then try again.",
        }

    cands = _candidates(conn, item)
    if not cands:
        return {
            "ok": True,
            "verdict": "single-source",
            "note": (
                "No other collected items mention the same entities, so this claim "
                "can't be cross-checked yet. Run a scan to gather more, then retry."
            ),
            "corroborating": [],
            "contradicting": [],
            "provider": settings.active_provider(),
        }

    from nexus.analysis.providers import get_provider
    from nexus.assistant import _extract_json

    provider = get_provider(settings)
    if provider is None:
        return {"ok": False, "error": "The configured AI provider could not be started."}

    claim = (item.get("title") or "").strip()
    if item.get("summary"):
        claim += f" — {item['summary']}"
    elif item.get("content"):
        claim += f" — {_truncate(item.get('content'), 240)}"

    lines = []
    for i, c in enumerate(cands, 1):
        body = _truncate(c.get("summary") or c.get("content"), 200)
        lines.append(
            f"Item {i} [{c.get('source') or '?'}]: {_truncate(c.get('title'), 120) or '(untitled)'}"
            + (f" — {body}" if body else "")
        )
    corpus = "\n".join(lines)

    sys_prompt = (
        "You are an OSINT verification assistant. Decide whether the OTHER collected "
        "items corroborate or contradict the CLAIM. Judge ONLY from the text provided; "
        "do not use outside knowledge. An item that does not actually address the claim "
        "belongs in NEITHER list. Reply with ONLY a JSON object: "
        '{"verdict": "corroborated|disputed|uncorroborated", '
        '"summary": "<one short sentence>", '
        '"corroborating": [item numbers], "contradicting": [item numbers]}. '
        "Use 'disputed' if any item contradicts the claim; 'corroborated' if some "
        "support it and none contradict; 'uncorroborated' if none address it."
    )
    usr_prompt = f"CLAIM: {_truncate(claim, 400)}\n\nOTHER COLLECTED ITEMS:\n{corpus}"

    try:
        parsed = _extract_json(provider.complete(sys_prompt, usr_prompt, max_tokens=700)) or {}
    except Exception:
        logger.exception("Verify: provider call failed")
        return {"ok": False, "error": "The AI model could not be reached. Please try again."}

    def _pick(nums) -> list[dict]:
        out: list[dict] = []
        seen: set[int] = set()
        for n in nums or []:
            try:
                idx = int(n) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(cands) and idx not in seen:
                seen.add(idx)
                c = cands[idx]
                out.append({
                    "id": c["id"], "title": c.get("title"),
                    "url": c.get("url"), "source": c.get("source"),
                })
        return out

    corroborating = _pick(parsed.get("corroborating"))
    contradicting = _pick(parsed.get("contradicting"))
    verdict = parsed.get("verdict")
    if verdict not in ("corroborated", "disputed", "uncorroborated"):
        verdict = (
            "disputed" if contradicting
            else "corroborated" if corroborating
            else "uncorroborated"
        )
    return {
        "ok": True,
        "verdict": verdict,
        "note": (parsed.get("summary") or "").strip(),
        "corroborating": corroborating,
        "contradicting": contradicting,
        "provider": settings.active_provider(),
    }
