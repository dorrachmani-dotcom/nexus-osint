"""Ingest one uploaded file: keep the original, extract, chunk, store, attach.

The flow for each file:

  1. size check and sha256 of the bytes,
  2. :func:`nexus.ingest.extract.extract` → sections (pages, sheets, slides…),
     refusing anything the safety layer rejects (ZIP bombs, oversize files),
  3. the original is written to ``DATA_DIR/uploads/<sha256>.<ext>`` — local
     only, named by hash so a crafted filename can never choose the path,
  4. each section is split into readable chunks and stored as a normal item
     (source ``upload``) with ``external_id = <sha256>:<chunk>`` — so uploading
     the same file again never duplicates anything,
  5. the items are linked to a ``documents`` row and either pinned into the
     chosen case or placed in the general knowledge pool (or both),
  6. watchlists run over the new text, exactly as for collected items.

Never raises for bad content; returns a result dict the UI renders.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from nexus.config import Settings, get_settings
from nexus.ingest.extract import FORMAT_LABELS, Section, extract
from nexus.ingest.safety import DEFAULT_LIMITS, Limits, UnsafeFileError, safe_display_name
from nexus.models import RawItem
from nexus.security.atrest import write_protected

logger = logging.getLogger("nexus.ingest.upload")

UPLOAD_SOURCE = "upload"
_LOCAL_PROVIDERS = {"off", "ollama", "local_openai"}


def cloud_ai_active(settings: Settings | None = None) -> bool:
    """True when the active AI provider sends text to a cloud service."""
    settings = settings or get_settings()
    return settings.active_provider() not in _LOCAL_PROVIDERS


def uploads_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.data_path / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _store_original(data: bytes, sha: str, settings: Settings) -> str:
    """Write the original once (content-addressed); return its relative path.

    Always ``<sha256>.bin``: the real filename/extension live in the database,
    so no stored original is ever served with an executable content type.
    """
    name = f"{sha}.bin"
    folder = uploads_dir(settings)
    target = folder / name
    if not target.exists():
        # Atomic (unique temp name + replace; concurrent uploads of the same
        # file are identical, so last rename wins) and encrypted at rest when
        # the analyst enabled it — the temp file already holds ciphertext.
        write_protected(target, data, content_addressed=True)
    return f"uploads/{name}"


def remove_original(rel_path: str | None, settings: Settings | None = None) -> None:
    """Delete a stored original (only ever inside the uploads directory)."""
    if not rel_path:
        return
    base = uploads_dir(settings).resolve()
    target = (base.parent / rel_path).resolve()
    if target.parent != base:
        logger.warning("Refusing to delete a path outside the uploads directory")
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete stored upload %s", target.name, exc_info=True)


def split_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks of at most ``max_chars``, at paragraph, line or
    sentence boundaries where possible."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = -1
        for sep in ("\n\n", "\n", ". ", " "):
            cut = window.rfind(sep)
            if cut >= max_chars // 2:
                cut += len(sep)
                break
        if cut < max_chars // 2:
            cut = max_chars
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest.strip():
        chunks.append(rest.strip())
    return [c for c in chunks if c]


def _chunks(sections: list[Section], limits: Limits):
    """Yield ``(index, label, part, parts, text, meta)`` for every stored chunk."""
    index = 0
    for section in sections:
        pieces = split_text(section.text, limits.chunk_chars)
        for n, piece in enumerate(pieces, 1):
            if index >= limits.max_chunks:
                return
            yield index, section.label, n, len(pieces), piece, section.meta
            index += 1


def _meta_for_raw(meta: dict) -> dict:
    """Keep only small JSON-safe metadata on each item."""
    out: dict = {}
    for key, value in meta.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value[:500] if isinstance(value, str) else value
        elif isinstance(value, list):
            out[key] = value[:50]
    return out


def ingest_upload(
    data: bytes,
    filename: str,
    *,
    case_id: int | None = None,
    in_pool: bool = False,
    no_ai: bool = False,
    settings: Settings | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> dict:
    """Ingest one file. Returns ``{ok, filename, kind, chunks, new, notes, ...}``."""
    settings = settings or get_settings()
    name = safe_display_name(filename)
    result: dict = {"ok": False, "filename": name, "kind": "unknown", "chunks": 0,
                    "new": 0, "notes": [], "error": ""}
    if not data:
        result["error"] = "the file is empty"
        return result
    if len(data) > limits.max_file_bytes:
        result["error"] = (f"the file is larger than the "
                           f"{limits.max_file_bytes // (1024 * 1024)} MB limit")
        return result
    if case_id is None and not in_pool:
        in_pool = True  # every upload must land somewhere findable

    sha = hashlib.sha256(data).hexdigest()
    try:
        extracted = extract(data, name, limits)
    except UnsafeFileError as exc:
        result["error"] = f"refused for safety: {exc}"
        return result
    except Exception as exc:  # extract() is defensive; this is the last net
        logger.exception("Extraction failed for %s", name)
        result["error"] = f"could not read the file ({type(exc).__name__})"
        return result

    result["kind"] = extracted.kind
    result["kind_label"] = FORMAT_LABELS.get(extracted.kind, "Other / unknown format")
    notes = list(extracted.notes)
    existed = (uploads_dir(settings) / f"{sha}.bin").exists()
    try:
        stored = _store_original(data, sha, settings)
        doc_id, chunks, new = _persist(
            extracted, data, name, sha, stored, case_id=case_id, in_pool=in_pool,
            no_ai=no_ai, limits=limits, notes=notes,
        )
    except Exception as exc:  # disk full, database locked, ...: report, don't 500
        logger.exception("Storing upload %s failed", name)
        if not existed:
            remove_original(f"uploads/{sha}.bin", settings)
        result["error"] = f"could not store the file ({type(exc).__name__}); please retry"
        return result

    result.update({"ok": True, "document_id": doc_id, "chunks": chunks, "new": new,
                   "notes": notes, "sha256": sha})
    logger.info("Ingested upload %s: %s, %d chunk(s), %d new", name, extracted.kind,
                chunks, new)
    return result


def _persist(extracted, data: bytes, name: str, sha: str, stored: str, *,
             case_id: int | None, in_pool: bool, no_ai: bool, limits: Limits,
             notes: list[str]) -> tuple[int, int, int]:
    """Write the document row, its items and links in ONE transaction, so an
    item can never exist without the link carrying its "no AI" flag."""
    from nexus.db import get_connection
    from nexus.storage import add_bookmark, match_watchlists, upsert_item
    from nexus.storage.documents import (
        create_document,
        finish_document,
        link_document_item,
    )

    new = 0
    chunks = 0
    with get_connection() as conn:
        doc_id = create_document(
            conn, filename=name, sha256=sha, size_bytes=len(data), kind=extracted.kind,
            stored_path=stored, case_id=case_id, in_pool=in_pool, no_ai=no_ai,
        )
        for index, label, part, parts, text, meta in _chunks(extracted.sections, limits):
            title = f"{name} — {label}" if label else name
            if parts > 1:
                title += f" ({part}/{parts})"
            item = RawItem(
                source=UPLOAD_SOURCE,
                track="upload",
                external_id=f"{sha}:{index}",
                title=title[:300],
                content=text,
                raw={
                    "document_id": doc_id,
                    "filename": name,
                    "sha256": sha,
                    "chunk": index,
                    "section": label,
                    "kind": extracted.kind,
                    **_meta_for_raw(meta),
                },
            )
            item_id, is_new = upsert_item(conn, item)
            if not item_id:
                continue
            chunks += 1
            link_document_item(conn, doc_id, item_id, index)
            if case_id is not None:
                add_bookmark(conn, item_id, case_id)
            if is_new:
                new += 1
                try:
                    match_watchlists(conn, item_id, f"{item.title}\n{item.content}")
                except Exception:
                    logger.exception("Watchlist matching failed for uploaded item %s", item_id)
        if chunks >= limits.max_chunks:
            notes.append(f"stopped after {limits.max_chunks} chunks")
        if chunks == 0 and not notes:
            notes.append("no readable text was found; the original file is stored")
        finish_document(conn, doc_id, chunks, notes)
    return doc_id, chunks, new
