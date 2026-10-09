"""Case-scoped item queries: live/new/reviewed items, unread counts,
timeline, pinned items, evidence and related cases.
"""

from __future__ import annotations

import sqlite3

from nexus.storage.cases import case_terms, get_case_last_visit
from nexus.storage.entities import _entity_tokens
from nexus.storage.items import build_fts_or_query, count_matching_items, mark_read, search_items
from nexus.storage.requirements import list_requirements


def case_timeline(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """Return all pinned items for a case sorted oldest-first, each row including
    its date (YYYY-MM-DD) so the template can group them into day bands."""
    rows = conn.execute(
        """
        SELECT i.id, i.source, i.url, i.title,
               COALESCE(i.published_at, i.fetched_at) AS ts,
               substr(COALESCE(i.published_at, i.fetched_at), 1, 10) AS day,
               a.threat_level, a.summary
        FROM bookmarks bm
        JOIN items i ON i.id = bm.item_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE bm.case_id = ?
        ORDER BY ts ASC NULLS LAST
        """,
        [case_id],
    ).fetchall()
    return [dict(r) for r in rows]


def _required_terms_for_case(conn: sqlite3.Connection, case_id: int) -> list[str]:
    """Return the effective required search terms for a case.

    For a sub-case this includes the parent's required terms (HERITAGE inheritance)
    so the sub-case feed is a superset of the parent's coverage plus its own focus.
    """
    rows = conn.execute(
        "SELECT term FROM case_terms WHERE case_id = ? AND kind = 'required' ORDER BY id",
        (case_id,),
    ).fetchall()
    terms = [r["term"] for r in rows]

    case_row = conn.execute("SELECT parent_id FROM cases WHERE id = ?", (case_id,)).fetchone()
    parent_id = case_row["parent_id"] if case_row else None
    if parent_id:
        parent_rows = conn.execute(
            "SELECT term FROM case_terms WHERE case_id = ? AND kind = 'required' ORDER BY id",
            (parent_id,),
        ).fetchall()
        seen = set(terms)
        for r in parent_rows:
            if r["term"] not in seen:
                terms.append(r["term"])
    return terms


def case_alert_terms(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """Alert words for a case ({id, term}) — passive flags on incoming items only."""
    rows = conn.execute(
        "SELECT id, term FROM case_terms WHERE case_id = ? AND kind = 'alert' ORDER BY id",
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def case_live_items(
    conn: sqlite3.Connection,
    case_id: int,
    limit: int = 100,
    unread_only: bool = True,
    source: str | None = None,
    since: str | None = None,
    q: str | None = None,
    sort: str = "newest",
) -> list[dict]:
    """Unread (queue) items for a case: matching required terms, newest first.

    Defaults to ``unread_only=True`` so the feed is a triage queue — items
    disappear once marked read and only re-appear when new material arrives.
    Pass ``unread_only=False`` for reports/briefings that need the full history.
    Sub-cases inherit parent required terms automatically.
    """
    terms = _required_terms_for_case(conn, case_id)
    if not terms:
        return []
    return search_items(
        conn, q=q, terms=terms, limit=limit,
        unread_only=unread_only, source=source, since=since, sort=sort,
    )


def case_unread_count(conn: sqlite3.Connection, case_id: int) -> int:
    """Total unread items matching this case's required terms (ignores last-visit cutoff)."""
    terms = _required_terms_for_case(conn, case_id)
    if not terms:
        return 0
    fts = build_fts_or_query(terms)
    if not fts:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM items WHERE id IN "
        "(SELECT rowid FROM items_fts WHERE items_fts MATCH ?) "
        "AND id NOT IN (SELECT item_id FROM item_reads)",
        (fts,),
    ).fetchone()
    return row[0] if row else 0


def case_reviewed_items(
    conn: sqlite3.Connection,
    case_id: int,
    limit: int = 100,
    source: str | None = None,
    since: str | None = None,
    q: str | None = None,
    sort: str = "newest",
) -> list[dict]:
    """Items matching this case's required terms that have been marked as read (reviewed)."""
    terms = _required_terms_for_case(conn, case_id)
    if not terms:
        return []
    return search_items(
        conn, q=q, terms=terms, limit=limit,
        read_only=True, source=source, since=since, sort=sort,
    )


def case_new_count(
    conn: sqlite3.Connection, case_id: int, terms: list[str] | None = None
) -> int:
    """How many tracked items have arrived since the case was last opened.

    Before the first visit there is no cutoff, so this is the full live count
    ('you have N items to review'); afterwards it is the delta since last visit.
    Only required terms are counted — alert words are passive and don't inflate
    the badge. ``terms`` may be passed in (from :func:`case_terms_map` with
    ``kind='required'``) to avoid an extra per-case query on the board.
    """
    if terms is None:
        terms = _required_terms_for_case(conn, case_id)
    if not terms:
        return 0
    return count_matching_items(
        conn, terms=terms, since_ts=get_case_last_visit(conn, case_id)
    )


def case_new_items(
    conn: sqlite3.Connection, case_id: int, limit: int = 20
) -> list[dict]:
    """The tracked items that arrived since the case was last opened (newest first)."""
    terms = [t["term"] for t in case_terms(conn, case_id)]
    if not terms:
        return []
    return search_items(
        conn, terms=terms, since_ts=get_case_last_visit(conn, case_id), limit=limit
    )


def case_items_collected_since(
    conn: sqlite3.Connection, case_id: int, since_ts: str, limit: int = 20
) -> tuple[int, list[dict]]:
    """(count, newest items) of a case's tracked items COLLECTED since ``since_ts``.

    Used by the daily email brief and the scheduled daily case report, which
    report on "what arrived since the last run" regardless of whether the
    analyst opened the case. A case with no tracking terms yields (0, []) —
    never the whole archive.
    """
    terms = _required_terms_for_case(conn, case_id)
    if not terms:
        return 0, []
    total = count_matching_items(conn, terms=terms, fetched_since=since_ts)
    if total <= 0:
        return 0, []
    items = search_items(conn, terms=terms, fetched_since=since_ts, limit=limit)
    return total, items


def mark_case_read(conn: sqlite3.Connection, case_id: int, limit: int = 2000) -> int:
    """Mark every one of a case's tracked items as read. Returns how many."""
    items = case_live_items(conn, case_id, limit=limit)
    for it in items:
        mark_read(conn, it["id"])
    return len(items)


def case_question_items(
    conn: sqlite3.Connection, case_id: int, min_score: int = 1, limit: int = 100
) -> list[dict]:
    """Items scored against THIS case's questions, most relevant first.

    A single scoped join (best score per item across the case's requirements),
    so a case with thousands of scored items stays instant.
    """
    req_ids = [int(r["id"]) for r in list_requirements(conn, case_id=case_id)]
    if not req_ids:
        return []
    placeholders = ",".join("?" * len(req_ids))
    sql = f"""
        SELECT
            i.id, i.source, i.url, i.author, i.title, i.content, i.language,
            i.published_at, i.fetched_at, i.cluster_id,
            COALESCE(c.shared_count, 1) AS shared_count,
            a.threat_level, a.summary, a.translation, a.target_lang,
            a.entities, a.party, a.contradiction, a.confidence,
            MAX(h.score) AS rel_score,
            COUNT(DISTINCT h.requirement_id) AS rel_match_count
        FROM requirement_hits h
        JOIN items i ON i.id = h.item_id
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE h.requirement_id IN ({placeholders}) AND h.score >= ?
        GROUP BY i.id
        ORDER BY rel_score DESC, COALESCE(i.published_at, i.fetched_at) DESC
        LIMIT ?
    """
    return [dict(r) for r in conn.execute(sql, [*req_ids, min_score, limit]).fetchall()]


def case_items(
    conn: sqlite3.Connection, case_id: int, query: str | None = None
) -> list[dict]:
    """Full item rows (with analysis) bookmarked into a case, newest first.

    Optional `query` does a simple case-insensitive substring match across the
    item title, content, summary and entities — a quick within-case filter.
    """
    sql = """
        SELECT
            i.id, i.source, i.url, i.author, i.title, i.content, i.language,
            i.published_at, i.fetched_at, i.cluster_id,
            COALESCE(c.shared_count, 1) AS shared_count,
            a.threat_level, a.summary, a.translation, a.target_lang,
            a.entities, a.party, a.contradiction, a.confidence
        FROM bookmarks bm
        JOIN items i ON i.id = bm.item_id
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE bm.case_id = ?
    """
    params: list = [case_id]
    q = (query or "").strip()
    if q:
        like = f"%{q}%"
        sql += """
          AND (i.title LIKE ? OR i.content LIKE ?
               OR a.summary LIKE ? OR a.entities LIKE ?)
        """
        params += [like, like, like, like]
    sql += " ORDER BY COALESCE(i.published_at, i.fetched_at) DESC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def case_evidence(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """Every evidence screenshot captured for items pinned in a case.

    The basis of a chain-of-custody manifest: each row carries the SHA-256 hash
    and capture timestamp recorded at capture time, plus the item it documents.
    Ordered oldest-first (chronological provenance).
    """
    rows = conn.execute(
        """
        SELECT e.id, e.sha256, e.captured_at, e.screenshot,
               i.id AS item_id, i.title, i.url, i.source
        FROM evidence e
        JOIN items i ON i.id = e.item_id
        JOIN bookmarks b ON b.item_id = e.item_id AND b.case_id = ?
        GROUP BY e.id
        ORDER BY e.captured_at ASC, e.id ASC
        """,
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def related_cases(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """Other cases connected to this one, for pivoting across investigations.

    Two kinds of link are detected and merged per other-case:
      - shared_items: the exact same item is pinned in both cases.
      - shared_entities: an entity (person/org/wallet/domain…) named in this
        case's material also appears in the other case's material.
    Returns a list sorted by strength (shared items first, then entity overlap).
    """
    # This case's pinned items + their entity tokens.
    mine = case_items(conn, case_id)
    if not mine:
        return []
    my_item_ids = {int(r["id"]) for r in mine}
    my_tokens: set[str] = set()
    for r in mine:
        my_tokens |= _entity_tokens(r.get("entities"))

    # Every other open/closed case and its pinned items (single pass).
    others: dict[int, dict] = {}
    rows = conn.execute(
        """
        SELECT c.id AS cid, c.name AS cname,
               COALESCE(c.status,'open') AS status,
               COALESCE(c.priority,'medium') AS priority,
               i.id AS iid, i.title AS ititle, a.entities AS entities
        FROM cases c
        JOIN bookmarks bm ON bm.case_id = c.id
        JOIN items i ON i.id = bm.item_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE c.id != ?
        """,
        (case_id,),
    ).fetchall()
    for row in rows:
        cid = int(row["cid"])
        bucket = others.setdefault(
            cid,
            {
                "id": cid,
                "name": row["cname"],
                "status": row["status"],
                "priority": row["priority"],
                "shared_items": [],
                "shared_entities": set(),
            },
        )
        iid = int(row["iid"])
        if iid in my_item_ids:
            bucket["shared_items"].append({"id": iid, "title": row["ititle"]})
        overlap = my_tokens & _entity_tokens(row["entities"])
        if overlap:
            bucket["shared_entities"] |= overlap

    out: list[dict] = []
    for b in others.values():
        if not b["shared_items"] and not b["shared_entities"]:
            continue
        # Dedup shared items by id; cap entity list for display.
        seen: set[int] = set()
        uniq_items = []
        for it in b["shared_items"]:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            uniq_items.append(it)
        b["shared_items"] = uniq_items
        b["shared_entities"] = sorted(b["shared_entities"])[:12]
        out.append(b)

    out.sort(
        key=lambda b: (-len(b["shared_items"]), -len(b["shared_entities"]), b["name"].lower())
    )
    return out
