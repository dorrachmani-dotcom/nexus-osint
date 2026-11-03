"""Edit -> publish: the analyst layer over collected items, write-ups composed
from pinned items, and the record of every publish.

The collected item (``items`` + ``analyses``) is never modified here: an edit
is stored beside it in ``item_edits`` and shown on top of it, with the
original still visible.
"""

from __future__ import annotations

import json
import sqlite3

from nexus.db import last_row_id

MAX_TITLE = 300
MAX_SUMMARY = 20_000
MAX_NOTE = 20_000
MAX_BODY = 100_000
MAX_WRITEUP_ITEMS = 50
SUBJECT_KINDS = {"item", "writeup"}
TARGETS = {"confluence", "obsidian", "pool"}


def _clean(text, limit: int) -> str | None:
    value = str(text or "").replace("\r\n", "\n").strip()
    return value[:limit] or None


# ------------------------------------------------------------- item edits
def get_item_edit(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM item_edits WHERE item_id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def save_item_edit(conn: sqlite3.Connection, item_id: int, *, title=None, summary=None,
                   note=None) -> dict | None:
    """Store (or clear, when every field is empty) the analyst's edit of an item."""
    t, s, n = _clean(title, MAX_TITLE), _clean(summary, MAX_SUMMARY), _clean(note, MAX_NOTE)
    if not (t or s or n):
        conn.execute("DELETE FROM item_edits WHERE item_id = ?", (item_id,))
        return None
    conn.execute(
        """
        INSERT INTO item_edits (item_id, title, summary, note, edited_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(item_id) DO UPDATE SET title = excluded.title,
            summary = excluded.summary, note = excluded.note, edited_at = datetime('now')
        """,
        (item_id, t, s, n),
    )
    return get_item_edit(conn, item_id)


def edited_item_ids(conn: sqlite3.Connection, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT item_id FROM item_edits WHERE item_id IN ({marks})",
                        [int(i) for i in ids]).fetchall()
    return {int(r["item_id"]) for r in rows}


def item_for_publish(conn: sqlite3.Connection, item_id: int) -> dict | None:
    """The item with its analysis, the analyst edit and the cases it is pinned to."""
    row = conn.execute(
        """
        SELECT i.id, i.source, i.url, i.author, i.title, i.content, i.published_at,
               i.fetched_at, a.summary, a.entities,
               e.title AS edit_title, e.summary AS edit_summary, e.note AS edit_note,
               e.edited_at
        FROM items i
        LEFT JOIN analyses a ON a.item_id = i.id
        LEFT JOIN item_edits e ON e.item_id = i.id
        WHERE i.id = ?
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["cases"] = [r["name"] for r in conn.execute(
        """SELECT DISTINCT c.name FROM bookmarks b JOIN cases c ON c.id = b.case_id
           WHERE b.item_id = ? ORDER BY c.name""", (item_id,)).fetchall()]
    return item


# ---------------------------------------------------------------- write-ups
def _ids(raw) -> list[int]:
    out: list[int] = []
    for v in raw or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out[:MAX_WRITEUP_ITEMS]


def create_writeup(conn: sqlite3.Connection, *, case_id: int | None, title: str,
                   body: str, item_ids) -> int:
    cur = conn.execute(
        "INSERT INTO writeups (case_id, title, body, item_ids) VALUES (?, ?, ?, ?)",
        (case_id, _clean(title, MAX_TITLE) or "Write-up", _clean(body, MAX_BODY) or "",
         json.dumps(_ids(item_ids))),
    )
    return last_row_id(cur)


def get_writeup(conn: sqlite3.Connection, writeup_id: int) -> dict | None:
    row = conn.execute(
        """SELECT w.*, c.name AS case_name FROM writeups w
           LEFT JOIN cases c ON c.id = w.case_id WHERE w.id = ?""", (writeup_id,),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["item_ids"] = _ids(json.loads(out.get("item_ids") or "[]"))
    except ValueError:
        out["item_ids"] = []
    return out


def update_writeup(conn: sqlite3.Connection, writeup_id: int, *, title: str, body: str) -> None:
    conn.execute(
        "UPDATE writeups SET title = ?, body = ?, edited_at = datetime('now') WHERE id = ?",
        (_clean(title, MAX_TITLE) or "Write-up", _clean(body, MAX_BODY) or "", writeup_id),
    )


def delete_writeup(conn: sqlite3.Connection, writeup_id: int) -> None:
    """Delete the write-up itself. Its publish records stay: whatever was not
    cleaned up (see :mod:`nexus.publish.retract`) keeps a record of where it
    still is. Write-up ids are never reused (AUTOINCREMENT)."""
    conn.execute("DELETE FROM writeups WHERE id = ?", (writeup_id,))


def list_writeups(conn: sqlite3.Connection, case_id: int | None = None) -> list[dict]:
    if case_id is None:
        rows = conn.execute("SELECT id, case_id, title, edited_at FROM writeups "
                            "ORDER BY edited_at DESC, id DESC").fetchall()
    else:
        rows = conn.execute("SELECT id, case_id, title, edited_at FROM writeups "
                            "WHERE case_id = ? ORDER BY edited_at DESC, id DESC",
                            (case_id,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------- publications
def record_publication(conn: sqlite3.Connection, *, subject_kind: str, subject_id: int,
                       target: str, target_id: int, mode: str, remote_id: str,
                       location: str | None, url: str | None, title: str | None) -> int:
    if subject_kind not in SUBJECT_KINDS or target not in TARGETS:
        raise ValueError("unknown publish subject or target")
    conn.execute(
        """
        INSERT INTO publications (subject_kind, subject_id, target, target_id, mode,
                                  remote_id, location, url, title)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(subject_kind, subject_id, target, target_id, remote_id) DO UPDATE SET
            mode = excluded.mode, location = excluded.location, url = excluded.url,
            title = excluded.title, published_at = datetime('now')
        """,
        (subject_kind, int(subject_id), target, int(target_id), mode[:20], str(remote_id)[:500],
         (location or "")[:1000] or None, (url or "")[:2000] or None, (title or "")[:300] or None),
    )
    row = conn.execute(
        "SELECT id FROM publications WHERE subject_kind = ? AND subject_id = ? AND target = ? "
        "AND target_id = ? AND remote_id = ?",
        (subject_kind, int(subject_id), target, int(target_id), str(remote_id)[:500]),
    ).fetchone()
    return int(row["id"])


def list_publications(conn: sqlite3.Connection, subject_kind: str,
                      subject_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM publications WHERE subject_kind = ? AND subject_id = ? "
        "ORDER BY published_at DESC, id DESC",
        (subject_kind, int(subject_id)),
    ).fetchall()
    return [dict(r) for r in rows]


def find_publication(conn: sqlite3.Connection, subject_kind: str, subject_id: int,
                     target: str, target_id: int, *, mode: str | None = None,
                     remote_id: str | None = None) -> dict | None:
    sql = ("SELECT * FROM publications WHERE subject_kind = ? AND subject_id = ? "
           "AND target = ? AND target_id = ?")
    params: list = [subject_kind, int(subject_id), target, int(target_id)]
    if mode is not None:
        sql += " AND mode = ?"
        params.append(mode)
    if remote_id is not None:
        sql += " AND remote_id = ?"
        params.append(str(remote_id))
    row = conn.execute(sql + " ORDER BY published_at DESC, id DESC LIMIT 1", params).fetchone()
    return dict(row) if row else None


def forget_publication(conn: sqlite3.Connection, publication_id: int) -> None:
    conn.execute("DELETE FROM publications WHERE id = ?", (publication_id,))


# ---------------------------------------------------- opt-in switches
def set_confluence_publish(conn: sqlite3.Connection, source_id: int, allow: bool) -> None:
    conn.execute("UPDATE confluence_sources SET allow_publish = ? WHERE id = ?",
                 (1 if allow else 0, source_id))


def set_vault_write(conn: sqlite3.Connection, vault_id: int, allow: bool, folder: str) -> None:
    conn.execute("UPDATE obsidian_vaults SET allow_write = ?, write_folder = ? WHERE id = ?",
                 (1 if allow else 0, folder, vault_id))
