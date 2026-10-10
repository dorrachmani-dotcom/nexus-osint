"""Items: the single write path (``upsert_item``), FTS query building, feed
search/filtering and per-item read/dismiss state.

`upsert_item` enforces idempotent re-scans (the same post fetched twice is
ignored) while still counting genuine echoes — a *different* post carrying
identical content bumps the cluster's shared_count, which drives the
"🔥 Echoed N times" badge.
"""

from __future__ import annotations

import json
import re
import sqlite3

from nexus.db import last_row_id
from nexus.models import RawItem

# Unicode word characters: \w already covers every script (Latin, Cyrillic,
# CJK, and more) under re.UNICODE, so search tokenizes any language.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def build_fts_query(raw: str | None) -> str | None:
    """Turn free user text into a safe FTS5 MATCH expression.

    Each token is quoted and AND-ed (implicit), so punctuation or FTS operators
    in user input can never produce a syntax error.
    """
    if not raw:
        return None
    tokens = _WORD_RE.findall(raw)
    if not tokens:
        return None
    return " ".join(f'"{t}"' for t in tokens)


def build_fts_or_query(terms: list[str] | None) -> str | None:
    """Match an item against ANY of several terms (used for capsule views).

    Each term is tokenised and AND-ed within itself (so a multi-word term like
    "Elon Musk" stays a phrase), then the terms are OR-ed together. Punctuation
    and FTS operators in user input can never produce a syntax error.
    """
    parts: list[str] = []
    for term in terms or []:
        tokens = _WORD_RE.findall(term or "")
        if tokens:
            phrase = " ".join(f'"{t}"' for t in tokens)
            parts.append(f"({phrase})")
    return " OR ".join(parts) if parts else None


def _bump_cluster(conn: sqlite3.Connection, dedup_key: str) -> int:
    """Increment a cluster's echo counter and return its representative item id."""
    conn.execute(
        "UPDATE clusters SET shared_count = shared_count + 1 WHERE id = ?", (dedup_key,)
    )
    rep = conn.execute(
        "SELECT id FROM items WHERE cluster_id = ? ORDER BY id LIMIT 1", (dedup_key,)
    ).fetchone()
    return int(rep["id"]) if rep else 0


