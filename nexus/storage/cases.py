"""Cases: CRUD, the active case, case terms, visit tracking and notes."""

from __future__ import annotations

import sqlite3
from datetime import UTC

from nexus.db import last_row_id
from nexus.storage.meta import get_meta, set_meta

# --- Analyst workspace: cases, bookmarks, notes -----------------------------
# These power the investigator surface: pin items, attach notes, and group work
# into named cases. All writes are idempotent where a natural key exists.


# Allowed lifecycle states and priorities (constrains user input to a safe set).
_CASE_STATUSES = {"open", "closed"}


_CASE_PRIORITIES = {"high", "medium", "low"}


# Sort weight so high-priority open cases float to the top of the board.
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


# Sentinel so callers can distinguish "don't filter by case" from "case is NULL".
_NO_CASE_FILTER = object()


def create_case(
    conn: sqlite3.Connection,
    name: str,
    description: str | None = None,
    priority: str = "medium",
    parent_id: int | None = None,
) -> int:
    """Create a case. ``parent_id`` makes it a sub-case of another case.

    Sub-cases are one level deep: if ``parent_id`` itself points at a sub-case,
    we re-parent to that sub-case's top-level case so the tree never nests.
    """
    priority = priority if priority in _CASE_PRIORITIES else "medium"
    if parent_id is not None:
        row = conn.execute(
            "SELECT parent_id FROM cases WHERE id = ?", (parent_id,)
        ).fetchone()
        if row is not None and row["parent_id"] is not None:
            parent_id = int(row["parent_id"])  # flatten to one level
    cur = conn.execute(
        "INSERT INTO cases (name, description, priority, parent_id) VALUES (?, ?, ?, ?)",
        (name, description, priority, parent_id),
    )
    return last_row_id(cur)


def update_case(
    conn: sqlite3.Connection,
    case_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: str | None = None,
) -> None:
    """Patch any subset of a case's editable fields. Unknown enum values ignored."""
    sets: list[str] = []
    params: list = []
    if name is not None:
        name = name.strip()[:120]
        if name:
            sets.append("name = ?")
            params.append(name)
    if description is not None:
        sets.append("description = ?")
        params.append(description.strip() or None)
    if status is not None and status in _CASE_STATUSES:
        sets.append("status = ?")
        params.append(status)
    if priority is not None and priority in _CASE_PRIORITIES:
        sets.append("priority = ?")
        params.append(priority)
    if not sets:
        return
    params.append(case_id)
    conn.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id = ?", params)


def delete_case(conn: sqlite3.Connection, case_id: int) -> None:
    """Delete a case. Its bookmarks/notes cascade; the items themselves stay."""
    conn.execute("DELETE FROM bookmarks WHERE case_id = ?", (case_id,))
    conn.execute("DELETE FROM notes WHERE case_id = ?", (case_id,))
    conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
    # If it was the active case, clear that pointer so the UI doesn't dangle.
    if get_meta(conn, "active_case_id") == str(case_id):
        set_meta(conn, "active_case_id", None)


