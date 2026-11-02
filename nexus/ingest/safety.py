"""Guard rails for untrusted uploaded files.

Everything an analyst uploads is treated as hostile input: a "company report"
can just as well be a ZIP bomb, a path-traversal archive, an XML entity bomb or
a macro-laden Office file. This module holds the limits and the checks that
every parser goes through *before* it touches the content.

  * size limits per file, per upload request, and on extracted text,
  * ZIP-bomb protection: entry count, total uncompressed size, per-entry
    compression ratio, nesting depth,
  * path-traversal-proof archive member names (members are never written to
    disk under their own names; the name is only metadata),
  * display-name sanitising for anything shown back in the UI.

Nothing here executes content: Office macros are never run (the parsers only
read XML parts), and no member is ever extracted to an attacker-chosen path.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass

MB = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    """Tunable bounds. Defaults are generous for real reports, small for bombs."""

    max_file_bytes: int = 50 * MB          # one uploaded file
    max_request_bytes: int = 200 * MB      # all files in one upload
    max_zip_entries: int = 2000            # members in one archive
    max_zip_total_bytes: int = 500 * MB    # sum of uncompressed member sizes
    max_zip_member_bytes: int = 50 * MB    # one uncompressed member
    max_zip_ratio: int = 100               # uncompressed / compressed per member
    max_nesting: int = 2                   # zip-in-zip / email-attachment depth
    max_text_chars: int = 5_000_000        # extracted text kept per document
    max_chunks: int = 2000                 # items created per document
    chunk_chars: int = 4000                # target size of one item
    # Office/ODF containers are parsed fully in memory: much tighter bounds.
    max_office_expanded_bytes: int = 120 * MB
    max_office_part_bytes: int = 30 * MB
    max_table_cols: int = 200              # spreadsheet/CSV columns read per row


DEFAULT_LIMITS = Limits()


class UnsafeFileError(ValueError):
    """The file was refused by a safety check (reason is user-presentable)."""


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BAD_CHARS_RE = re.compile(r'[<>:"|?*\\/]')


def safe_display_name(name: str | None, default: str = "upload") -> str:
    """A filename safe to store as metadata and show in the UI.

    Keeps only the last path component (an archive member called
    ``../../etc/passwd`` becomes ``passwd``), strips control characters and
    characters Windows forbids, and caps the length. Never used as a path on
    disk — stored originals are named by their sha256.
    """
    raw = unicodedata.normalize("NFC", str(name or ""))
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    raw = _CTRL_RE.sub("", raw)
    raw = _BAD_CHARS_RE.sub("_", raw).strip().strip(".")
    if not raw:
        return default
    if len(raw) > 180:
        stem, dot, ext = raw.rpartition(".")
        raw = (stem[:170] + dot + ext[:9]) if dot and len(ext) <= 9 else raw[:180]
    return raw


def safe_member_path(name: str) -> str | None:
    """Normalise an archive member name to a relative POSIX path, or None.

    Rejects absolute paths, drive letters and anything that climbs out of the
    archive root. The result is only used as metadata (e.g. to resolve the
    internal links of an exported web site); nothing is written there.
    """
    if not name:
        return None
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    norm = posixpath.normpath(n)
    if norm in (".", "") or norm.startswith("../") or norm == "..":
        return None
    return norm


def check_zip(zf: zipfile.ZipFile, limits: Limits = DEFAULT_LIMITS) -> list[zipfile.ZipInfo]:
    """Validate an archive's central directory; return the safe file members.

    Raises :class:`UnsafeFileError` for a ZIP bomb (too many entries, too much
    uncompressed data, an absurd compression ratio) or encrypted members.
    Members with traversal names are silently dropped (they are never written
    anywhere, but they are also not worth ingesting).
    """
    infos = zf.infolist()
    if len(infos) > limits.max_zip_entries:
        raise UnsafeFileError(
            f"archive has {len(infos)} entries (limit {limits.max_zip_entries})"
        )
    total = 0
    safe: list[zipfile.ZipInfo] = []
    for info in infos:
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            raise UnsafeFileError("archive contains encrypted members")
        size = int(info.file_size)
        if size > limits.max_zip_member_bytes:
            raise UnsafeFileError(
                f"archive member is {size // MB} MB uncompressed "
                f"(limit {limits.max_zip_member_bytes // MB} MB)"
            )
        comp = max(int(info.compress_size), 1)
        if size > 1 * MB and size / comp > limits.max_zip_ratio:
            raise UnsafeFileError("archive member has a suspicious compression ratio")
        total += size
        if total > limits.max_zip_total_bytes:
            raise UnsafeFileError(
                f"archive expands beyond {limits.max_zip_total_bytes // MB} MB"
            )
        if safe_member_path(info.filename) is None:
            continue
        safe.append(info)
    return safe


def read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo,
                limits: Limits = DEFAULT_LIMITS) -> bytes:
    """Read one member with a hard cap, never trusting the declared size.

    The central directory can lie about ``file_size``; reading in bounded
    pieces makes the real decompressed size the thing that is limited.
    """
    cap = min(limits.max_zip_member_bytes, limits.max_file_bytes)
    out = bytearray()
    with zf.open(info) as fh:
        while True:
            piece = fh.read(1024 * 1024)
            if not piece:
                break
            out += piece
            if len(out) > cap:
                raise UnsafeFileError("archive member expands beyond the size limit")
    return bytes(out)


def guarded_ooxml(data: bytes, limits: Limits = DEFAULT_LIMITS) -> int:
    """Office Open XML / ODF files are ZIPs: bomb-check them before parsing.

    Office parsers load every part (and build an XML tree) in memory, so the
    bounds here are much tighter than for a plain archive. Returns the total
    declared expanded size so the caller can count it against its budget.
    Raises :class:`UnsafeFileError` for a bomb, ``ValueError`` for a merely
    corrupt container (the caller then falls back to best-effort text).
    """
    import io

    office = Limits(
        max_zip_entries=min(limits.max_zip_entries, 5000),
        max_zip_total_bytes=min(limits.max_zip_total_bytes, limits.max_office_expanded_bytes),
        max_zip_member_bytes=min(limits.max_zip_member_bytes, limits.max_office_part_bytes),
        max_zip_ratio=limits.max_zip_ratio,
    )
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = check_zip(zf, office)
            return sum(int(m.file_size) for m in members)
    except zipfile.BadZipFile as exc:
        raise ValueError("not a valid Office document (bad ZIP container)") from exc
