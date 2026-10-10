"""Wayback archive state for items and cases."""

from __future__ import annotations

import sqlite3

# --- Internet Archive (Wayback Machine) captures ---------------------------
# One row per item: the latest archive attempt. Written by nexus.wayback's
# background queue; read by the item drawer, the case page and the exports.
ARCHIVE_STATUSES = ("pending", "done", "failed", "existing")


def set_item_archive(
    conn: sqlite3.Connection,
    item_id: int,
    url: str,
    status: str,
    *,
    archive_url: str | None = None,
    archived_at: str | None = None,
    error: str | None = None,
) -> None:
    """Upsert the latest archive attempt for an item.

    A new ``pending`` request resets ``requested_at`` and clears any old error;
    a finished attempt keeps the original ``requested_at``.
    """
    if status not in ARCHIVE_STATUSES:
        raise ValueError(f"bad archive status: {status!r}")
    if status == "pending":
        conn.execute(
            """
            INSERT INTO item_archives (item_id, url, status, requested_at)
            VALUES (?, ?, 'pending', datetime('now'))
            ON CONFLICT(item_id) DO UPDATE SET
                url = excluded.url, status = 'pending',
                requested_at = datetime('now'), error = NULL
            """,
            (item_id, url or ""),
        )
        return
    conn.execute(
        """
        INSERT INTO item_archives (item_id, url, status, archive_url, archived_at, error)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            url = excluded.url, status = excluded.status,
            archive_url = COALESCE(excluded.archive_url, item_archives.archive_url),
            archived_at = COALESCE(excluded.archived_at, item_archives.archived_at),
            error = excluded.error
        """,
        (item_id, url or "", status, archive_url, archived_at,
         (error or "")[:500] or None),
    )


def get_item_archive(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(
        "SELECT item_id, url, archive_url, status, requested_at, archived_at, error "
        "FROM item_archives WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    return dict(row) if row else None


def item_archives_map(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, dict]:
    """``{item_id: archive row}`` for the given items (missing ids omitted)."""
    ids = [int(i) for i in item_ids if i is not None]
    out: dict[int, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            "SELECT item_id, url, archive_url, status, requested_at, archived_at, error "
            f"FROM item_archives WHERE item_id IN ({marks})",
            chunk,
        ).fetchall():
            out[int(r["item_id"])] = dict(r)
    return out


def case_archive_targets(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """Pinned items of a case that have a URL and still need a capture
    (no row yet, or only a failed attempt)."""
    rows = conn.execute(
        """
        SELECT DISTINCT i.id, i.url
        FROM bookmarks b
        JOIN items i ON i.id = b.item_id
        LEFT JOIN item_archives a ON a.item_id = i.id
        WHERE b.case_id = ? AND COALESCE(i.url, '') <> ''
          AND (a.item_id IS NULL OR a.status = 'failed')
        ORDER BY i.id
        """,
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def case_archive_summary(conn: sqlite3.Connection, case_id: int) -> dict:
    """Archive progress over a case's pinned items that have a URL."""
    rows = conn.execute(
        """
        SELECT COALESCE(a.status, 'none') AS status, COUNT(DISTINCT i.id) AS n
        FROM bookmarks b
        JOIN items i ON i.id = b.item_id
        LEFT JOIN item_archives a ON a.item_id = i.id
        WHERE b.case_id = ? AND COALESCE(i.url, '') <> ''
        GROUP BY COALESCE(a.status, 'none')
        """,
        (case_id,),
    ).fetchall()
    keys = ("none", *ARCHIVE_STATUSES)
    counts = dict.fromkeys(keys, 0)
    for r in rows:
        counts[r["status"]] = int(r["n"])
    counts["total"] = sum(counts[k] for k in keys)
    counts["archived"] = counts["done"] + counts["existing"]
    return counts


def fail_stale_archive_jobs(conn: sqlite3.Connection) -> int:
    """Mark ``pending`` rows as failed. Called at startup: the in-memory queue
    does not survive a restart, so anything still pending was interrupted."""
    cur = conn.execute(
        "UPDATE item_archives SET status = 'failed', "
        "error = 'Interrupted because the app was restarted. Retry to capture it.' "
        "WHERE status = 'pending'"
    )
    return cur.rowcount or 0


def case_auto_archive(conn: sqlite3.Connection, case_id: int) -> bool:
    row = conn.execute(
        "SELECT COALESCE(auto_archive, 0) FROM cases WHERE id = ?", (case_id,)
    ).fetchone()
    return bool(row and row[0])


def set_case_auto_archive(conn: sqlite3.Connection, case_id: int, on: bool) -> None:
    conn.execute(
        "UPDATE cases SET auto_archive = ? WHERE id = ?", (1 if on else 0, case_id)
    )