def list_cases(
    conn: sqlite3.Connection,
    status: str | None = None,
    parent_id=_NO_CASE_FILTER,
) -> list[dict]:
    """Cases with item/note/sub-case counts, ordered open-first then by priority.

    ``status`` filters lifecycle ('open'/'closed'). ``parent_id`` filtering: omit
    for all cases; pass ``None`` for only top-level cases (the hub overview); pass
    an int for one case's sub-cases.
    """
    clauses: list[str] = []
    params: list = []
    if status in _CASE_STATUSES:
        clauses.append("c.status = ?")
        params.append(status)
    if parent_id is not _NO_CASE_FILTER:
        if parent_id is None:
            clauses.append("c.parent_id IS NULL")
        else:
            clauses.append("c.parent_id = ?")
            params.append(int(parent_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT c.id, c.name, c.description, c.created_at, c.parent_id,
               COALESCE(c.status, 'open') AS status,
               COALESCE(c.priority, 'medium') AS priority,
               (SELECT COUNT(*) FROM bookmarks b WHERE b.case_id = c.id) AS item_count,
               (SELECT COUNT(*) FROM notes n WHERE n.case_id = c.id) AS note_count,
               (SELECT COUNT(*) FROM cases s WHERE s.parent_id = c.id) AS subcase_count
        FROM cases c
        {where}
        ORDER BY c.id DESC
        """,
        params,
    ).fetchall()
    out = [dict(r) for r in rows]
    # Final ordering in Python so we can rank by priority cleanly:
    # open before closed, then high>medium>low, then newest id.
    out.sort(
        key=lambda c: (
            0 if c["status"] == "open" else 1,
            _PRIORITY_RANK.get(c["priority"], 1),
            -int(c["id"]),
        )
    )
    return out


def get_case(conn: sqlite3.Connection, case_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, name, description, created_at, parent_id,
               COALESCE(status, 'open') AS status,
               COALESCE(priority, 'medium') AS priority,
               COALESCE(auto_archive, 0) AS auto_archive
        FROM cases WHERE id = ?
        """,
        (case_id,),
    ).fetchone()
    return dict(row) if row else None


def set_active_case(conn: sqlite3.Connection, case_id: int | None) -> None:
    """Remember which case the analyst is currently working in (or None)."""
    set_meta(conn, "active_case_id", str(case_id) if case_id else None)


def get_active_case(conn: sqlite3.Connection) -> dict | None:
    """The currently active case as a full row, or None if unset/missing."""
    raw = get_meta(conn, "active_case_id")
    if not raw:
        return None
    try:
        case = get_case(conn, int(raw))
    except (TypeError, ValueError):
        return None
    return case


# --- A case's tracking words (its "word capsule") + derived live feed --------
def case_terms(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """The tracking terms for a case (each as {id, term, kind}), oldest first."""
    rows = conn.execute(
        "SELECT id, term, kind FROM case_terms WHERE case_id = ? ORDER BY id",
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def all_case_terms(conn: sqlite3.Connection) -> list[str]:
    """Every distinct tracking word across all cases (for the home 'topics' scope)."""
    rows = conn.execute("SELECT DISTINCT term FROM case_terms ORDER BY term").fetchall()
    return [r["term"] for r in rows]


def add_case_term(
    conn: sqlite3.Connection, case_id: int, term: str, kind: str = "required"
) -> None:
    """Add one tracking word/phrase to a case (idempotent per case)."""
    term = (term or "").strip()
    if term:
        conn.execute(
            "INSERT OR IGNORE INTO case_terms (case_id, term, kind) VALUES (?, ?, ?)",
            (case_id, term, kind),
        )


def remove_case_term(conn: sqlite3.Connection, case_id: int, term_id: int) -> None:
    conn.execute(
        "DELETE FROM case_terms WHERE id = ? AND case_id = ?", (term_id, case_id)
    )


def update_case_term(
    conn: sqlite3.Connection, case_id: int, term_id: int, new_term: str
) -> None:
    """Edit an existing tracking word in place. Blank is ignored; a clash with
    another existing term for the case removes this row instead of duplicating."""
    new_term = (new_term or "").strip()
    if not new_term:
        return
    clash = conn.execute(
        "SELECT id FROM case_terms WHERE case_id = ? AND term = ? AND id <> ?",
        (case_id, new_term, term_id),
    ).fetchone()
    if clash is not None:
        conn.execute(
            "DELETE FROM case_terms WHERE id = ? AND case_id = ?", (term_id, case_id)
        )
        return
    conn.execute(
        "UPDATE case_terms SET term = ? WHERE id = ? AND case_id = ?",
        (new_term, term_id, case_id),
    )


# --- "New since last visit" — the daily-routine engine ---------------------
def get_case_last_visit(conn: sqlite3.Connection, case_id: int) -> str | None:
    """ISO timestamp of when the analyst last opened this case (or None)."""
    return get_meta(conn, f"case_visit:{case_id}")


def touch_case_visit(conn: sqlite3.Connection, case_id: int) -> None:
    """Record 'just visited now' so the case's new-count resets."""
    from datetime import datetime

    set_meta(conn, f"case_visit:{case_id}", datetime.now(UTC).isoformat())


def case_terms_map(
    conn: sqlite3.Connection, case_ids: list[int], kind: str | None = None
) -> dict[int, list[str]]:
    """All tracking terms for many cases in ONE query → {case_id: [term, ...]}.

    Lets the Cases board avoid a per-case term query (N+1). Missing cases map to [].
    Pass ``kind='required'`` to restrict to search terms (excludes alert words).
    """
    out: dict[int, list[str]] = {int(i): [] for i in case_ids}
    if not case_ids:
        return out
    placeholders = ",".join("?" * len(case_ids))
    params: list = list(case_ids)
    sql = f"SELECT case_id, term FROM case_terms WHERE case_id IN ({placeholders})"
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    for r in rows:
        out.setdefault(int(r["case_id"]), []).append(r["term"])
    return out


def add_note(
    conn: sqlite3.Connection,
    body: str,
    item_id: int | None = None,
    case_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO notes (item_id, case_id, body) VALUES (?, ?, ?)",
        (item_id, case_id, body),
    )
    return last_row_id(cur)


def case_notes(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, item_id, body, created_at
        FROM notes WHERE case_id = ?
        ORDER BY id DESC
        """,
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_note(conn: sqlite3.Connection, note_id: int, body: str) -> int | None:
    """Edit a note's body in place; returns the note's case_id (for re-render)."""
    body = (body or "").strip()
    if not body:
        return None
    conn.execute("UPDATE notes SET body = ? WHERE id = ?", (body, note_id))
    row = conn.execute("SELECT case_id FROM notes WHERE id = ?", (note_id,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def delete_note(conn: sqlite3.Connection, note_id: int) -> int | None:
    """Delete a note; returns the case_id it belonged to (for re-render)."""
    row = conn.execute("SELECT case_id FROM notes WHERE id = ?", (note_id,)).fetchone()
    case_id = int(row[0]) if row and row[0] is not None else None
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    return case_id
