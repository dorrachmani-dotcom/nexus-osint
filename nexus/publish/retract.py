"""Clean up what a write-up published, when the write-up is deleted.

:func:`plan` reads the publish log and says, per target, what can be removed
and whether it is ticked by default. :func:`run` removes the targets the
analyst ticked (re-checking every rule right before acting) and returns one
result per target. Nothing here deletes a Confluence page or a note Nexus did
not write.

Rules
  * Knowledge pool: the analyst document is local; removing it is the default.
  * Obsidian: the note is removed only when it sits directly in the vault's
    write folder (default ``Nexus/``), still carries Nexus's frontmatter marker
    for this write-up, has nothing typed below the ``%% nexus:end %%`` line and
    was not changed after Nexus last wrote it. Otherwise it is left alone and
    the reason is shown. Needs the vault's "Allow writing" switch.
  * Confluence: only Nexus's own anchored section is removed from the page;
    the page itself is never deleted. Needs the connection's "Allow publishing
    to Confluence" switch, and is never ticked by default: a shared team page
    is only edited when the analyst asks for it. 409 / 429 handling is the
    publisher's.

A publish record is forgotten only when its target was cleaned up (or is
already gone). Anything left in place, or unreachable, keeps its record.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from nexus.publish.content import PublishDoc, remove_section

logger = logging.getLogger("nexus.publish.retract")

# A note modified this long after Nexus's last write counts as edited by hand.
EDIT_GRACE_S = 120


def _doc(kind: str, subject_id: int) -> PublishDoc:
    return PublishDoc(kind=kind, subject_id=int(subject_id), title="")


def _when(text: str | None) -> float | None:
    try:
        return datetime.strptime(str(text or ""), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC).timestamp()
    except ValueError:
        return None


def _what(pub: dict) -> str:
    if pub["target"] == "confluence":
        what = "section" if pub.get("mode") == "append" else "page"
        return f"Confluence {what} “{pub.get('title') or pub['remote_id']}”"
    if pub["target"] == "obsidian":
        return f"Obsidian note {pub.get('location') or pub['remote_id']}"
    return "Knowledge-pool document"


# ------------------------------------------------------------------ obsidian
def _note_check(conn, pub: dict) -> tuple[Path | None, str]:
    """``(path, "")`` when the note may be removed, else ``(None, why)``."""
    from nexus.publish.vault import VaultWriteError, _inside, _kept_tail, nexus_marker, note_dir
    from nexus.sources.obsidian import check_vault
    from nexus.storage.obsidian_vaults import get_obsidian_vault

    vault = get_obsidian_vault(conn, int(pub["target_id"]))
    if vault is None:
        return None, "the vault was removed from Sources > Obsidian; the note is left as is"
    if not vault.get("allow_write"):
        return None, ("writing is off for this vault (Sources > Obsidian); "
                      "the note is left as is")
    root, reason = check_vault(vault.get("path"))
    if root is None:
        return None, f"vault not reachable ({reason}); the note is left as is"
    try:
        folder = note_dir(vault, create=False)
    except VaultWriteError as exc:
        return None, f"{exc}; the note is left as is"
    path = root / str(pub["remote_id"])
    if not path.exists() and not path.is_symlink():
        return path, ""  # already gone: nothing to remove, the record can go
    if path.is_symlink() or not _inside(path, folder):
        return None, "the note is not in Nexus's own vault folder; it is left as is"
    if nexus_marker(path) != _doc(pub["subject_kind"], pub["subject_id"]).nexus_id:
        return None, "the note no longer carries Nexus's marker; it is left as is"
    if _kept_tail(path).strip():
        return None, "you added text below the Nexus line in this note; it is left as is"
    written = _when(pub.get("published_at"))
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None, "the note could not be read; it is left as is"
    if written is not None and mtime > written + EDIT_GRACE_S:
        return None, "the note was changed after Nexus wrote it; it is left as is"
    return path, ""


def _remove_note(conn, pub: dict) -> dict:
    path, why = _note_check(conn, pub)
    if path is None:
        return {"status": "kept", "message": why}
    if not path.exists():
        return {"status": "removed", "message": "the note was already gone", "forget": True}
    try:
        path.unlink()
    except OSError as exc:
        return {"status": "failed",
                "message": f"could not remove the note ({type(exc).__name__})"}
    return {"status": "removed", "message": f"removed {pub['remote_id']}", "forget": True}


# ---------------------------------------------------------------- confluence
class _NoSection(Exception):
    pass


def _confluence_check(conn, pub: dict):
    from nexus.publish.confluence import load_publisher, valid_page_id

    p = load_publisher(conn, int(pub["target_id"]))
    if p is None:
        return None, "the Confluence connection was removed; the page is left as is"
    ok, why = p.write_allowed()
    if not ok:
        return None, f"{why}; the page is left as is"
    if valid_page_id(pub["remote_id"]) is None:
        return None, "the recorded page id is not valid; the page is left as is"
    return p, ""


def _remove_section(conn, pub: dict) -> dict:
    from nexus.sources.confluence import ConfluenceError

    publisher, why = _confluence_check(conn, pub)
    if publisher is None:
        return {"status": "kept", "message": why}
    marker = _doc(pub["subject_kind"], pub["subject_id"])

    def build(page: dict) -> tuple[str, str]:
        body, removed = remove_section(page["body"], marker)
        if not removed:
            raise _NoSection
        return page["title"], body if body.strip() else "<p></p>"

    try:
        publisher.update_page(str(pub["remote_id"]), build)
    except _NoSection:
        return {"status": "removed", "forget": True,
                "message": "Nexus's section was no longer on the page; nothing changed"}
    except ConfluenceError as exc:
        if "404" in str(exc):
            return {"status": "removed", "forget": True,
                    "message": "the page no longer exists in Confluence"}
        return {"status": "failed", "message": f"Confluence: {exc}; the record is kept"}
    return {"status": "removed", "forget": True,
            "message": "removed Nexus's section; the page and anything colleagues "
                       "added to it stay"}


# ---------------------------------------------------------------------- pool
def _remove_pool(conn, pub: dict) -> dict:
    from nexus.ingest.connector import remove_document
    from nexus.publish.service import POOL_KIND, POOL_SOURCE

    remove_document(conn, POOL_KIND, 0, f"{pub['subject_kind']}:{pub['subject_id']}",
                    POOL_SOURCE)
    return {"status": "removed", "forget": True, "message": "removed from the knowledge pool"}


# ---------------------------------------------------------------------- API
def plan(conn, kind: str, subject_id: int) -> list[dict]:
    """One row per recorded publish: ``{pub, what, allowed, default, note}``.

    Reads the vault and the local database; never touches the network.
    """
    from nexus.storage.publishing import list_publications

    rows = []
    for pub in list_publications(conn, kind, subject_id):
        if pub["target"] == "pool":
            allowed, note = True, "the local analyst document Sherlock answers from"
        elif pub["target"] == "obsidian":
            path, why = _note_check(conn, pub)
            allowed = path is not None
            note = why or "a note Nexus wrote and nobody changed since"
        else:
            publisher, why = _confluence_check(conn, pub)
            allowed = publisher is not None
            note = why or ("only Nexus's section is removed; the page itself is never "
                           "deleted")
        # A shared Confluence page is edited only on an explicit tick.
        default = allowed and pub["target"] != "confluence"
        rows.append({"pub": pub, "what": _what(pub), "allowed": allowed,
                     "default": default, "note": note})
    return rows


def run(conn, kind: str, subject_id: int, selected: set[int]) -> list[dict]:
    """Remove the ticked targets. Returns ``{what, target, status, message}``
    per recorded publish (``removed`` | ``kept`` | ``failed``); never raises."""
    from nexus.storage.publishing import forget_publication, list_publications

    out = []
    for pub in list_publications(conn, kind, subject_id):
        what = _what(pub)
        if int(pub["id"]) not in selected:
            why = ""
            if pub["target"] == "obsidian":
                why = _note_check(conn, pub)[1]
            elif pub["target"] == "confluence":
                why = _confluence_check(conn, pub)[1]
            res = {"status": "kept", "message": why or "not selected; left as is"}
        else:
            try:
                if pub["target"] == "pool":
                    res = _remove_pool(conn, pub)
                elif pub["target"] == "obsidian":
                    res = _remove_note(conn, pub)
                else:
                    res = _remove_section(conn, pub)
            except Exception:
                logger.exception("Cleaning up a %s publish failed", pub["target"])
                res = {"status": "failed", "message": "failed unexpectedly; the record is kept"}
        if res.pop("forget", False):
            forget_publication(conn, int(pub["id"]))
        if pub["target"] in ("confluence", "obsidian") and res["status"] != "kept":
            from nexus.security.outbound import host_of, record

            if pub["target"] == "confluence":
                dest = host_of(pub.get("url")) or "Confluence"
            else:
                dest = "Obsidian vault"
            record("publish", dest, f"{pub['target']}: remove", items=1,
                   ok=res["status"] == "removed", conn=conn)
        logger.info("Write-up cleanup: %s %s -> %s", pub["target"], pub["id"], res["status"])
        out.append({"what": what, "target": pub["target"], "url": pub.get("url"), **res})
    return out
