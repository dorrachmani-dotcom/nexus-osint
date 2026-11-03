"""Write Markdown notes into an Obsidian vault - only inside Nexus's own
subfolder, never over a note the analyst wrote.

Rules
  * Writing needs the vault's opt-in switch ("Allow writing to the vault",
    off by default).
  * Notes go only into the vault's write folder (default ``Nexus/``): a
    relative path with no ``..``, no hidden or drive components; every existing
    component must be a real folder (no symlink / junction), and the resolved
    folder must still be inside the vault.
  * A note is only ever replaced when it carries Nexus's frontmatter marker
    (``generated_by: Nexus-OSINT``) with the SAME ``nexus_id``. Anything the
    analyst typed below the ``%% nexus:end ... %%`` line is kept. Any other
    file with the wanted name is left alone and the note becomes
    ``name (2).md``, ``name (3).md`` ...
  * New notes are created exclusively (``open(..., "x")``); updates are written
    to a temp file in the same folder and swapped in atomically.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

from nexus.publish.content import (
    ID_KEY,
    KEEP_LINE,
    MARKER_KEY,
    MARKER_VALUE,
    PublishDoc,
    to_markdown as render_markdown,
)

DEFAULT_FOLDER = "Nexus"
MAX_DEPTH = 4
MAX_SUFFIX = 50
_BAD_PART = re.compile(r'[<>:"|?*\x00-\x1f]')
_HEAD_BYTES = 64 * 1024
# Windows device names: a file or folder with one of these names is unusable.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (junctions, symlinks)


def _reserved(name: str) -> bool:
    return name.split(".", 1)[0].strip().upper() in _RESERVED


class VaultWriteError(Exception):
    """Refused or failed (message is safe to show)."""


def clean_folder(raw) -> str | None:
    """A safe vault-relative folder like ``Nexus`` or ``Nexus/Cases``; None if unsafe."""
    text = str(raw or "").strip().replace("\\", "/").strip("/")
    if not text:
        return DEFAULT_FOLDER
    parts = text.split("/")
    if len(parts) > MAX_DEPTH:
        return None
    for part in parts:
        p = part.strip()
        if (not p or p in (".", "..") or p.startswith(".") or p != part
                or _BAD_PART.search(p) or len(p) > 80 or p.endswith(".") or _reserved(p)):
            return None
    return "/".join(parts)


def _safe_name(title: str) -> str:
    from nexus.obsidian import _safe_name as safe

    name = safe(title, fallback="Nexus note", limit=80)
    return f"Nexus - {name}" if _reserved(name) else name


def note_dir(vault: dict, *, create: bool) -> Path:
    """The vault's write folder, validated (and created when ``create``)."""
    from nexus.sources.obsidian import check_vault

    root, reason = check_vault(vault.get("path"))
    if root is None:
        raise VaultWriteError(f"vault not available: {reason}")
    folder = clean_folder(vault.get("write_folder"))
    if folder is None:
        raise VaultWriteError("the vault write folder is not a safe relative folder name")
    current = root
    for part in folder.split("/"):
        current = current / part
        if current.is_symlink() or _is_junction(current):
            raise VaultWriteError("the vault write folder is a link; refusing to follow it")
        if current.exists():
            if not current.is_dir():
                raise VaultWriteError("the vault write folder exists but is not a folder")
        elif create:
            current.mkdir()
        else:
            return current  # not created yet; nothing to check further
    real = current.resolve(strict=True)
    if not real.is_relative_to(root):
        raise VaultWriteError("the vault write folder leads outside the vault")
    return real


def _is_junction(path: Path) -> bool:
    """A Windows junction (or any reparse point) - also on Python 3.11."""
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _REPARSE_POINT)


def _read_head(path: Path) -> str:
    with path.open("rb") as fh:
        return fh.read(_HEAD_BYTES).decode("utf-8", errors="replace")


