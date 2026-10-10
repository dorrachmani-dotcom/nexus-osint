"""Watchlists and their hits, with ReDoS-bounded regex matching."""

from __future__ import annotations

import re
import sqlite3

from nexus.db import last_row_id
from nexus.storage.meta import get_meta, set_meta

# ReDoS guardrails for user-authored watchlist regexes. We can't portably
# interrupt a running regex (no SIGALRM on Windows), so we bound the two inputs
# that drive catastrophic backtracking instead: the pattern length and the
# length of the text it runs against. A few KB of haystack keeps even a
# pathological pattern's worst case fast, and no real keyword/regex needs to be
# hundreds of characters long.
_MAX_REGEX_PATTERN_LEN = 500


_MAX_REGEX_HAYSTACK = 8192


# --- Watchlists + alerts ----------------------------------------------------
# A watchlist is a pattern (keyword / regex / literal token like a wallet or
# phone number). New items are matched against every watchlist during a scan;
# a match records a watchlist_hit, which surfaces as an alert in the UI.
def create_watchlist(
    conn: sqlite3.Connection, label: str, pattern: str, kind: str = "keyword"
) -> int:
    # Cap regex pattern length up front (ReDoS guardrail): a runaway pattern
    # never gets stored, so it can't be evaluated on every scanned item.
    if kind == "regex":
        pattern = (pattern or "")[:_MAX_REGEX_PATTERN_LEN]
    cur = conn.execute(
        "INSERT INTO watchlists (label, pattern, kind) VALUES (?, ?, ?)",
        (label, pattern, kind),
    )
    return last_row_id(cur)


def list_watchlists(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT w.id, w.label, w.pattern, w.kind, w.enabled, w.created_at,
               (SELECT COUNT(*) FROM watchlist_hits h WHERE h.watchlist_id = w.id) AS hit_count
        FROM watchlists w
        ORDER BY w.id DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def toggle_watchlist(conn: sqlite3.Connection, watchlist_id: int) -> None:
    conn.execute(
        "UPDATE watchlists SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END WHERE id = ?",
        (watchlist_id,),
    )


def delete_watchlist(conn: sqlite3.Connection, watchlist_id: int) -> None:
    conn.execute("DELETE FROM watchlists WHERE id = ?", (watchlist_id,))


def list_watchlist_hits(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Recent alerts: each hit joined to its watchlist and the matched item."""
    rows = conn.execute(
        """
        SELECT h.id, h.hit_at, w.label, w.pattern, w.kind,
               i.id AS item_id, i.source, i.title, i.url,
               COALESCE(i.summary, '') AS summary, i.content
        FROM watchlist_hits h
        JOIN watchlists w ON w.id = h.watchlist_id
        JOIN items i ON i.id = h.item_id
        ORDER BY h.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_new_watchlist_hits(conn: sqlite3.Connection) -> int:
    """Return the number of watchlist hits the analyst hasn't seen yet."""
    last_raw = get_meta(conn, "watchlist_last_seen_hit_id") or "0"
    try:
        last_id = int(last_raw)
    except ValueError:
        last_id = 0
    row = conn.execute(
        "SELECT COUNT(*) FROM watchlist_hits WHERE id > ?", (last_id,)
    ).fetchone()
    return row[0] if row else 0


def mark_watchlist_hits_seen(conn: sqlite3.Connection) -> None:
    """Record that the analyst has now seen all current hits."""
    row = conn.execute("SELECT MAX(id) FROM watchlist_hits").fetchone()
    max_id = row[0] if row and row[0] is not None else 0
    set_meta(conn, "watchlist_last_seen_hit_id", str(max_id))


def _matches(pattern: str, kind: str, text: str) -> bool:
    if not pattern or not text:
        return False
    if kind == "regex":
        try:
            # Bound the haystack so a pathological pattern can't backtrack for an
            # unbounded time on a long item body (ReDoS guardrail).
            return re.search(pattern[:_MAX_REGEX_PATTERN_LEN], text[:_MAX_REGEX_HAYSTACK],
                             re.IGNORECASE) is not None
        except re.error:
            return False
    # keyword / wallet / phone -> case-insensitive substring match.
    return pattern.lower() in text.lower()


def match_watchlists(conn: sqlite3.Connection, item_id: int, text: str) -> int:
    """Record a hit for every watchlist whose pattern matches `text`.

    Idempotent per (watchlist, item) via the table's UNIQUE constraint. Returns
    the number of new hits recorded.
    """
    watchlists = conn.execute(
        "SELECT id, pattern, kind FROM watchlists WHERE enabled = 1"
    ).fetchall()
    hits = 0
    for w in watchlists:
        if _matches(w["pattern"], w["kind"], text):
            cur = conn.execute(
                "INSERT OR IGNORE INTO watchlist_hits (watchlist_id, item_id) VALUES (?, ?)",
                (w["id"], item_id),
            )
            hits += cur.rowcount or 0
    return hits
