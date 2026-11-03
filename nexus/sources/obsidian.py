"""Obsidian: import vault — a local Markdown vault as a watched source.

The analyst points Nexus at a vault folder on this computer. On each scan the
vault is walked **read-only**:

  * hidden folders (``.obsidian/``, ``.trash/``, ``.git/`` …) are skipped, and
    so is anything whose real path is outside the vault (symlinks are never
    followed out of it); optional include / exclude folders and a required tag
    narrow the import;
  * a note is re-read only when its modification time or size changed, and
    stored again only when its content hash changed (new → its text; changed →
    fresh text plus a "what changed" item, like watched web pages); notes that
    disappeared are removed from the knowledge base;
  * each note keeps its frontmatter, tags, ``[[wikilinks]]`` and ``![[embeds]]``
    as metadata; embedded files (PDFs, images, Office files…) inside the vault
    are read through the upload parsers; long notes are split at headings.

Notes go to the chosen case and/or the knowledge pool exactly like uploads (see
:mod:`nexus.ingest.connector`). Size limits bound every scan. Never raises out
of :meth:`ObsidianSource.ingest`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nexus.config import Settings, get_settings
from nexus.ingest.connector import ConnectorDoc, Target, known_keys, remove_missing, sync_document
from nexus.ingest.extract import decode_text
from nexus.ingest.obsidian_notes import is_note_embed, parse_note
from nexus.ingest.safety import DEFAULT_LIMITS
from nexus.models import RawItem
from nexus.sources.base import Source

logger = logging.getLogger("nexus.sources.obsidian")

KIND = "obsidian"
NOTE_EXT = (".md", ".markdown")
MAX_NOTE_BYTES = 5 * 1024 * 1024
MAX_NOTES = 10_000
MAX_SCAN_BYTES = 500 * 1024 * 1024
MAX_EMBEDS_PER_NOTE = 10


def _folders(raw: str | None) -> list[str]:
    out = []
    for tok in (raw or "").replace(";", ",").split(","):
        tok = tok.strip().replace("\\", "/").strip("/")
        if tok and ".." not in tok.split("/"):
            out.append(tok.lower())
    return out


def _under(rel: str, folders: list[str]) -> bool:
    low = rel.lower()
    return any(low == f or low.startswith(f + "/") for f in folders)


@dataclass
class VaultFile:
    rel: str          # vault-relative POSIX path
    path: Path        # real path (inside the vault)
    size: int
    mtime_ns: int


def check_vault(path_text: str | None) -> tuple[Path | None, str]:
    """Resolve and validate a vault folder: ``(path, "")`` or ``(None, reason)``."""
    raw = (path_text or "").strip().strip('"')
    if not raw:
        return None, "enter the vault folder"
    try:
        root = Path(os.path.expanduser(raw)).resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "that folder does not exist on this computer"
    if not root.is_dir():
        return None, "that path is a file, not a folder"
    if root.parent == root:
        return None, "pick the vault folder itself, not a whole drive"
    return root, ""


def walk_vault(root: Path, include: list[str], exclude: list[str]) -> tuple[
        list[VaultFile], dict[str, VaultFile], list[str]]:
    """``(notes, other_files_by_name, notes_for_the_operator)``.

    Never follows a symlink out of the vault; hidden folders are skipped.
    """
    notes: list[VaultFile] = []
    others: dict[str, VaultFile] = {}
    remarks: list[str] = []
    escaped = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        base = Path(dirpath)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = base / name
            try:
                real = full.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not real.is_relative_to(root):
                escaped += 1  # a symlink pointing outside the vault
                continue
            rel = full.relative_to(root).as_posix()
            if include and not _under(rel, include):
                continue
            if exclude and _under(rel, exclude):
                continue
            try:
                st = real.stat()
            except OSError:
                continue
            vf = VaultFile(rel=rel, path=real, size=st.st_size, mtime_ns=st.st_mtime_ns)
            if name.lower().endswith(NOTE_EXT):
                notes.append(vf)
            else:
                others.setdefault(name.lower(), vf)
                others.setdefault(rel.lower(), vf)
    if escaped:
        remarks.append(f"{escaped} link(s) pointing outside the vault were skipped")
    return notes, others, remarks


class ObsidianSource(Source):
    """One imported vault (an ``obsidian_vaults`` row)."""

    def __init__(self, cfg: dict, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cfg = cfg
        self.id = int(cfg["id"])
        self.name = (cfg.get("name") or f"obsidian{self.id}").strip() or f"obsidian{self.id}"
        self.status = ""

    def is_available(self) -> bool:
        if not self.cfg.get("enabled", 1):
            return False
        return check_vault(self.cfg.get("path"))[0] is not None

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        return []  # notes are stored by ingest()

    # ---------------------------------------------------------- helpers
    def _required_tag(self) -> str:
        return (self.cfg.get("required_tag") or "").strip().lstrip("#").lower()

    def _embeds(self, targets: list[str], others: dict[str, VaultFile]) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        for target in targets:
            if is_note_embed(target) or len(out) >= MAX_EMBEDS_PER_NOTE:
                continue
            key = target.strip().replace("\\", "/").lower()
            vf = others.get(key) or others.get(key.rsplit("/", 1)[-1])
            if vf is None or vf.size > DEFAULT_LIMITS.max_file_bytes:
                continue
            try:
                out.append((vf.rel.rsplit("/", 1)[-1], vf.path.read_bytes()))
            except OSError:
                continue
        return out

    def describe(self) -> dict:
        """For the Test button: validate the folder and count what would be read."""
        root, reason = check_vault(self.cfg.get("path"))
        if root is None:
            return {"ok": False, "error": reason}
        notes, others, remarks = walk_vault(root, _folders(self.cfg.get("include_folders")),
                                            _folders(self.cfg.get("exclude_folders")))
        return {"ok": True, "notes": len(notes), "files": len({v.rel for v in others.values()}),
                "is_vault": (root / ".obsidian").is_dir(), "remarks": remarks}

    # ------------------------------------------------------------- ingest
    def ingest(self) -> dict:
        from nexus.db import get_connection
        from nexus.storage.obsidian_vaults import save_obsidian_scan

        root, reason = check_vault(self.cfg.get("path"))
        if root is None:
            self.status = f"error: {reason}"
            self._save(save_obsidian_scan)
            return {"fetched": 0, "new": 0, "status": self.status}
        target = Target.from_row(self.cfg)
        need_tag = self._required_tag()
        checked = stored = changed = new_items = skipped = 0
        total_bytes = 0
        complete = True
        present: set[str] = set()
        try:
            notes, others, remarks = walk_vault(
                root, _folders(self.cfg.get("include_folders")),
                _folders(self.cfg.get("exclude_folders")))
            with get_connection() as conn:
                known = known_keys(conn, KIND, self.id)
            for vf in notes:
                if checked >= MAX_NOTES or total_bytes > MAX_SCAN_BYTES:
                    complete = False
                    remarks.append("stopped at the per-scan size limit; the rest follows next scan")
                    break
                checked += 1
                if vf.size > MAX_NOTE_BYTES:
                    skipped += 1
                    continue
                version = f"{vf.mtime_ns}:{vf.size}"
                prev = known.get(vf.rel)
                if prev and prev.get("version") == version:
                    present.add(vf.rel)
                    continue  # untouched since the last scan
                try:
                    raw = vf.path.read_bytes()
                except OSError:
                    skipped += 1
                    present.add(vf.rel)  # unreadable now; keep what we had
                    continue
                total_bytes += len(raw)
                title = vf.rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                note = parse_note(decode_text(raw), title)
                if need_tag and need_tag not in {t.lower() for t in note.tags}:
                    continue  # filtered out (and removed if it was stored before)
                present.add(vf.rel)
                doc = ConnectorDoc(
                    key=vf.rel, title=note.title, text=note.text,
                    author=str(note.frontmatter.get("author") or "")[:200] or None,
                    modified=datetime.fromtimestamp(vf.mtime_ns / 1e9, tz=UTC),
                    version=version, sections=note.sections,
                    meta={"vault": self.name, "path": vf.rel, "tags": note.tags,
                          "links": note.links, "embeds": note.embeds,
                          "folder": vf.rel.rsplit("/", 1)[0] if "/" in vf.rel else "",
                          "frontmatter": note.frontmatter},
                    attachments=self._embeds(note.embeds, others),
                )
                if note.links:
                    doc.text += "\n\nLinked notes: " + ", ".join(note.links)
                    doc.sections = [*(doc.sections or []),
                                    ("links", "Linked notes: " + ", ".join(note.links))]
                with get_connection() as conn:
                    res = sync_document(conn, kind=KIND, source_id=self.id,
                                        source_name=self.name, track="obsidian",
                                        doc=doc, target=target)
                    if res["change"] == "same":
                        # Content unchanged (e.g. only touched): remember the new mtime.
                        conn.execute(
                            "UPDATE connector_docs SET version = ? WHERE kind = ? AND "
                            "source_id = ? AND doc_key = ?", (version, KIND, self.id, vf.rel))
                stored += res["change"] != "same"
                changed += res["change"] == "changed"
                new_items += int(res.get("new") or 0)
            removed = 0
            if complete:
                with get_connection() as conn:
                    removed = remove_missing(conn, KIND, self.id, present, self.name)
        except Exception as exc:  # never let one vault break a scan
            logger.exception("Obsidian vault '%s' failed", self.name)
            self.status = f"error: {type(exc).__name__}"
            self._save(save_obsidian_scan)
            return {"fetched": checked, "new": new_items, "status": self.status}
        parts = [f"{checked} note(s) checked, {stored - changed} new, {changed} updated"]
        if removed:
            parts.append(f"{removed} removed")
        if skipped:
            parts.append(f"{skipped} skipped (too large or unreadable)")
        self.status = ", ".join(parts) + ("; " + "; ".join(remarks) if remarks else "")
        self._save(save_obsidian_scan)
        logger.info("Obsidian vault '%s': %s", self.name, self.status)
        return {"fetched": checked, "new": new_items, "changed": changed,
                "removed": removed, "status": self.status}

    def _save(self, saver) -> None:
        from nexus.db import get_connection

        try:
            with get_connection() as conn:
                saver(conn, self.id, self.status)
        except Exception:
            logger.exception("Could not save the scan state for vault '%s'", self.name)


def load_obsidian_sources(settings: Settings | None = None) -> list[ObsidianSource]:
    """An ObsidianSource for every enabled vault. Never raises."""
    from nexus.db import get_connection
    from nexus.storage.obsidian_vaults import list_obsidian_vaults

    settings = settings or get_settings()
    try:
        with get_connection() as conn:
            rows = list_obsidian_vaults(conn, enabled_only=True)
    except Exception:
        logger.exception("Could not load Obsidian vaults")
        return []
    return [ObsidianSource(row, settings) for row in rows]
