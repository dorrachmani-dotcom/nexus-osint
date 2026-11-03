"""Prepare -> preview -> confirm: the only way anything is published.

:func:`prepare` resolves the subject (item or write-up), checks the target's
opt-in switch, renders EXACTLY what will be sent or written and parks it under
a random one-time token (in memory, 15 minutes). Nothing leaves the app here.

:func:`confirm` is called only from an explicit click (the "Confirm & publish"
button in the drawer, the write-up page or Sherlock's confirmation card). It
re-checks the opt-in switch, sends the prepared payload unchanged, and records
the publish so a later republish updates the same page or note.

Sherlock can only reach :func:`prepare`; it has no path to :func:`confirm`.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nexus.publish.content import (
    PublishDoc,
    load_doc,
    replace_section,
    to_storage,
    to_text,
)

logger = logging.getLogger("nexus.publish")

TOKEN_TTL = 15 * 60
MAX_PENDING = 200
POOL_SOURCE = "Analyst write-back"
POOL_KIND = "analyst"
TARGET_LABELS = {"confluence": "Confluence", "obsidian": "Obsidian vault",
                 "pool": "Knowledge pool"}


class PublishError(Exception):
    """A refusal or failure whose message is safe to show the analyst."""


@dataclass
class Prepared:
    subject_kind: str
    subject_id: int
    target: str                  # confluence | obsidian | pool
    target_id: int
    mode: str                    # create | update | append | note | pool
    title: str
    payload: str                 # exactly what is sent / written
    destination: str             # human description of where it goes
    options: dict = field(default_factory=dict)
    origin: str = "ui"           # ui | sherlock
    token: str = ""
    created: float = 0.0


_PENDING: dict[str, Prepared] = {}
_LOCK = threading.Lock()


def _park(p: Prepared) -> Prepared:
    now = time.monotonic()
    with _LOCK:
        for tok in [t for t, v in _PENDING.items() if now - v.created > TOKEN_TTL]:
            _PENDING.pop(tok, None)
        while len(_PENDING) >= MAX_PENDING:
            _PENDING.pop(next(iter(_PENDING)))
        p.token = secrets.token_urlsafe(24)
        p.created = now
        _PENDING[p.token] = p
    return p


def _take(token: str) -> Prepared | None:
    with _LOCK:
        p = _PENDING.pop(str(token or ""), None)
    if p is None or time.monotonic() - p.created > TOKEN_TTL:
        return None
    return p


def peek(token: str) -> Prepared | None:
    with _LOCK:
        return _PENDING.get(str(token or ""))


# ------------------------------------------------------------------ prepare
def _doc(conn, kind: str, subject_id: int) -> PublishDoc:
    doc = load_doc(conn, kind, int(subject_id))
    if doc is None:
        raise PublishError("that item or write-up no longer exists")
    return doc


def _confluence(conn, doc: PublishDoc, opts: dict) -> Prepared:
    from nexus.publish.confluence import load_publisher, valid_page_id, valid_space_key
    from nexus.storage.publishing import find_publication

    try:
        source_id = int(opts.get("source_id") or 0)
    except (TypeError, ValueError):
        source_id = 0
    pub = load_publisher(conn, source_id) if source_id else None
    if pub is None:
        raise PublishError("choose a Confluence connection (Sources > Confluence)")
    ok, why = pub.write_allowed()
    if not ok:
        raise PublishError(why)
    mode = str(opts.get("mode") or "create")
    title = " ".join(str(opts.get("title") or doc.title).split())[:255] or doc.title[:255]
    if mode == "append":
        page_id = valid_page_id(opts.get("page_id"))
        if page_id is None:
            raise PublishError("choose the page to append to")
        page_title = " ".join(str(opts.get("page_title") or "").split())[:200] or f"page {page_id}"
        again = find_publication(conn, doc.kind, doc.subject_id, "confluence", source_id,
                                 remote_id=page_id)
        dest = (f"{pub.name}: update this {doc.kind}'s section on “{page_title}”"
                if again else f"{pub.name}: append a section to “{page_title}”")
        return Prepared(doc.kind, doc.subject_id, "confluence", source_id, "append", title,
                        to_storage(doc, as_section=True), dest,
                        {"page_id": page_id, "page_title": page_title})
    previous = find_publication(conn, doc.kind, doc.subject_id, "confluence", source_id,
                                mode="create")
    if previous:
        return Prepared(doc.kind, doc.subject_id, "confluence", source_id, "update", title,
                        to_storage(doc),
                        f"{pub.name}: update Nexus's part of the page published before "
                        f"(“{previous.get('title') or previous['remote_id']}”); "
                        "anything else on that page is kept",
                        {"page_id": previous["remote_id"]})
    space = valid_space_key(opts.get("space"))
    if space is None:
        raise PublishError("choose the space for the new page")
    if opts.get("restrict_spaces") and space not in pub.spaces():
        raise PublishError(f"space {space} is not one of this connection's chosen spaces")
    parent = valid_page_id(opts.get("parent_id")) if opts.get("parent_id") else None
    dest = (f"{pub.name}: new page “{title}” in space {space}"
            + (f" under page {parent}" if parent else ""))
    return Prepared(doc.kind, doc.subject_id, "confluence", source_id, "create", title,
                    to_storage(doc), dest, {"space": space, "parent_id": parent})


def _vault_row(conn, vault_id: int) -> dict:
    from nexus.storage.obsidian_vaults import get_obsidian_vault

    row = get_obsidian_vault(conn, vault_id) if vault_id else None
    if row is None:
        raise PublishError("choose an Obsidian vault (Sources > Obsidian)")
    return row


def _obsidian(conn, doc: PublishDoc, opts: dict) -> Prepared:
    from nexus.publish.vault import VaultWriteError, plan_note, render
    from nexus.storage.publishing import find_publication

    try:
        vault_id = int(opts.get("vault_id") or 0)
    except (TypeError, ValueError):
        vault_id = 0
    vault = _vault_row(conn, vault_id)
    if not vault.get("allow_write"):
        raise PublishError("writing is off for this vault: turn on \"Allow writing to the "
                           "vault\" under Sources > Obsidian")
    previous = find_publication(conn, doc.kind, doc.subject_id, "obsidian", vault_id)
    try:
        plan = plan_note(vault, doc, previous["remote_id"] if previous else None)
    except VaultWriteError as exc:
        raise PublishError(str(exc)) from exc
    verb = "update the note" if plan["action"] == "update" else "create the note"
    return Prepared(doc.kind, doc.subject_id, "obsidian", vault_id, "note", doc.title,
                    render(doc), f"{vault['name']}: {verb} {plan['rel']}",
                    {"previous_rel": previous["remote_id"] if previous else None})


def _pool(conn, doc: PublishDoc) -> Prepared:
    from nexus.storage.publishing import find_publication

    again = find_publication(conn, doc.kind, doc.subject_id, "pool", 0)
    dest = ("Knowledge pool: replace the earlier version" if again
            else "Knowledge pool: add as a document Sherlock can answer from")
    return Prepared(doc.kind, doc.subject_id, "pool", 0, "pool", doc.title, to_text(doc), dest)


def prepare(conn, kind: str, subject_id: int, target: str, options: dict | None = None,
            *, origin: str = "ui") -> Prepared:
    """Render and park a publish. Raises :class:`PublishError` (safe message)."""
    opts = dict(options or {})
    if kind not in ("item", "writeup"):
        raise PublishError("unknown subject")
    doc = _doc(conn, kind, subject_id)
    if target == "confluence":
        p = _confluence(conn, doc, opts)
    elif target == "obsidian":
        p = _obsidian(conn, doc, opts)
    elif target == "pool":
        p = _pool(conn, doc)
    else:
        raise PublishError("unknown publish target")
    p.origin = origin
    return _park(p)


# ------------------------------------------------------------------ confirm
def _do_confluence(conn, p: Prepared) -> dict:
    from nexus.publish.confluence import load_publisher
    from nexus.publish.content import PublishDoc as _Doc
    from nexus.sources.confluence import ConfluenceError
    from nexus.storage.publishing import find_publication, forget_publication

    pub = load_publisher(conn, p.target_id)
    if pub is None:
        raise PublishError("the Confluence connection was removed")
    ok, why = pub.write_allowed()
    if not ok:
        raise PublishError(why)
    try:
        if p.mode == "create":
            page = pub.create_page(p.options["space"], p.title, p.payload,
                                   p.options.get("parent_id"))
        elif p.mode == "update":
            marker = _Doc(kind=p.subject_kind, subject_id=p.subject_id, title=p.title)

            def build_update(page: dict) -> tuple[str, str]:
                # Replace only Nexus's anchored part; colleagues' additions stay.
                body, _replaced = replace_section(page["body"], marker, p.payload)
                return p.title, body

            try:
                page = pub.update_page(p.options["page_id"], build_update)
            except ConfluenceError as exc:
                if "404" not in str(exc):
                    raise
                # The page was deleted in Confluence: forget it; prepare again to create.
                old = find_publication(conn, p.subject_kind, p.subject_id, "confluence",
                                       p.target_id, remote_id=p.options["page_id"])
                if old:
                    forget_publication(conn, old["id"])
                raise PublishError("the page published before no longer exists in "
                                   "Confluence; publish again to create a new one") from exc
        else:  # append (or update our earlier section)
            marker_doc = _Doc(kind=p.subject_kind, subject_id=p.subject_id, title=p.title)

            def build(page: dict) -> tuple[str, str]:
                body, _replaced = replace_section(page["body"], marker_doc, p.payload)
                return page["title"], body

            page = pub.update_page(p.options["page_id"], build)
    except ConfluenceError as exc:
        raise PublishError(f"Confluence: {exc}") from exc
    mode = "create" if p.mode in ("create", "update") else "append"
    return {"remote_id": page["id"], "location": page.get("space") or "",
            "url": page.get("url"), "title": page.get("title") or p.title, "mode": mode,
            "label": f"Published to Confluence: “{page.get('title') or p.title}”"}


def _do_obsidian(conn, p: Prepared) -> dict:
    from nexus.publish.vault import VaultWriteError, obsidian_uri, write_note

    vault = _vault_row(conn, p.target_id)
    doc = _doc(conn, p.subject_kind, p.subject_id)
    try:
        res = write_note(vault, doc, p.payload, p.options.get("previous_rel"))
    except VaultWriteError as exc:
        raise PublishError(str(exc)) from exc
    except OSError as exc:
        raise PublishError(f"could not write the note ({type(exc).__name__})") from exc
    return {"remote_id": res["rel"], "location": res["rel"], "url": obsidian_uri(vault, res["rel"]),
            "title": p.title, "mode": "note",
            "label": f"Saved to Obsidian: {res['rel']}"}


def _do_pool(conn, p: Prepared) -> dict:
    from nexus.ingest.connector import ConnectorDoc, Target, sync_document

    doc = _doc(conn, p.subject_kind, p.subject_id)
    res = sync_document(
        conn, kind=POOL_KIND, source_id=0, source_name=POOL_SOURCE, track="analyst",
        doc=ConnectorDoc(key=f"{p.subject_kind}:{p.subject_id}", title=p.title, text=p.payload,
                         url=doc.source_url, modified=datetime.now(UTC),
                         meta={"nexus_id": doc.nexus_id, "edited": doc.edited}),
        target=Target(case_id=None, in_pool=True, no_ai=False),
    )
    doc_id = res.get("document_id")
    if doc_id is None:  # unchanged since the last add
        row = conn.execute(
            "SELECT document_id FROM connector_docs WHERE kind = ? AND source_id = 0 "
            "AND doc_key = ?", (POOL_KIND, f"{p.subject_kind}:{p.subject_id}")).fetchone()
        doc_id = row["document_id"] if row else 0
    return {"remote_id": str(doc_id), "location": "knowledge pool", "url": "/sources/documents",
            "title": p.title, "mode": "pool",
            "label": "Added to the knowledge pool (Sherlock can answer from it)"}


def _log_publish(conn, p: Prepared, mode: str, *, ok: bool, url: str = "") -> None:
    """Outbound activity: Confluence leaves the machine; an Obsidian vault may
    be synced elsewhere. The knowledge pool is local and not logged."""
    from nexus.security.outbound import host_of, record

    if p.target == "confluence":
        target = host_of(url) or "Confluence"
    elif p.target == "obsidian":
        target = "Obsidian vault"
    else:
        return
    record("publish", target, f"{p.target}: {mode}", items=1, ok=ok, conn=conn)


def confirm(conn, token: str) -> dict:
    """Execute a prepared publish. Returns ``{ok, label, url}`` (never raises)."""
    from nexus.storage.publishing import record_publication

    p = _take(token)
    if p is None:
        return {"ok": False, "label": "This preview expired or was already used. "
                "Open the publish dialog again."}
    try:
        if p.target == "confluence":
            res = _do_confluence(conn, p)
        elif p.target == "obsidian":
            res = _do_obsidian(conn, p)
        else:
            res = _do_pool(conn, p)
        record_publication(conn, subject_kind=p.subject_kind, subject_id=p.subject_id,
                           target=p.target, target_id=p.target_id, mode=res["mode"],
                           remote_id=res["remote_id"], location=res["location"],
                           url=res["url"], title=res["title"])
        _log_publish(conn, p, res["mode"], ok=True, url=res.get("url") or "")
        conn.commit()
    except PublishError as exc:
        _log_publish(None, p, "publish", ok=False)
        return {"ok": False, "label": str(exc)}
    except Exception:
        logger.exception("Publishing to %s failed", p.target)
        return {"ok": False, "label": "Publishing failed unexpectedly; nothing was recorded."}
    logger.info("Published %s %s to %s (%s)", p.subject_kind, p.subject_id, p.target, p.origin)
    return {"ok": True, "label": res["label"], "url": res["url"]}