def nexus_marker(path: Path) -> str | None:
    """The ``nexus_id`` of a note Nexus wrote, or None (user note / unreadable)."""
    from nexus.ingest.obsidian_notes import parse_frontmatter

    try:
        if path.is_symlink() or not path.is_file():
            return None
        fm, _body = parse_frontmatter(_read_head(path))
    except OSError:
        return None
    if str(fm.get(MARKER_KEY) or "") != MARKER_VALUE:
        return None
    value = fm.get(ID_KEY)
    return str(value) if value else None


def _kept_tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    idx = _keep_index(text)
    return text[idx + len(KEEP_LINE):] if idx >= 0 else ""


def _keep_index(text: str) -> int:
    """Where our end marker line starts (searched only after the frontmatter)."""
    start = 0
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        start = end + 5 if end >= 0 else 0
    idx = text.find("\n" + KEEP_LINE, start)
    return idx + 1 if idx >= 0 else -1


def _replace_atomically(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".nexus-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        if path.is_symlink():
            raise VaultWriteError("the note became a link; refusing to replace it")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.parent.resolve(strict=True) == folder
    except OSError:
        return False


def plan_note(vault: dict, doc: PublishDoc, previous_rel: str | None = None) -> dict:
    """Decide where the note goes (no writing): ``{rel, path, action}``.

    ``action`` is ``update`` (our own note for this subject) or ``create``.
    """
    from nexus.sources.obsidian import check_vault

    root, reason = check_vault(vault.get("path"))
    if root is None:
        raise VaultWriteError(f"vault not available: {reason}")
    folder = note_dir(vault, create=False)
    if previous_rel:
        prev = (root / previous_rel)
        if prev.exists() and _inside(prev, folder) and nexus_marker(prev) == doc.nexus_id:
            return {"rel": prev.relative_to(root).as_posix(), "path": prev, "action": "update"}
    base = _safe_name(doc.title)
    for n in range(1, MAX_SUFFIX + 1):
        name = f"{base}.md" if n == 1 else f"{base} ({n}).md"
        path = folder / name
        if not path.exists() and not path.is_symlink():
            return {"rel": path.relative_to(root).as_posix(), "path": path, "action": "create"}
        if nexus_marker(path) == doc.nexus_id:
            return {"rel": path.relative_to(root).as_posix(), "path": path, "action": "update"}
    raise VaultWriteError("too many notes with this name in the write folder")


def write_note(vault: dict, doc: PublishDoc, text: str,
               previous_rel: str | None = None) -> dict:
    """Write ``text`` (from :func:`render`) for ``doc``. Returns ``{rel, action}``."""
    if not vault.get("allow_write"):
        raise VaultWriteError("writing is off for this vault: turn on \"Allow writing to the "
                              "vault\" under Sources > Obsidian")
    folder = note_dir(vault, create=True)
    plan = plan_note(vault, doc, previous_rel)
    path: Path = plan["path"]
    if not _inside(path, folder):
        raise VaultWriteError("refusing to write outside the vault write folder")
    if plan["action"] == "update":
        if nexus_marker(path) != doc.nexus_id:  # re-check right before writing
            raise VaultWriteError("the note changed owner; not overwriting it")
        tail = _kept_tail(path)
        if tail.strip():
            idx = _keep_index(text)
            text = (text[:idx + len(KEEP_LINE)] if idx >= 0 else text) + tail
        _replace_atomically(path, text)
    else:
        try:
            with path.open("x", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except FileExistsError as exc:
            raise VaultWriteError("a note with this name appeared meanwhile; try again") from exc
    return {"rel": plan["rel"], "action": plan["action"]}


def render(doc: PublishDoc) -> str:
    return render_markdown(doc)


def obsidian_uri(vault: dict, rel: str) -> str | None:
    """``obsidian://open?vault=...&file=...`` for the "Published to" link."""
    from urllib.parse import quote

    from nexus.sources.obsidian import check_vault

    root, _reason = check_vault(vault.get("path"))
    if root is None:
        return None
    file = rel[:-3] if rel.lower().endswith(".md") else rel
    return f"obsidian://open?vault={quote(root.name)}&file={quote(file)}"
