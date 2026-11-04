"""Uploaded documents: the file record and its links to stored items.

An uploaded file becomes one ``documents`` row (filename, sha256, where the
original is kept, which case it belongs to) and many ``items`` (one per page /
section / sheet / slide chunk). ``document_items`` links the two, so a document
can be listed, searched as a unit, and removed cleanly.

A document either belongs to a case (its items are pinned into that case) or to
the general **knowledge pool** (``in_pool = 1``), the shared reference library
Sherlock answers from. ``no_ai = 1`` marks an upload the analyst chose to keep
away from AI: its items are skipped by automatic enrichment and, when the
active provider is a cloud service, by Sherlock's context.
"""

from __future__ import annotations

import json
import sqlite3

from nexus.db import last_row_id
from nexus.storage.items import build_fts_or_query

# SQL predicate (alias ``i`` = items) that is true when an item may be sent to
# AI: it is not part of any upload marked "no AI". Used by the enrichment
# work queues so a no-AI upload is never enriched automatically.
AI_ALLOWED_SQL = (
    "NOT EXISTS (SELECT 1 FROM document_items di "
    "JOIN documents d ON d.id = di.document_id "
    "WHERE di.item_id = i.id AND d.no_ai = 1)"
)


def create_document(
    conn: sqlite3.Connection,
    *,
    filename: str,
    sha256: str,
    size_bytes: int,
    kind: str,
    stored_path: str,
    case_id: int | None,
    in_pool: bool,
    no_ai: bool,
    notes: list[str] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO documents (filename, sha256, size_bytes, kind, stored_path,
                               case_id, in_pool, no_ai, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (filename, sha256, int(size_bytes), kind, stored_path, case_id,
         1 if in_pool else 0, 1 if no_ai else 0, json.dumps(notes or [])),
    )
    return last_row_id(cur)


def finish_document(conn: sqlite3.Connection, doc_id: int, chunks: int,
                    notes: list[str]) -> None:
    conn.execute(
        "UPDATE documents SET chunks = ?, notes = ? WHERE id = ?",
        (int(chunks), json.dumps(notes[:50]), doc_id),
    )


def link_document_item(conn: sqlite3.Connection, doc_id: int, item_id: int,
                       chunk_index: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO document_items (document_id, item_id, chunk_index) "
        "VALUES (?, ?, ?)",
        (doc_id, item_id, chunk_index),
    )


def _decode(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["notes"] = json.loads(d.get("notes") or "[]")
    except (TypeError, ValueError):
        d["notes"] = []
    return d


def get_document(conn: sqlite3.Connection, doc_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT d.*, c.name AS case_name FROM documents d
        LEFT JOIN cases c ON c.id = d.case_id WHERE d.id = ?
        """,
        (doc_id,),
    ).fetchone()
    return _decode(row) if row else None


def list_documents(
    conn: sqlite3.Connection,
    case_id: int | None = None,
    pool_only: bool = False,
    limit: int = 500,
) -> list[dict]:
    """Uploaded documents, newest first; optionally one case's or the pool's."""
    where: list[str] = []
    params: list = []
    if case_id is not None:
        where.append("d.case_id = ?")
        params.append(case_id)
    if pool_only:
        where.append("d.in_pool = 1")
    sql = (
        "SELECT d.*, c.name AS case_name FROM documents d "
        "LEFT JOIN cases c ON c.id = d.case_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.id DESC LIMIT ?"
    params.append(limit)
    return [_decode(r) for r in conn.execute(sql, params).fetchall()]


def document_items(conn: sqlite3.Connection, doc_id: int, limit: int = 2000) -> list[dict]:
    rows = conn.execute(
        """
        SELECT i.id, i.title, substr(i.content, 1, 400) AS excerpt, di.chunk_index
        FROM document_items di JOIN items i ON i.id = di.item_id
        WHERE di.document_id = ? ORDER BY di.chunk_index LIMIT ?
        """,
        (doc_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_document(conn: sqlite3.Connection, doc_id: int) -> str | None:
    """Delete a document and the items only it produced.

    Items that another upload also produced (identical chunk text) are kept,
    and so is any collected (non-upload) item a chunk was folded into as an
    echo — only rows this upload created are removed.
    Returns the stored file's relative path when no other document still uses
    it (so the caller can remove the original), else ``None``.
    """
    doc = get_document(conn, doc_id)
    if doc is None:
        return None
    # Uploads created items with source 'upload'; a connector (Confluence,
    # Obsidian) with its own source name, recorded as the document's origin.
    origin = doc.get("origin") or "upload"
    conn.execute(
        """
        DELETE FROM items WHERE source = ? AND id IN (
            SELECT di.item_id FROM document_items di
            WHERE di.document_id = ?
              AND NOT EXISTS (SELECT 1 FROM document_items o
                              WHERE o.item_id = di.item_id AND o.document_id != ?)
        )
        """,
        (origin, doc_id, doc_id),
    )
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    if not doc["stored_path"]:
        return None  # connector documents keep no original file
    still_used = conn.execute(
        "SELECT 1 FROM documents WHERE stored_path = ? LIMIT 1", (doc["stored_path"],)
    ).fetchone()
    return None if still_used else doc["stored_path"]


def ai_excluded_ids(conn: sqlite3.Connection, item_ids: list[int]) -> set[int]:
    """The subset of ``item_ids`` that belong to a "no AI" upload."""
    ids = [int(i) for i in item_ids if i]
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT di.item_id FROM document_items di
        JOIN documents d ON d.id = di.document_id
        WHERE d.no_ai = 1 AND di.item_id IN ({marks})
        """,
        ids,
    ).fetchall()
    return {int(r["item_id"]) for r in rows}


def drop_ai_excluded(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """``rows`` (dicts with an ``id``) minus items from "no AI" uploads.

    The single filter every prompt builder applies before item text can reach
    a cloud model (and the e-mail brief applies always)."""
    if not rows:
        return rows
    hidden = ai_excluded_ids(conn, [r.get("id") or 0 for r in rows])
    if not hidden:
        return rows
    return [r for r in rows if r.get("id") not in hidden]


def chunk_scope(case_id: int | None) -> tuple[str, list]:
    """SQL condition (on ``d``) for what Sherlock may read: pool plus one case."""
    if case_id is None:
        return "d.in_pool = 1", []
    return "(d.in_pool = 1 OR d.case_id = ?)", [case_id]


_CHUNK_COLUMNS = "i.id, i.title, i.content, i.url, d.filename, d.id AS document_id, d.case_id, d.no_ai"


def chunks_by_id(conn: sqlite3.Connection, ids: list[int],
                 case_id: int | None = None) -> list[dict]:
    """The chunks ``ids``, in the same shape as :func:`search_document_chunks`."""
    if not ids:
        return []
    scope, params = chunk_scope(case_id)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT {_CHUNK_COLUMNS}
        FROM items i
        JOIN document_items di ON di.item_id = i.id
        JOIN documents d ON d.id = di.document_id
        WHERE i.id IN ({marks}) AND {scope}
        GROUP BY i.id
        """,
        [*ids, *params],
    ).fetchall()
    return [dict(r) for r in rows]


def search_document_chunks(
    conn: sqlite3.Connection,
    terms: list[str] | None,
    case_id: int | None = None,
    limit: int = 6,
) -> list[dict]:
    """Best-matching uploaded-document chunks from the knowledge pool (and, when
    given, one case's documents), for grounding Sherlock's answers.

    Without ``terms`` returns nothing (a generic question should not dump the
    library into the prompt).
    """
    match = build_fts_or_query(terms or [])
    if not match:
        return []
    scope, scoped = chunk_scope(case_id)
    params: list = [match, *scoped, limit]
    rows = conn.execute(
        f"""
        SELECT {_CHUNK_COLUMNS}
        FROM (
            SELECT rowid, bm25(items_fts) AS rank FROM items_fts
            WHERE items_fts MATCH ? ORDER BY rank LIMIT 500
        ) f
        JOIN items i ON i.id = f.rowid
        JOIN document_items di ON di.item_id = i.id
        JOIN documents d ON d.id = di.document_id
        WHERE {scope}
        GROUP BY i.id
        ORDER BY MIN(f.rank)
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def document_counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(in_pool), 0) AS pool, "
        "COALESCE(SUM(no_ai), 0) AS no_ai FROM documents"
    ).fetchone()
    return {"documents": int(row["n"]), "pool": int(row["pool"]),
            "no_ai": int(row["no_ai"])}
