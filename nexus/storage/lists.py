"""Analyst lists and bookmarks (pins)."""

from __future__ import annotations

import sqlite3

from nexus.db import last_row_id

# --- Custom triage lists / lanes --------------------------------------------
# Personal organization layer: the analyst creates named lists (e.g. "Very
# interesting", "Not interesting") and files items into any number of them.

# Accent colors the UI knows how to render (keeps user input to a safe set).
_LIST_COLORS = {
    "slate", "emerald", "amber", "red", "sky", "indigo", "fuchsia", "teal", "orange",
}


def create_list(
    conn: sqlite3.Connection, name: str, color: str = "slate",
    case_id: int | None = None,
) -> int:
    """Create a triage list, optionally scoped to a case."""
    name = (name or "").strip()[:60] or "Untitled"
    color = color if color in _LIST_COLORS else "slate"
    if case_id is not None:
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM lists WHERE case_id = ?",
            (case_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO lists (name, color, position, case_id) VALUES (?, ?, ?, ?)",
            (name, color, pos, case_id),
        )
    else:
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM lists WHERE case_id IS NULL"
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO lists (name, color, position) VALUES (?, ?, ?)",
            (name, color, pos),
        )
    return last_row_id(cur)


def rename_list(conn: sqlite3.Connection, list_id: int, name: str) -> None:
    name = (name or "").strip()[:60]
    if name:
        conn.execute("UPDATE lists SET name = ? WHERE id = ?", (name, list_id))


def delete_list(conn: sqlite3.Connection, list_id: int) -> None:
    """Delete a list (memberships cascade; the items themselves are untouched)."""
    conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))


def update_list(conn: sqlite3.Connection, list_id: int, name: str) -> None:
    """Rename a triage list."""
    conn.execute("UPDATE lists SET name = ? WHERE id = ?", (name.strip(), list_id))


def list_lists(conn: sqlite3.Connection, case_id: int | None = None) -> list[dict]:
    """Triage lists in display order, each with its item count.

    When ``case_id`` is given, returns only lists scoped to that case.
    When ``case_id`` is None, returns only global (unscoped) lists.
    """
    if case_id is not None:
        rows = conn.execute(
            """
            SELECT l.id, l.name, l.color, l.position, l.created_at,
                   (SELECT COUNT(*) FROM list_memberships m WHERE m.list_id = l.id) AS item_count,
                   (SELECT MAX(i.fetched_at) FROM list_memberships m2
                    JOIN items i ON i.id = m2.item_id WHERE m2.list_id = l.id) AS last_item_at
            FROM lists l
            WHERE l.case_id = ?
            ORDER BY l.position, l.id
            """,
            (case_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT l.id, l.name, l.color, l.position, l.created_at,
                   (SELECT COUNT(*) FROM list_memberships m WHERE m.list_id = l.id) AS item_count,
                   (SELECT MAX(i.fetched_at) FROM list_memberships m2
                    JOIN items i ON i.id = m2.item_id WHERE m2.list_id = l.id) AS last_item_at
            FROM lists l
            WHERE l.case_id IS NULL
            ORDER BY l.position, l.id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_list(conn: sqlite3.Connection, list_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, name, color, position, created_at FROM lists WHERE id = ?",
        (list_id,),
    ).fetchone()
    return dict(row) if row else None


def add_to_list(conn: sqlite3.Connection, list_id: int, item_id: int) -> None:
    """File an item into a list (idempotent)."""
    conn.execute(
        "INSERT OR IGNORE INTO list_memberships (list_id, item_id) VALUES (?, ?)",
        (list_id, item_id),
    )


def remove_from_list(conn: sqlite3.Connection, list_id: int, item_id: int) -> None:
    conn.execute(
        "DELETE FROM list_memberships WHERE list_id = ? AND item_id = ?",
        (list_id, item_id),
    )


def list_member_items(conn: sqlite3.Connection, list_id: int) -> list[dict]:
    """Full feed rows filed into a list, most-recently-added first."""
    rows = conn.execute(
        """
        SELECT
            i.id, i.source, i.url, i.author, i.title, i.content, i.language,
            i.published_at, i.fetched_at, i.cluster_id,
            COALESCE(c.shared_count, 1) AS shared_count,
            a.threat_level, a.summary, a.translation, a.target_lang,
            a.entities, a.party, a.contradiction, a.confidence
        FROM list_memberships m
        JOIN items i ON i.id = m.item_id
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE m.list_id = ?
        ORDER BY m.added_at DESC
        """,
        (list_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_bookmark(
    conn: sqlite3.Connection, item_id: int, case_id: int | None = None
) -> None:
    """Pin an item, optionally into a case. Idempotent on (item_id, case_id)."""
    conn.execute(
        "INSERT OR IGNORE INTO bookmarks (item_id, case_id) VALUES (?, ?)",
        (item_id, case_id),
    )


def remove_bookmark(
    conn: sqlite3.Connection, item_id: int, case_id: int | None = None
) -> None:
    if case_id is None:
        conn.execute(
            "DELETE FROM bookmarks WHERE item_id = ? AND case_id IS NULL", (item_id,)
        )
    else:
        conn.execute(
            "DELETE FROM bookmarks WHERE item_id = ? AND case_id = ?", (item_id, case_id)
        )
