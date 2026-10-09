"""AI enrichment persistence: the analysis queue, saved analyses (and the
entity index they feed) and keyless translations.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from nexus.storage.entities import (
    _clean_entity_display,
    _decode_entities,
    _entity_kind_map,
    _norm_entity_key,
)

logger = logging.getLogger("nexus.storage")


# --- Analysis persistence ---------------------------------------------------
# The intelligence layer is decoupled from collection: items land first, then a
# separate pass enriches them. `pending_analysis` finds rows that still need
# Claude; `save_analysis` writes the result back and mirrors the summary into
# items.summary so it becomes full-text searchable.
def pending_analysis(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Newest stored items that have no analysis row yet.

    Used by the Claude pipeline as its work queue. `limit` is the per-run cap
    (budget guard) so a large backlog can never blow the token budget at once.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.source, i.title, i.content, i.language
        FROM items i
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE a.item_id IS NULL
        ORDER BY i.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_analysis(conn: sqlite3.Connection, item_id: int, analysis) -> None:
    """Persist an Analysis for an item (idempotent on item_id).

    Also copies the summary onto items.summary; the FTS update trigger then
    folds it into items_fts so analyst summaries are searchable.
    """
    # Persist the typed entity object when the model supplied one (backward
    # compatible: older rows hold a flat JSON array; readers handle both).
    entity_groups = getattr(analysis, "entity_groups", None) or {}
    if any(entity_groups.values()):
        entities_json = json.dumps(entity_groups, ensure_ascii=False)
    else:
        entities_json = json.dumps(analysis.entities, ensure_ascii=False)

    conn.execute(
        """
        INSERT INTO analyses (
            item_id, threat_level, summary, translation, target_lang,
            entities, party, contradiction, confidence, model, analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            threat_level  = excluded.threat_level,
            summary       = excluded.summary,
            translation   = excluded.translation,
            target_lang   = excluded.target_lang,
            entities      = excluded.entities,
            party         = excluded.party,
            contradiction = excluded.contradiction,
            confidence    = excluded.confidence,
            model         = excluded.model,
            analyzed_at   = excluded.analyzed_at
        """,
        (
            item_id,
            analysis.threat_level.value,
            analysis.summary,
            analysis.translation,
            analysis.target_lang,
            entities_json,
            analysis.party.value,
            1 if analysis.contradiction else 0,
            getattr(analysis, "confidence", None),
            analysis.model,
            analysis.analyzed_at.isoformat() if analysis.analyzed_at else None,
        ),
    )
    # Mirror summary into items so it is indexed by items_fts (UPDATE trigger).
    if analysis.summary:
        conn.execute(
            "UPDATE items SET summary = ? WHERE id = ?", (analysis.summary, item_id)
        )
    # Keep the entity index in sync (powers the entity dossier + faster graph).
    _reindex_item_entities(conn, item_id, entities_json)


def _reindex_item_entities(conn: sqlite3.Connection, item_id: int, entities_raw) -> None:
    """Rebuild item_entities rows for one item from its entities JSON.

    Idempotent: clears this item's rows then re-inserts the de-duplicated set, so
    a re-analysis updates the index cleanly. Fail-soft — an indexing hiccup must
    never break saving an analysis (graceful degradation)."""
    try:
        groups, flat = _decode_entities(entities_raw)
        kind_map = _entity_kind_map(groups)
        conn.execute("DELETE FROM item_entities WHERE item_id = ?", (item_id,))
        seen: set[str] = set()
        for raw_name in flat:
            norm = _norm_entity_key(raw_name)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            conn.execute(
                "INSERT OR IGNORE INTO item_entities "
                "(item_id, name, name_norm, kind) VALUES (?, ?, ?, ?)",
                (item_id, _clean_entity_display(raw_name), norm,
                 kind_map.get(norm, "entity")),
            )
    except Exception:
        logger.exception("entity index update failed for item %s", item_id)


# --- Keyless translation fallback -------------------------------------------
# A separate, optional pass renders non-English items into English when no AI
# translation exists (e.g. no AI provider is configured). It reuses the same
# analyses.translation slot the feed already reads, so no template change is
# needed. It must NEVER clobber a higher-quality AI translation.
def pending_keyless_translation(
    conn: sqlite3.Connection, limit: int = 200
) -> list[dict]:
    """Items that still lack ANY English translation, newest first.

    Returns rows with no analysis row at all, OR an analysis row whose
    ``translation`` is empty. Such rows are the only candidates the keyless pass
    may write to (rows that already carry a translation are left untouched).
    """
    rows = conn.execute(
        """
        SELECT i.id, i.source, i.title, i.content, i.language
        FROM items i
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE a.item_id IS NULL
           OR a.translation IS NULL
           OR TRIM(a.translation) = ''
        ORDER BY i.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_keyless_translation(
    conn: sqlite3.Connection,
    item_id: int,
    translation: str,
    target_lang: str,
    model: str,
) -> bool:
    """Store a keyless English rendering into the existing translation slot.

    Idempotent and non-destructive: writes ONLY when the row has no translation
    yet, so an AI-produced (higher-quality) translation is never overwritten.
    Creates a minimal analyses row when none exists. Returns True if it wrote.
    """
    cur = conn.execute(
        "SELECT item_id, translation FROM analyses WHERE item_id = ?", (item_id,)
    ).fetchone()

    if cur is None:
        # No analysis row at all — create a minimal one carrying just the
        # translation (threat_level/party keep their schema defaults).
        conn.execute(
            """
            INSERT INTO analyses (item_id, translation, target_lang, model, analyzed_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (item_id, translation, target_lang, model),
        )
        return True

    existing = (cur["translation"] or "").strip()
    if existing:
        # Guard: a translation already exists (AI or earlier keyless pass). Do
        # NOT clobber it.
        return False

    conn.execute(
        """
        UPDATE analyses
        SET translation = ?, target_lang = ?
        WHERE item_id = ? AND (translation IS NULL OR TRIM(translation) = '')
        """,
        (translation, target_lang, item_id),
    )
    return True
