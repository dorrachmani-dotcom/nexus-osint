"""Shared ingestion for knowledge connectors (Confluence spaces, Obsidian vaults).

A connector yields :class:`ConnectorDoc` objects — one per page or note — and
:func:`sync_document` stores each the same way an upload is stored, so it
lands in the same places and obeys the same rules:

  * one ``documents`` row per page/note (``kind`` = the connector, ``origin`` =
    the source name), placed in the chosen case and/or the knowledge pool, with
    the "no AI" flag honoured by every enrichment queue and Sherlock;
  * the text is split into readable chunks, each a normal item linked to that
    document (and pinned into the case); attachments go through the upload
    parsers (:func:`nexus.ingest.extract.extract`);
  * watchlists run over new text.

Change detection mirrors watched web pages: per document key the last content
hash and text are kept in ``connector_docs``. Unchanged → nothing. Changed →
the old chunks are replaced by the new text and one "<title> — updated" item
with a short diff summary is added (pinned to the case, kept as history). A
document that disappeared from the source can be removed with
:func:`remove_missing`.

Nothing here talks to the network or the filesystem; callers do.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from nexus.ingest.safety import DEFAULT_LIMITS, UnsafeFileError, safe_display_name
from nexus.ingest.upload import split_text
from nexus.models import RawItem

logger = logging.getLogger("nexus.ingest.connector")

CHUNK_CHARS = 6000
STATE_TEXT_MAX = 200_000
_META_MAX = 8000


@dataclass
class ConnectorDoc:
    """One page / note as read from a connector."""

    key: str                                  # stable id within the source
    title: str
    text: str = ""
    url: str | None = None
    author: str | None = None
    modified: datetime | None = None
    version: str | None = None
    meta: dict = field(default_factory=dict)  # labels, space, breadcrumb, tags, links...
    # Optional pre-split sections ``(label, text)`` (e.g. Markdown headings).
    sections: list[tuple[str, str]] | None = None
    # Optional attachments ``(filename, bytes)`` read through the upload parsers.
    attachments: list[tuple[str, bytes]] = field(default_factory=list)


@dataclass
class Target:
    """Where a connector's documents go (like an upload's destination)."""

    case_id: int | None = None
    in_pool: bool = True
    no_ai: bool = False

    @classmethod
    def from_row(cls, row: dict) -> Target:
        case_id = row.get("case_id")
        in_pool = bool(row.get("in_pool", 1))
        if case_id is None:
            in_pool = True  # must land somewhere findable
        return cls(case_id=int(case_id) if case_id is not None else None,
                   in_pool=in_pool, no_ai=bool(row.get("no_ai", 0)))


def content_hash(doc: ConnectorDoc) -> str:
    h = hashlib.sha256()
    h.update(" ".join((doc.title or "").split()).encode("utf-8"))
    h.update(b"\x00")
    h.update(" ".join((doc.text or "").split()).encode("utf-8"))
    for name, data in doc.attachments:
        h.update(b"\x00" + name.encode("utf-8") + b"\x00" + hashlib.sha256(data).digest())
    return h.hexdigest()


def _state(conn: sqlite3.Connection, kind: str, source_id: int, key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM connector_docs WHERE kind = ? AND source_id = ? AND doc_key = ?",
        (kind, source_id, key),
    ).fetchone()
    return dict(row) if row else None


def known_keys(conn: sqlite3.Connection, kind: str, source_id: int) -> dict[str, dict]:
    """``{key: {content_hash, version, modified, document_id}}`` for a source."""
    rows = conn.execute(
        "SELECT doc_key, content_hash, version, modified, document_id FROM connector_docs "
        "WHERE kind = ? AND source_id = ?",
        (kind, source_id),
    ).fetchall()
    return {r["doc_key"]: dict(r) for r in rows}


def _meta_json(meta: dict) -> str:
    try:
        text = json.dumps(meta, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"
    return text if len(text) <= _META_MAX else "{}"


def _small_meta(meta: dict) -> dict:
    """JSON-safe, size-capped metadata copied onto each item."""
    out: dict = {}
    for k, v in meta.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v[:500] if isinstance(v, str) else v
        elif isinstance(v, list):
            out[k] = [x[:300] if isinstance(x, str) else x for x in v[:50]]
    return out


def _sections(doc: ConnectorDoc) -> list[tuple[str, str]]:
    if doc.sections:
        return [(label, text) for label, text in doc.sections if (text or "").strip()]
    return [("", doc.text)] if (doc.text or "").strip() else []


def _drop_items(conn: sqlite3.Connection, doc_id: int, origin: str) -> None:
    """Remove the chunk items this document produced (not shared with another
    document, and only rows this connector created)."""
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
    conn.execute("DELETE FROM document_items WHERE document_id = ?", (doc_id,))


def _store_item(conn: sqlite3.Connection, item: RawItem, target: Target) -> tuple[int, bool]:
    from nexus.storage import add_bookmark, match_watchlists, upsert_item

    item_id, is_new = upsert_item(conn, item)
    if item_id and target.case_id is not None:
        add_bookmark(conn, item_id, target.case_id)
    if item_id and is_new:
        try:
            match_watchlists(conn, item_id, f"{item.title or ''}\n{item.content}")
        except Exception:
            logger.exception("Watchlist matching failed for connector item %s", item_id)
    return item_id, is_new


def sync_document(
    conn: sqlite3.Connection,
    *,
    kind: str,
    source_id: int,
    source_name: str,
    track: str,
    doc: ConnectorDoc,
    target: Target,
) -> dict:
    """Store one page/note. Returns ``{"change": new|changed|same, "new": n}``."""
    from nexus.ingest.extract import extract
    from nexus.sources.webpage import diff_summary
    from nexus.storage.documents import create_document, finish_document, link_document_item

    digest = content_hash(doc)
    prev = _state(conn, kind, source_id, doc.key)
    if prev is not None and prev.get("content_hash") == digest:
        conn.execute(
            "UPDATE connector_docs SET last_seen = datetime('now') "
            "WHERE kind = ? AND source_id = ? AND doc_key = ?",
            (kind, source_id, doc.key),
        )
        return {"change": "same", "new": 0}

    title = (doc.title or doc.key or "untitled").strip()[:300]
    doc_id = prev.get("document_id") if prev else None
    if doc_id is not None and conn.execute(
        "SELECT 1 FROM documents WHERE id = ?", (doc_id,)
    ).fetchone() is None:
        doc_id = None  # the analyst deleted it; a changed page comes back
    size = len((doc.text or "").encode("utf-8")) + sum(len(d) for _n, d in doc.attachments)
    if doc_id is None:
        doc_id = create_document(
            conn, filename=title, sha256=digest, size_bytes=size, kind=kind,
            stored_path="", case_id=target.case_id, in_pool=target.in_pool,
            no_ai=target.no_ai,
        )
        conn.execute("UPDATE documents SET origin = ? WHERE id = ?", (source_name, doc_id))
    else:
        _drop_items(conn, doc_id, source_name)
        conn.execute(
            "UPDATE documents SET filename = ?, sha256 = ?, size_bytes = ?, case_id = ?, "
            "in_pool = ?, no_ai = ? WHERE id = ?",
            (title, digest, size, target.case_id, 1 if target.in_pool else 0,
             1 if target.no_ai else 0, doc_id),
        )

    base_raw = {"connector": kind, "connector_source_id": source_id, "doc_key": doc.key,
                "document_id": doc_id, "version": doc.version, **_small_meta(doc.meta)}
    new = 0
    index = 0
    notes: list[str] = []
    limits = DEFAULT_LIMITS
    parts: list[tuple[str, str]] = list(_sections(doc))
    for name, data in doc.attachments:
        try:
            ex = extract(data, safe_display_name(name), limits)
        except UnsafeFileError as exc:
            notes.append(f"attachment {safe_display_name(name)} refused: {exc}")
            continue
        except Exception as exc:  # parsers are defensive; last net
            notes.append(f"attachment {safe_display_name(name)} unreadable ({type(exc).__name__})")
            continue
        for sec in ex.sections:
            label = f"attachment {safe_display_name(name)}" + (f" — {sec.label}" if sec.label else "")
            parts.append((label, sec.text))

    for label, text in parts:
        pieces = split_text(text, CHUNK_CHARS)
        for n, piece in enumerate(pieces, 1):
            if index >= limits.max_chunks:
                break
            item_title = title + (f" — {label}" if label else "")
            if len(pieces) > 1:
                item_title += f" ({n}/{len(pieces)})"
            item = RawItem(
                source=source_name, track=track,
                external_id=f"{doc.key}#{digest[:16]}:{index}",
                url=doc.url, author=doc.author, title=item_title[:300], content=piece,
                published_at=doc.modified,
                raw={**base_raw, "chunk": index, "section": label,
                     "change": "changed" if prev else "new"},
            )
            item_id, is_new = _store_item(conn, item, target)
            if not item_id:
                continue
            link_document_item(conn, doc_id, item_id, index)
            new += int(is_new)
            index += 1

    if prev is not None and prev.get("content_hash"):
        summary = diff_summary(prev.get("text") or "", doc.text or "")
        item = RawItem(
            source=source_name, track=track,
            external_id=f"{doc.key}#{digest[:16]}:changed",
            url=doc.url, author=doc.author, title=f"{title} — updated"[:300],
            content=summary[:CHUNK_CHARS], published_at=doc.modified,
            raw={**base_raw, "change": "changed",
                 "previous_hash": prev.get("content_hash"),
                 "previous_version": prev.get("version")},
        )
        _id, is_new = _store_item(conn, item, target)
        new += int(is_new)

    if index == 0 and not notes:
        notes.append("no readable text")
    finish_document(conn, doc_id, index, notes)
    conn.execute(
        """
        INSERT INTO connector_docs (kind, source_id, doc_key, document_id, content_hash,
                                    version, modified, title, text, meta)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, source_id, doc_key) DO UPDATE SET
            document_id = excluded.document_id, content_hash = excluded.content_hash,
            version = excluded.version, modified = excluded.modified,
            title = excluded.title, text = excluded.text, meta = excluded.meta,
            last_changed = datetime('now'), last_seen = datetime('now')
        """,
        (kind, source_id, doc.key, doc_id, digest, doc.version,
         doc.modified.isoformat() if doc.modified else None, title,
         (doc.text or "")[:STATE_TEXT_MAX], _meta_json(doc.meta)),
    )
    return {"change": "changed" if prev else "new", "new": new, "document_id": doc_id}


def remove_document(conn: sqlite3.Connection, kind: str, source_id: int, key: str,
                    origin: str) -> None:
    """Forget one page/note: its document, its chunk items and its state."""
    st = _state(conn, kind, source_id, key)
    if st is None:
        return
    doc_id = st.get("document_id")
    if doc_id is not None:
        _drop_items(conn, int(doc_id), origin)
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.execute(
        "DELETE FROM connector_docs WHERE kind = ? AND source_id = ? AND doc_key = ?",
        (kind, source_id, key),
    )


def remove_missing(conn: sqlite3.Connection, kind: str, source_id: int,
                   present: set[str], origin: str) -> int:
    """Remove every stored page/note whose key is not in ``present``."""
    gone = [k for k in known_keys(conn, kind, source_id) if k not in present]
    for key in gone:
        remove_document(conn, kind, source_id, key, origin)
    return len(gone)


def forget_source(conn: sqlite3.Connection, kind: str, source_id: int,
                  origin: str, keep_documents: bool = True) -> None:
    """Drop a deleted source's state. Its documents stay unless asked otherwise."""
    if not keep_documents:
        for key in list(known_keys(conn, kind, source_id)):
            remove_document(conn, kind, source_id, key, origin)
    conn.execute("DELETE FROM connector_docs WHERE kind = ? AND source_id = ?",
                 (kind, source_id))
