"""Intelligence requirements, their scoring queue and the intel views."""

from __future__ import annotations

import sqlite3

from nexus.db import last_row_id
from nexus.storage.cases import _NO_CASE_FILTER
from nexus.storage.items import build_fts_query


# --- Intelligence Requirements (PIRs) ---------------------------------------
# Standing questions the analyst wants answered. The AI scores items against the
# enabled requirements; the Intel view ranks material by that relevance score.
def add_requirement(
    conn: sqlite3.Connection,
    question: str,
    priority: int = 2,
    topic: str | None = None,
    case_id: int | None = None,
) -> int:
    """Add a standing requirement (intelligence question).

    ``case_id`` binds the question to one Case (its Questions tab). None = a
    global question (shown on the standalone Intel/Requirements view). ``topic``
    is the legacy grouping key, kept only for backward compatibility.
    """
    topic = (topic or "").strip() or None
    cur = conn.execute(
        "INSERT INTO requirements (question, priority, enabled, topic, case_id) "
        "VALUES (?, ?, 1, ?, ?)",
        (question, priority, topic, case_id),
    )
    return last_row_id(cur)


def list_requirements(
    conn: sqlite3.Connection,
    only_enabled: bool = False,
    topic: str | None = None,
    case_id=_NO_CASE_FILTER,
) -> list[dict]:
    """All requirements (newest first) with how many items matched each.

    ``case_id`` filtering: omit it for all questions; pass ``None`` for only the
    global ones (not bound to any case); pass an int for one case's questions.
    """
    sql = """
        SELECT r.id, r.question, r.priority, r.enabled, r.created_at, r.topic,
               r.case_id,
               (SELECT COUNT(*) FROM requirement_hits h
                WHERE h.requirement_id = r.id AND h.score >= 1) AS match_count
        FROM requirements r
    """
    clauses: list[str] = []
    params: list = []
    if only_enabled:
        clauses.append("r.enabled = 1")
    if topic is not None:
        clauses.append("r.topic = ?")
        params.append((topic or "").strip())
    if case_id is not _NO_CASE_FILTER:
        if case_id is None:
            clauses.append("r.case_id IS NULL")
        else:
            clauses.append("r.case_id = ?")
            params.append(int(case_id))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY r.priority, r.id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_requirement_enabled(
    conn: sqlite3.Connection, req_id: int, enabled: bool
) -> None:
    conn.execute(
        "UPDATE requirements SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, req_id),
    )


def delete_requirement(conn: sqlite3.Connection, req_id: int) -> None:
    conn.execute("DELETE FROM requirements WHERE id = ?", (req_id,))


def active_requirements(conn: sqlite3.Connection) -> list[dict]:
    """Enabled requirements, highest priority first (for the scoring pass)."""
    rows = conn.execute(
        """
        SELECT id, question, priority FROM requirements
        WHERE enabled = 1 ORDER BY priority, id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def unscored_requirements_for_item(
    conn: sqlite3.Connection, item_id: int
) -> list[dict]:
    """Enabled requirements this item has not been scored against yet."""
    rows = conn.execute(
        """
        SELECT r.id, r.question, r.priority FROM requirements r
        WHERE r.enabled = 1
          AND NOT EXISTS (
              SELECT 1 FROM requirement_hits h
              WHERE h.item_id = ? AND h.requirement_id = r.id
          )
        ORDER BY r.priority, r.id
        """,
        (item_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def pending_requirement_scoring(
    conn: sqlite3.Connection, limit: int = 200
) -> list[dict]:
    """Newest items that still have at least one unscored enabled requirement.

    The work queue for the PIR scoring pass. Empty when no requirements exist,
    so the pass is a no-op until the analyst defines some.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.source, i.title, i.content, i.language
        FROM items i
        WHERE EXISTS (
            SELECT 1 FROM requirements r
            WHERE r.enabled = 1
              AND NOT EXISTS (
                  SELECT 1 FROM requirement_hits h
                  WHERE h.item_id = i.id AND h.requirement_id = r.id
              )
        )
        ORDER BY i.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_requirement_hit(
    conn: sqlite3.Connection,
    item_id: int,
    requirement_id: int,
    score: int,
    rationale: str | None,
    model: str | None,
) -> None:
    """Persist (idempotent) a relevance score of an item to a requirement."""
    conn.execute(
        """
        INSERT INTO requirement_hits
            (item_id, requirement_id, score, rationale, model, scored_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(item_id, requirement_id) DO UPDATE SET
            score = excluded.score,
            rationale = excluded.rationale,
            model = excluded.model,
            scored_at = excluded.scored_at
        """,
        (item_id, requirement_id, int(score), rationale, model),
    )


def intel_items(
    conn: sqlite3.Connection,
    requirement_id: int | None = None,
    min_score: int = 1,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Items ranked by Intelligence-Requirement relevance, highest first.

    With a `requirement_id`, returns that requirement's matches. Without one,
    returns each matching item once at its best score across all requirements.
    """
    base_cols = """
        i.id, i.source, i.url, i.author, i.title, i.content, i.language,
        i.published_at, i.fetched_at, i.cluster_id,
        COALESCE(c.shared_count, 1) AS shared_count,
        a.threat_level, a.summary, a.translation, a.target_lang,
        a.entities, a.party, a.contradiction, a.confidence
    """
    where = ["h.score >= ?"]
    params: list = [min_score]
    if requirement_id:
        where.append("h.requirement_id = ?")
        params.append(requirement_id)
    if since:
        where.append("substr(COALESCE(i.published_at, i.fetched_at), 1, 10) >= ?")
        params.append(since)
    if until:
        where.append("substr(COALESCE(i.published_at, i.fetched_at), 1, 10) <= ?")
        params.append(until)
    if q:
        fts_q = build_fts_query(q.strip())
        if fts_q:
            where.append("i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
            params.append(fts_q)
    where_sql = " AND ".join(where)

    if requirement_id:
        sql = f"""
            SELECT {base_cols},
                   h.score AS rel_score, h.rationale AS rel_rationale,
                   h.requirement_id AS rel_requirement_id, 1 AS rel_match_count
            FROM requirement_hits h
            JOIN items i ON i.id = h.item_id
            LEFT JOIN clusters c ON c.id = i.cluster_id
            LEFT JOIN analyses a ON a.item_id = i.id
            WHERE {where_sql}
            ORDER BY h.score DESC, COALESCE(i.published_at, i.fetched_at) DESC
            LIMIT ? OFFSET ?
        """
    else:
        sql = f"""
            SELECT {base_cols},
                   MAX(h.score) AS rel_score, NULL AS rel_rationale,
                   NULL AS rel_requirement_id,
                   COUNT(DISTINCT h.requirement_id) AS rel_match_count
            FROM requirement_hits h
            JOIN items i ON i.id = h.item_id
            LEFT JOIN clusters c ON c.id = i.cluster_id
            LEFT JOIN analyses a ON a.item_id = i.id
            WHERE {where_sql}
            GROUP BY i.id
            ORDER BY rel_score DESC, COALESCE(i.published_at, i.fetched_at) DESC
            LIMIT ? OFFSET ?
        """
    params.extend([limit, offset])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