def upsert_item(conn: sqlite3.Connection, item: RawItem) -> tuple[int, bool]:
    """Insert an item, or fold a (near-)duplicate into its cluster.

    Returns (item_id, is_new). Behaviour:
      * Re-fetch of the same post (matched on source+external_id, or identical
        text when no id) -> no-op, no echo bump.
      * Near/exact duplicate of *different* content already stored -> bump the
        cluster's shared_count ("Echoed N times") and do NOT store a new row.
      * Genuinely new content -> store it as the cluster representative.
    Resilient to a concurrent writer inserting the same content_hash.
    """
    content_hash = item.content_hash
    dedup_key = item.dedup_key

    # 1) Re-fetch detection (idempotent re-scans).
    if item.external_id:
        same = conn.execute(
            "SELECT id FROM items WHERE source = ? AND external_id = ?",
            (item.source, item.external_id),
        ).fetchone()
    else:
        same = conn.execute(
            "SELECT id FROM items WHERE content_hash = ?", (content_hash,)
        ).fetchone()
    if same is not None:
        return int(same["id"]), False

    # 2) Echo of already-clustered content.
    cluster = conn.execute("SELECT id FROM clusters WHERE id = ?", (dedup_key,)).fetchone()
    if cluster is not None:
        return _bump_cluster(conn, dedup_key), False

    # 3) New content -> store the representative item.
    conn.execute(
        "INSERT OR IGNORE INTO clusters (id, shared_count) VALUES (?, 1)", (dedup_key,)
    )
    try:
        cur = conn.execute(
            """
            INSERT INTO items (
                content_hash, dedup_key, source, track, external_id, url, author,
                title, content, language, published_at, fetched_at, media_urls, raw, cluster_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_hash,
                dedup_key,
                item.source,
                item.track,
                item.external_id,
                item.url,
                item.author,
                item.title,
                item.content,
                item.language,
                item.published_at.isoformat() if item.published_at else None,
                item.fetched_at.isoformat(),
                json.dumps(item.media_urls),
                json.dumps(item.raw, default=str),
                dedup_key,
            ),
        )
        return last_row_id(cur), True
    except sqlite3.IntegrityError:
        # Lost a race on content_hash -> treat as an echo of the winner.
        return _bump_cluster(conn, dedup_key), False


def list_items(
    conn: sqlite3.Connection,
    limit: int = 100,
    offset: int = 0,
    source: str | None = None,
) -> list[dict]:
    """Newest-first feed rows joined with echo count and (optional) analysis."""
    sql = [
        """
        SELECT
            i.id, i.source, i.url, i.author, i.title, i.content, i.language,
            i.published_at, i.fetched_at, i.cluster_id,
            COALESCE(c.shared_count, 1) AS shared_count,
            a.threat_level, a.summary, a.translation, a.target_lang,
            a.entities, a.party, a.contradiction, a.confidence
        FROM items i
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        """
    ]
    params: list = []
    if source:
        sql.append("WHERE i.source = ?")
        params.append(source)
    sql.append("ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT ? OFFSET ?")
    params.extend([limit, offset])

    rows = conn.execute("\n".join(sql), params).fetchall()
    return [dict(r) for r in rows]


def count_items(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


def distinct_sources(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT source FROM items ORDER BY source").fetchall()
    return [r[0] for r in rows]


def dismiss_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE items SET dismissed = 1 WHERE id = ?", [item_id])
    conn.commit()


def undismiss_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE items SET dismissed = 0 WHERE id = ?", [item_id])
    conn.commit()


def _item_filter_clauses(
    *,
    q: str | None,
    source: str | None,
    threat: str | None,
    since: str | None,
    until: str | None,
    since_ts: str | None,
    unread_only: bool,
    read_only: bool = False,
    show_dismissed: bool = False,
    terms: list[str] | None,
    fetched_since: str | None = None,
) -> tuple[list[str], list]:
    """Build the shared WHERE clauses + params for item search and counting.

    Kept in one place so ``search_items`` and ``count_matching_items`` can never
    drift apart (pagination relies on the count matching the listing exactly).
    """
    where: list[str] = []
    params: list = []

    if terms is not None:
        # Case-scoped feed: restrict to items matching ANY tracking term.
        fts_terms = build_fts_or_query(terms)
        if fts_terms:
            where.append("i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
            params.append(fts_terms)
        # Secondary text search narrows within those results (AND semantics).
        if q and q.strip():
            fts_q = build_fts_query(q.strip())
            if fts_q:
                where.append("i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
                params.append(fts_q)
    else:
        fts = build_fts_query(q)
        if fts:
            fts_clause = "i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)"
            # On the free-text path, also match OCR text from evidence screenshots.
            if q and q.strip():
                where.append(
                    "(" + fts_clause
                    + " OR i.id IN (SELECT item_id FROM evidence "
                    "WHERE ocr_text IS NOT NULL AND ocr_text LIKE ?))"
                )
                params.append(fts)
                params.append(f"%{q.strip()}%")
            else:
                where.append(fts_clause)
                params.append(fts)
    if source:
        where.append("i.source = ?")
        params.append(source)
    if threat:
        where.append("a.threat_level = ?")
        params.append(threat)
    if since:
        where.append("substr(COALESCE(i.published_at, i.fetched_at), 1, 10) >= ?")
        params.append(since)
    if until:
        where.append("substr(COALESCE(i.published_at, i.fetched_at), 1, 10) <= ?")
        params.append(until)
    if since_ts:
        where.append("COALESCE(i.published_at, i.fetched_at) >= ?")
        params.append(since_ts)
    if fetched_since:
        # "Collected since" (not "published since"): an item published days ago
        # but first collected today is still new to the analyst.
        where.append("i.fetched_at >= ?")
        params.append(fetched_since)
    if unread_only:
        where.append("i.id NOT IN (SELECT item_id FROM item_reads)")
    if read_only:
        where.append("i.id IN (SELECT item_id FROM item_reads)")
    if not show_dismissed:
        where.append("COALESCE(i.dismissed, 0) = 0")
    return where, params


def search_items(
    conn: sqlite3.Connection,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,   # 'YYYY-MM-DD'
    until: str | None = None,   # 'YYYY-MM-DD'
    since_ts: str | None = None,  # full ISO timestamp cutoff (for hour-level windows)
    unread_only: bool = False,
    read_only: bool = False,
    show_dismissed: bool = False,
    terms: list[str] | None = None,
    sort: str = "newest",
    limit: int = 100,
    offset: int = 0,
    fetched_since: str | None = None,  # ISO cutoff on collection time
) -> list[dict]:
    """Full-text + faceted search over the entire local history.

    Free text hits items_fts (title/content/summary); the rest are exact-match
    facets. ``since``/``until`` filter on the date part; ``since_ts`` is a full
    ISO timestamp cutoff for hour-level "last N hours" windows. ``unread_only``
    hides items already marked read. When ``terms`` is given (a capsule's terms)
    the text match becomes an OR across those terms instead of the single ``q``
    AND-match.
    """
    select = """
        SELECT
            i.id, i.source, i.url, i.author, i.title, i.content, i.language,
            i.published_at, i.fetched_at, i.cluster_id,
            COALESCE(i.dismissed, 0) AS dismissed,
            COALESCE(c.shared_count, 1) AS shared_count,
            a.threat_level, a.summary, a.translation, a.target_lang,
            a.entities, a.party, a.contradiction, a.confidence
        FROM items i
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
    """
    where, params = _item_filter_clauses(
        q=q, source=source, threat=threat, since=since, until=until,
        since_ts=since_ts, unread_only=unread_only, read_only=read_only,
        show_dismissed=show_dismissed, terms=terms, fetched_since=fetched_since,
    )

    order_dir = "ASC" if sort == "oldest" else "DESC"
    sql = select
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY COALESCE(i.published_at, i.fetched_at) {order_dir} LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_matching_items(
    conn: sqlite3.Connection,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    since_ts: str | None = None,
    unread_only: bool = False,
    show_dismissed: bool = False,
    terms: list[str] | None = None,
    fetched_since: str | None = None,
) -> int:
    """Total number of items matching the same filters ``search_items`` uses.

    Drives pagination: lets the feed know whether more pages exist beyond the
    current ``limit``/``offset`` window.
    """
    where, params = _item_filter_clauses(
        q=q, source=source, threat=threat, since=since, until=until,
        since_ts=since_ts, unread_only=unread_only, show_dismissed=show_dismissed, terms=terms,
        fetched_since=fetched_since,
    )
    sql = (
        "SELECT COUNT(*) FROM items i "
        "LEFT JOIN analyses a ON a.item_id = i.id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(conn.execute(sql, params).fetchone()[0])


# --- Read / unread tracking -------------------------------------------------
def mark_read(conn: sqlite3.Connection, item_id: int) -> None:
    """Mark an item read (idempotent)."""
    conn.execute(
        "INSERT OR IGNORE INTO item_reads (item_id) VALUES (?)", (item_id,)
    )


def mark_unread(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM item_reads WHERE item_id = ?", (item_id,))


def is_read(conn: sqlite3.Connection, item_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM item_reads WHERE item_id = ?", (item_id,)
    ).fetchone()
    return row is not None
