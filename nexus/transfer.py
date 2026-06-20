"""Air-gap transfer — move collected intelligence between two machines.

The intended workflow (the analyst's "two-computer" setup):

  * Machine A (online collector): runs scans, gathers items, AI analysis, scores
    and evidence exactly as today.
  * Machine A exports a portable ``.nexusbundle`` file (a ZIP) to a USB stick —
    either *everything*, or only what is *new since the last export* (so a daily
    "Neymar" run produces just that day's delta).
  * Machine B (air-gapped analysis station): runs the same Nexus app with no
    network. It imports the bundle and instantly sees every new item, with its
    summary, translation, entities, requirement scores and evidence screenshots.
    Article links are preserved for reference but, being offline, simply won't
    open — which is expected and fine.

Design notes:
  * Items are keyed on their natural ``content_hash`` (stable across machines),
    NOT on the auto-increment ``id``. Import lets SQLite assign fresh local ids
    and re-links analysis/scores/evidence to them, so the two databases never
    collide and re-importing is idempotent.
  * No secrets ever travel in a bundle — only collected open-source data and its
    analysis. ``.env`` and configuration stay on each machine.
  * Evidence images are carried as files inside the ZIP and written under
    ``data/evidence`` on import (basename only — no path traversal).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from nexus.config import get_settings
from nexus.storage import get_meta, set_meta

logger = logging.getLogger("nexus.transfer")

BUNDLE_VERSION = 1
BUNDLE_MAGIC = "nexus-osint-bundle"
_LAST_EXPORT_KEY = "transfer:last_export_at"


# --------------------------------------------------------------------------- #
# Export                                                                       #
# --------------------------------------------------------------------------- #
def _select_item_ids(
    conn: sqlite3.Connection,
    *,
    scope: str,
    case_id: int | None,
    since: str | None,
) -> list[int]:
    """Item ids to export for the chosen scope (newest first)."""
    where: list[str] = []
    params: list = []
    if scope == "case" and case_id:
        where.append(
            "i.id IN (SELECT item_id FROM bookmarks WHERE case_id = ?)"
        )
        params.append(case_id)
    if since:
        # "New since" uses fetched_at: when the collector first stored the item.
        where.append("COALESCE(i.fetched_at, i.published_at) > ?")
        params.append(since)
    sql = "SELECT i.id FROM items i"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(i.published_at, i.fetched_at) DESC"
    return [int(r[0]) for r in conn.execute(sql, params).fetchall()]


def export_bundle(
    conn: sqlite3.Connection,
    *,
    scope: str = "all",
    case_id: int | None = None,
    only_new: bool = False,
) -> tuple[bytes, dict]:
    """Build a ``.nexusbundle`` (ZIP bytes) for the chosen scope.

    Returns ``(zip_bytes, summary)``. ``summary`` reports counts and the
    ``since`` watermark used, for display. When ``only_new`` is set the export
    includes just the items collected after the previous export, and on success
    the watermark is advanced.
    """
    since = get_meta(conn, _LAST_EXPORT_KEY) if only_new else None
    started_at = datetime.now(timezone.utc).isoformat()

    item_ids = _select_item_ids(conn, scope=scope, case_id=case_id, since=since)

    settings = get_settings()
    data_root = settings.data_path.resolve()

    records: list[dict] = []
    evidence_files: dict[str, Path] = {}  # arcname -> absolute source path

    for iid in item_ids:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
        if row is None:
            continue
        it = dict(row)
        cluster = conn.execute(
            "SELECT shared_count FROM clusters WHERE id = ?", (it.get("cluster_id"),)
        ).fetchone()

        rec: dict = {
            "content_hash": it["content_hash"],
            "dedup_key": it.get("dedup_key"),
            "source": it["source"],
            "track": it.get("track") or "api",
            "external_id": it.get("external_id"),
            "url": it.get("url"),
            "author": it.get("author"),
            "title": it.get("title"),
            "content": it.get("content") or "",
            "summary": it.get("summary") or "",
            "language": it.get("language"),
            "published_at": it.get("published_at"),
            "fetched_at": it.get("fetched_at"),
            "media_urls": it.get("media_urls") or "[]",
            "raw": it.get("raw") or "{}",
            "shared_count": int(cluster["shared_count"]) if cluster else 1,
        }

        an = conn.execute(
            "SELECT * FROM analyses WHERE item_id = ?", (iid,)
        ).fetchone()
        if an is not None:
            a = dict(an)
            rec["analysis"] = {
                "threat_level": a.get("threat_level") or "none",
                "summary": a.get("summary"),
                "translation": a.get("translation"),
                "target_lang": a.get("target_lang"),
                "entities": a.get("entities") or "[]",
                "party": a.get("party") or "unknown",
                "contradiction": int(a.get("contradiction") or 0),
                "model": a.get("model"),
                "analyzed_at": a.get("analyzed_at"),
            }

        hits = conn.execute(
            """
            SELECT rq.question, rq.priority, h.score, h.rationale, h.model, h.scored_at
            FROM requirement_hits h
            JOIN requirements rq ON rq.id = h.requirement_id
            WHERE h.item_id = ?
            """,
            (iid,),
        ).fetchall()
        if hits:
            rec["requirements"] = [
                {
                    "question": h["question"],
                    "priority": int(h["priority"]) if h["priority"] is not None else 2,
                    "score": int(h["score"]) if h["score"] is not None else 0,
                    "rationale": h["rationale"],
                    "model": h["model"],
                    "scored_at": h["scored_at"],
                }
                for h in hits
            ]

        ev_rows = conn.execute(
            "SELECT screenshot, sha256, captured_at, ocr_text FROM evidence "
            "WHERE item_id = ? ORDER BY id",
            (iid,),
        ).fetchall()
        ev_list: list[dict] = []
        for ev in ev_rows:
            rel = ev["screenshot"]
            name = os.path.basename(rel)
            src = data_root / rel
            arcname = f"evidence/{name}"
            if src.is_file():
                evidence_files[arcname] = src
            ev_list.append(
                {
                    "filename": name,
                    "sha256": ev["sha256"],
                    "captured_at": ev["captured_at"],
                    "ocr_text": ev["ocr_text"],
                    "present": src.is_file(),
                }
            )
        if ev_list:
            rec["evidence"] = ev_list

        records.append(rec)

    # Optional case envelope (name + description + notes) so the case rebuilds
    # on the far side with its researcher notes intact.
    case_block = None
    if scope == "case" and case_id:
        crow = conn.execute(
            "SELECT name, description FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        if crow is not None:
            notes = conn.execute(
                "SELECT body, created_at FROM notes WHERE case_id = ? ORDER BY id",
                (case_id,),
            ).fetchall()
            case_block = {
                "name": crow["name"],
                "description": crow["description"],
                "notes": [{"body": n["body"], "created_at": n["created_at"]} for n in notes],
            }

    manifest = {
        "magic": BUNDLE_MAGIC,
        "version": BUNDLE_VERSION,
        "created_at": started_at,
        "scope": scope,
        "since": since,
        "item_count": len(records),
        "evidence_count": len(evidence_files),
        "has_case": case_block is not None,
    }

    payload = {"manifest": manifest, "items": records}
    if case_block is not None:
        payload["case"] = case_block

    # Serialize the payload once, then checksum exactly those bytes. The hash
    # lives only in the standalone manifest (never inside the payload it covers),
    # so the receiving machine can detect a bundle that arrived corrupted — a
    # half-copied USB file, a bad sector — before merging any partial records.
    data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    manifest_out = {**manifest, "data_sha256": hashlib.sha256(data_bytes).hexdigest()}

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest_out, ensure_ascii=False, indent=2))
        zf.writestr("data.json", data_bytes)
        for arcname, src in evidence_files.items():
            try:
                zf.write(src, arcname)
            except OSError:
                logger.warning("Could not add evidence file %s to bundle", src)

    if only_new:
        # Advance the watermark only after a successful build so a failed export
        # never silently skips items next time.
        set_meta(conn, _LAST_EXPORT_KEY, started_at)

    summary = {
        "item_count": len(records),
        "evidence_count": len(evidence_files),
        "since": since,
        "scope": scope,
        "has_case": case_block is not None,
    }
    return buffer.getvalue(), summary


# --------------------------------------------------------------------------- #
# Import                                                                       #
# --------------------------------------------------------------------------- #
def _requirement_id(conn: sqlite3.Connection, question: str, priority: int) -> int:
    """Find (case-insensitively) or create a requirement by its question text."""
    row = conn.execute(
        "SELECT id FROM requirements WHERE lower(question) = lower(?)", (question,)
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO requirements (question, priority, enabled) VALUES (?, ?, 1)",
        (question, priority),
    )
    return int(cur.lastrowid)


def import_bundle(conn: sqlite3.Connection, blob: bytes) -> dict:
    """Merge a ``.nexusbundle`` into the local database.

    Idempotent: items already present (matched on content_hash) are not
    duplicated; their analysis/scores are filled in only where missing. Returns
    a summary dict with counts for the UI.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("That file is not a valid Nexus bundle (not a ZIP).") from exc

    try:
        data_raw = zf.read("data.json")
    except KeyError as exc:
        raise ValueError("That file is not a Nexus bundle (no data.json inside).") from exc
    try:
        payload = json.loads(data_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("The bundle's data is corrupted or unreadable.") from exc

    manifest = payload.get("manifest") or {}
    if manifest.get("magic") != BUNDLE_MAGIC:
        raise ValueError("That file is not a Nexus intelligence bundle.")
    if int(manifest.get("version") or 0) > BUNDLE_VERSION:
        raise ValueError(
            "This bundle was made by a newer version of Nexus. Please update first."
        )

    # Integrity check: the standalone manifest carries a checksum of data.json.
    # If it's present and doesn't match, the file arrived corrupted — refuse it
    # rather than merging garbled records. Older bundles that predate the
    # checksum simply have no field here and skip the check (backward compatible).
    try:
        outer = json.loads(zf.read("manifest.json").decode("utf-8"))
    except (KeyError, ValueError, UnicodeDecodeError):
        outer = {}
    expected = outer.get("data_sha256")
    if expected and hashlib.sha256(data_raw).hexdigest() != expected:
        raise ValueError(
            "This bundle appears to be corrupted (its contents don't match its "
            "checksum). Please re-export it and copy the file again."
        )

    settings = get_settings()
    ev_dir = settings.data_path / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)

    new_items = 0
    skipped_items = 0
    analyses_added = 0
    scores_added = 0
    evidence_added = 0

    items = payload.get("items") or []
    for rec in items:
        chash = rec.get("content_hash")
        if not chash:
            continue

        existing = conn.execute(
            "SELECT id FROM items WHERE content_hash = ?", (chash,)
        ).fetchone()

        if existing is not None:
            item_id = int(existing["id"])
            skipped_items += 1
            # Backfill analysis only if this machine has none for the item.
            if rec.get("analysis") and conn.execute(
                "SELECT 1 FROM analyses WHERE item_id = ?", (item_id,)
            ).fetchone() is None:
                if _insert_analysis(conn, item_id, rec["analysis"]):
                    analyses_added += 1
        else:
            dedup_key = rec.get("dedup_key") or chash
            conn.execute(
                "INSERT OR IGNORE INTO clusters (id, shared_count) VALUES (?, ?)",
                (dedup_key, int(rec.get("shared_count") or 1)),
            )
            cur = conn.execute(
                """
                INSERT INTO items (
                    content_hash, dedup_key, source, track, external_id, url, author,
                    title, content, summary, language, published_at, fetched_at,
                    media_urls, raw, cluster_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chash,
                    dedup_key,
                    rec.get("source") or "import",
                    rec.get("track") or "api",
                    rec.get("external_id"),
                    rec.get("url"),
                    rec.get("author"),
                    rec.get("title"),
                    rec.get("content") or "",
                    rec.get("summary") or "",
                    rec.get("language"),
                    rec.get("published_at"),
                    # fetched_at is NOT NULL; a real export always sets it, but a
                    # partial/hand-built bundle might not — fall back to now so a
                    # missing field never aborts the whole import.
                    rec.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
                    rec.get("media_urls") or "[]",
                    rec.get("raw") or "{}",
                    dedup_key,
                ),
            )
            item_id = int(cur.lastrowid)
            new_items += 1

            if rec.get("analysis") and _insert_analysis(conn, item_id, rec["analysis"]):
                analyses_added += 1

        # Requirement scores (for both new and existing items: add any missing).
        for req in rec.get("requirements") or []:
            q = (req.get("question") or "").strip()
            if not q:
                continue
            rid = _requirement_id(conn, q, int(req.get("priority") or 2))
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO requirement_hits
                    (item_id, requirement_id, score, rationale, model, scored_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    rid,
                    int(req.get("score") or 0),
                    req.get("rationale"),
                    req.get("model"),
                    req.get("scored_at"),
                ),
            )
            if cur.rowcount:
                scores_added += 1

        # Evidence screenshots: write the file (basename only) + DB row.
        for ev in rec.get("evidence") or []:
            name = os.path.basename(ev.get("filename") or "")
            if not name:
                continue
            arcname = f"evidence/{name}"
            already = conn.execute(
                "SELECT 1 FROM evidence WHERE item_id = ? AND sha256 = ?",
                (item_id, ev.get("sha256")),
            ).fetchone()
            if already is not None:
                continue
            try:
                file_bytes = zf.read(arcname)
            except KeyError:
                file_bytes = None
            if file_bytes is not None:
                (ev_dir / name).write_bytes(file_bytes)
            conn.execute(
                "INSERT INTO evidence (item_id, screenshot, sha256, captured_at, ocr_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    item_id,
                    f"evidence/{name}",
                    ev.get("sha256") or "",
                    ev.get("captured_at") or datetime.now(timezone.utc).isoformat(),
                    ev.get("ocr_text"),
                ),
            )
            evidence_added += 1

    # Rebuild the case + its notes on the far side, linking imported items.
    case_block = payload.get("case")
    if case_block and case_block.get("name"):
        _import_case(conn, case_block, [r.get("content_hash") for r in items])

    return {
        "new_items": new_items,
        "skipped_items": skipped_items,
        "analyses_added": analyses_added,
        "scores_added": scores_added,
        "evidence_added": evidence_added,
        "total_in_bundle": len(items),
    }


def _insert_analysis(conn: sqlite3.Connection, item_id: int, a: dict) -> bool:
    """Insert an analysis row from a bundle record. Returns True on insert."""
    conn.execute(
        """
        INSERT INTO analyses (
            item_id, threat_level, summary, translation, target_lang,
            entities, party, contradiction, model, analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO NOTHING
        """,
        (
            item_id,
            a.get("threat_level") or "none",
            a.get("summary"),
            a.get("translation"),
            a.get("target_lang"),
            a.get("entities") or "[]",
            a.get("party") or "unknown",
            int(a.get("contradiction") or 0),
            a.get("model"),
            a.get("analyzed_at"),
        ),
    )
    # Mirror summary into items so FTS indexes it (matches save_analysis).
    if a.get("summary"):
        conn.execute(
            "UPDATE items SET summary = ? WHERE id = ? AND (summary IS NULL OR summary = '')",
            (a["summary"], item_id),
        )
    return True


def _import_case(conn: sqlite3.Connection, case_block: dict, content_hashes: list) -> None:
    """Find/create the case by name, link imported items, import its notes."""
    name = case_block["name"]
    row = conn.execute("SELECT id FROM cases WHERE name = ?", (name,)).fetchone()
    if row is not None:
        case_id = int(row["id"])
    else:
        cur = conn.execute(
            "INSERT INTO cases (name, description) VALUES (?, ?)",
            (name, case_block.get("description")),
        )
        case_id = int(cur.lastrowid)

    for chash in content_hashes:
        if not chash:
            continue
        irow = conn.execute(
            "SELECT id FROM items WHERE content_hash = ?", (chash,)
        ).fetchone()
        if irow is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO bookmarks (item_id, case_id) VALUES (?, ?)",
            (int(irow["id"]), case_id),
        )

    # Import notes we don't already have (dedup on body + created_at).
    for note in case_block.get("notes") or []:
        body = note.get("body")
        if not body:
            continue
        exists = conn.execute(
            "SELECT 1 FROM notes WHERE case_id = ? AND body = ? AND IFNULL(created_at,'') = IFNULL(?, '')",
            (case_id, body, note.get("created_at")),
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO notes (case_id, body, created_at) VALUES (?, ?, ?)",
                (case_id, body, note.get("created_at")),
            )
