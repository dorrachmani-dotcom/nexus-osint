"""Format detection and text extraction for uploaded files.

``extract(data, filename)`` turns the bytes of one uploaded file into a list of
:class:`Section` objects — one per PDF page, Word heading section, spreadsheet
sheet, slide, web page, e-mail, archive member, record… — plus human-readable
notes about anything that was skipped. It never raises for bad *content*: an
unknown or broken file degrades to best-effort text (or no sections and a
note). Only a safety refusal (:class:`UnsafeFileError`) propagates.

Parser libraries are optional and imported lazily (graceful degradation):

  ===========  ==================  ====================================
  format       library             without it
  ===========  ==================  ====================================
  PDF          ``pypdf``           format disabled, clear note
  DOCX         ``python-docx``     format disabled, clear note
  XLSX         ``openpyxl``        format disabled, clear note
  PPTX         ``python-pptx``     format disabled, clear note
  XML / ODF    ``defusedxml``      basic tag-stripped text (no parser)
  images       Tesseract (OCR)     stored, but no text
  scanned PDF  Tesseract, pypdfium2  those pages are skipped, clear note
  ===========  ==================  ====================================

Everything else (CSV, HTML, MHTML, Markdown/text, JSON/NDJSON, EML, RTF, ZIP,
unknown binaries) uses only the standard library. Nothing here executes
content: Office macros are never run, scripts in HTML are dropped, XML is
parsed with entity expansion disabled, and archives go through the ZIP-bomb
guard in :mod:`nexus.ingest.safety`.
"""

from __future__ import annotations

import csv
import email
import email.policy
import io
import json
import logging
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any
from urllib.parse import unquote, urlsplit

from nexus.ingest.flatten import flatten_lines, flatten_to_text, guess_title
from nexus.ingest.html_text import extract_html
from nexus.ingest.safety import (
    DEFAULT_LIMITS,
    Limits,
    UnsafeFileError,
    check_zip,
    guarded_ooxml,
    read_member,
    safe_display_name,
    safe_member_path,
)

logger = logging.getLogger("nexus.ingest")


@dataclass
class Section:
    """One logical part of a document; becomes one or more stored items."""

    label: str            # e.g. "p.3", "Sheet Q1", "slide 4", a heading, a page title
    text: str
    meta: dict = field(default_factory=dict)


@dataclass
class Extracted:
    kind: str
    sections: list[Section] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- detection
_EXT_KIND = {
    "pdf": "pdf",
    "docx": "docx", "docm": "docx", "dotx": "docx",
    "xlsx": "xlsx", "xlsm": "xlsx", "xltx": "xlsx",
    "pptx": "pptx", "pptm": "pptx", "ppsx": "pptx",
    "odt": "odf", "ods": "odf", "odp": "odf",
    "csv": "csv", "tsv": "csv",
    "html": "html", "htm": "html", "xhtml": "html", "shtml": "html",
    "mht": "mhtml", "mhtml": "mhtml",
    "md": "text", "markdown": "text", "txt": "text", "text": "text", "log": "text",
    "rst": "text", "ini": "text", "cfg": "text", "conf": "text", "yaml": "text",
    "yml": "text", "toml": "text", "srt": "text", "vtt": "text",
    "json": "json", "geojson": "json",
    "jsonl": "ndjson", "ndjson": "ndjson",
    "xml": "xml", "rss": "xml", "atom": "xml", "kml": "xml", "gpx": "xml",
    "eml": "eml",
    "rtf": "rtf",
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "bmp": "image",
    "tif": "image", "tiff": "image", "webp": "image",
    "zip": "zip",
}

# Format families, for the UI's "what can I upload" panel.
FORMAT_LABELS = {
    "pdf": "PDF",
    "docx": "Word (.docx)",
    "xlsx": "Excel (.xlsx)",
    "pptx": "PowerPoint (.pptx)",
    "odf": "OpenDocument (.odt/.ods/.odp)",
    "csv": "CSV / TSV",
    "html": "Web pages (.html)",
    "mhtml": "Saved web pages (.mhtml)",
    "text": "Text / Markdown",
    "json": "JSON / NDJSON",
    "xml": "XML / RSS / Atom",
    "eml": "E-mail (.eml)",
    "rtf": "Rich text (.rtf)",
    "image": "Images (OCR)",
    "zip": "ZIP archives / site exports",
}


def _ext(filename: str) -> str:
    base = (filename or "").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def detect_kind(filename: str, data: bytes) -> str:
    """Pick a parser from the extension, falling back to magic bytes."""
    kind = _EXT_KIND.get(_ext(filename))
    if kind:
        return kind
    head = data[:2048]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist()[:500])
        except zipfile.BadZipFile:
            return "unknown"
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        if "content.xml" in names and "mimetype" in names:
            return "odf"
        return "zip"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head[:8] in (b"\x89PNG\r\n\x1a\n",) or head[:3] == b"\xff\xd8\xff" or head[:6] in (
        b"GIF87a", b"GIF89a"
    ):
        return "image"
    low = head.lstrip().lower()
    if low.startswith((b"<!doctype html", b"<html")):
        return "html"
    if low.startswith(b"<?xml") or low.startswith(b"<rss") or low.startswith(b"<feed"):
        return "xml"
    if low.startswith((b"{", b"[")):
        return "json"
    if b"mime-version:" in low[:600] and b"multipart/related" in low:
        return "mhtml"
    if re.match(rb"(?i)(received|from|return-path|message-id|date|subject):", head):
        return "eml"
    return "unknown"


# -------------------------------------------------------- library support
def _has(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


_LIB_FOR_KIND = {
    "pdf": ("pypdf", "pypdf"),
    "docx": ("docx", "python-docx"),
    "xlsx": ("openpyxl", "openpyxl"),
    "pptx": ("pptx", "python-pptx"),
}


def format_support() -> list[dict]:
    """Per-format availability for the UI: ``{kind, label, ok, note}``."""
    from nexus.ocr import ocr_available, pdf_ocr_available

    out = []
    for kind, label in FORMAT_LABELS.items():
        ok, note = True, ""
        lib = _LIB_FOR_KIND.get(kind)
        if lib and not _has(lib[0]):
            ok, note = False, f"install the optional '{lib[1]}' package to enable"
        elif kind in ("xml", "odf") and not _has("defusedxml"):
            note = "basic text only — install 'defusedxml' for full parsing"
        elif kind == "image" and not ocr_available():
            ok, note = False, "needs the Tesseract OCR engine (images are stored without text)"
        elif kind == "pdf" and not pdf_ocr_available():
            note = "scanned pages need the Tesseract OCR engine and 'pypdfium2'"
        out.append({"kind": kind, "label": label, "ok": ok, "note": note})
    return out


# ------------------------------------------------------------ text helpers
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PRINTABLE_RUN_RE = re.compile(r"[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd]{6,}")


def decode_text(data: bytes) -> str:
    """Decode bytes of unknown encoding into text (BOM, UTF-8, then cp1252)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def looks_binary(data: bytes) -> bool:
    sample = data[:8192]
    if not sample:
        return False
    if b"\x00" in sample and not sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    text = sample.decode("utf-8", errors="replace")
    bad = len(_CTRL_RE.findall(text)) + text.count("\ufffd")
    return bad / max(len(text), 1) > 0.10


def strings_text(data: bytes, min_chars: int = 200) -> str:
    """Printable runs from a binary blob (like ``strings``); '' if too little."""
    text = data.decode("utf-8", errors="replace")
    runs = [r.strip() for r in _PRINTABLE_RUN_RE.findall(text)]
    runs = [r for r in runs if sum(c.isalpha() for c in r) >= 4]
    joined = "\n".join(runs)
    return joined if len(joined) >= min_chars else ""


def _clean(text: str) -> str:
    text = _CTRL_RE.sub("", text or "")
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# ----------------------------------------------------------- the extractor
class _Ctx:
    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.notes: list[str] = []
        self.chars = 0
        self.truncated = False
        self.bytes_read = 0  # decompressed bytes across ALL (nested) archives

    def account(self, n: int) -> None:
        self.bytes_read += n
        if self.bytes_read > self.limits.max_zip_total_bytes:
            raise UnsafeFileError("archive contents expand beyond the size limit")

    def note(self, msg: str) -> None:
        if msg not in self.notes and len(self.notes) < 50:
            self.notes.append(msg)

    def room(self) -> bool:
        return self.chars < self.limits.max_text_chars

    def add(self, out: list[Section], label: str, text: str, meta: dict | None = None) -> None:
        text = _clean(text)
        if not text:
            return
        left = self.limits.max_text_chars - self.chars
        if left <= 0:
            if not self.truncated:
                self.truncated = True
                self.note("text limit reached — the rest of the document was not stored")
            return
        if len(text) > left:
            text = text[:left]
            self.truncated = True
            self.note("text limit reached — the rest of the document was not stored")
        self.chars += len(text)
        out.append(Section(label=label, text=text, meta=dict(meta or {})))


def extract(data: bytes, filename: str, limits: Limits = DEFAULT_LIMITS) -> Extracted:
    """Extract sections from one file. See the module docstring."""
    ctx = _Ctx(limits)
    kind = detect_kind(filename, data)
    sections = _dispatch(kind, data, filename, ctx, depth=0)
    return Extracted(kind=kind, sections=sections, notes=ctx.notes)


def _dispatch(kind: str, data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    handler = _HANDLERS.get(kind, _x_unknown)
    try:
        return handler(data, filename, ctx, depth)
    except UnsafeFileError:
        raise
    except Exception as exc:
        # A parser choked on malformed content: degrade to best-effort text.
        logger.info("Parser for %s failed on %s: %s", kind, safe_display_name(filename), exc)
        ctx.note(f"{FORMAT_LABELS.get(kind, kind)} parser could not read "
                 f"'{safe_display_name(filename)}'; kept best-effort text instead")
        return _x_unknown(data, filename, ctx, depth)


# --- PDF ------------------------------------------------------------------
_MAX_PDF_PAGES = 3000
_MIN_PAGE_TEXT = 16  # less than this (a stray page number): treat the page as scanned


class _PdfOcr:
    """OCR for the scanned pages of one PDF, opened on first use, within limits."""

    def __init__(self, data: bytes, ctx: _Ctx) -> None:
        self.data, self.ctx = data, ctx
        self.reader: Any = None
        self.pages = 0
        self.started = 0.0
        self.closed = False  # off for this file (unavailable, unopenable or spent)

    def _open(self) -> bool:
        from nexus.ocr import PdfPageOcr, pdf_ocr_available

        if not pdf_ocr_available():
            self.closed = True
            return False
        try:
            self.reader = PdfPageOcr(self.data, self.ctx.limits.ocr_page_seconds)
            self.reader.__enter__()
        except Exception as exc:
            logger.warning("PDF could not be opened for OCR: %s", type(exc).__name__)
            self.reader, self.closed = None, True
            self.ctx.note("scanned pages could not be read: the PDF did not open for OCR")
            return False
        self.started = time.monotonic()
        return True

    def read(self, page: int) -> str | None:
        """Text of ``page`` (from 1), or ``None`` when OCR is off or found none."""
        if self.closed or (self.reader is None and not self._open()):
            return None
        limits = self.ctx.limits
        if self.pages >= limits.max_ocr_pages:
            self.ctx.note(f"OCR stopped after {self.pages} scanned pages (the per-document limit)")
            self.closed = True
            return None
        if time.monotonic() - self.started > limits.max_ocr_seconds:
            self.ctx.note(f"OCR stopped after {self.pages} scanned pages (the time limit)")
            self.closed = True
            return None
        self.pages += 1
        return self.reader.read(page - 1)

    def close(self) -> None:
        if self.reader is not None:
            self.reader.__exit__(None, None, None)
            self.reader = None


def _x_pdf(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    try:
        from pypdf import PdfReader
    except ImportError:
        ctx.note("PDF support is off: install the optional 'pypdf' package")
        return []
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                ctx.note("PDF is password-protected; it was stored but not read")
                return []
        except Exception:
            ctx.note("PDF is encrypted; it was stored but not read")
            return []
    out: list[Section] = []
    pages = reader.pages
    ocr = _PdfOcr(data, ctx)
    scanned_left = 0
    try:
        for i, page in enumerate(pages, 1):
            if i > _MAX_PDF_PAGES or not ctx.room():
                ctx.note(f"only the first {i - 1} PDF pages were read")
                break
            try:
                text = page.extract_text() or ""
            except Exception:
                logger.debug("pypdf could not read page %d", i, exc_info=True)
                text = ""
            if len(text.strip()) < _MIN_PAGE_TEXT:
                # No real text layer: a scanned page.
                read = ocr.read(i)
                if read:
                    ctx.add(out, f"p.{i} (OCR)", read, {"page": i, "ocr": True})
                    continue
                if not text.strip():
                    scanned_left += 1
            ctx.add(out, f"p.{i}", text, {"page": i})
    finally:
        ocr.close()
    if scanned_left:
        from nexus.ocr import pdf_ocr_available

        if not pdf_ocr_available():
            ctx.note(f"{scanned_left} page(s) have no text layer (scanned?): reading them "
                     "needs the Tesseract OCR engine and the 'pypdfium2' package")
        else:
            ctx.note(f"{scanned_left} scanned page(s) gave no readable text")
    return out


# --- DOCX -----------------------------------------------------------------
def _x_docx(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        ctx.note("Word support is off: install the optional 'python-docx' package")
        return []
    ctx.account(guarded_ooxml(data, ctx.limits))
    document = docx.Document(io.BytesIO(data))
    out: list[Section] = []
    heading: str | None = None
    buf: list[str] = []
    index = 0

    def flush() -> None:
        nonlocal index
        if buf or heading:
            index += 1
            ctx.add(out, heading or f"part {index}", "\n".join(buf),
                    {"section": index, "heading": heading})

    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, document)
            text = para.text.strip()
            style = (para.style.name if para.style is not None else "") or ""
            if text and (style.startswith("Heading") or style == "Title"):
                flush()
                heading, buf = text[:200], []
            elif text:
                buf.append(text)
        elif tag == "tbl":
            table = Table(child, document)
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                # Merged cells repeat; collapse consecutive duplicates.
                dedup = [c for j, c in enumerate(cells) if c and (j == 0 or c != cells[j - 1])]
                if dedup:
                    buf.append(" | ".join(dedup))
        if not ctx.room():
            break
    flush()
    return out


# --- XLSX / CSV -----------------------------------------------------------
_MAX_ROWS = 100_000


def _rows_to_lines(rows, max_cols: int = 200) -> list[str]:
    """Render table rows as ``Header: value; …`` lines (header = first row)."""
    lines: list[str] = []
    header: list[str] | None = None
    for n, row in enumerate(rows):
        if n >= _MAX_ROWS:
            lines.append(f"[… stopped after {_MAX_ROWS} rows]")
            break
        cells = ["" if v is None else str(v).strip() for v in list(row)[:max_cols]]
        if not any(cells):
            continue
        if header is None:
            header = cells
            lines.append(" | ".join(c for c in cells if c))
            continue
        pairs = []
        for j, val in enumerate(cells):
            if not val:
                continue
            key = header[j] if j < len(header) and header[j] else f"col{j + 1}"
            pairs.append(f"{key}: {val}")
        lines.append("; ".join(pairs))
    return lines


def _x_xlsx(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    try:
        import openpyxl
    except ImportError:
        ctx.note("Excel support is off: install the optional 'openpyxl' package")
        return []
    ctx.account(guarded_ooxml(data, ctx.limits))
    # data_only: cached values, never formulas; openpyxl never runs macros.
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out: list[Section] = []
    try:
        for ws in wb.worksheets:
            if not ctx.room():
                break
            lines = _rows_to_lines(
                ws.iter_rows(values_only=True, max_col=ctx.limits.max_table_cols),
                ctx.limits.max_table_cols,
            )
            ctx.add(out, f"sheet {ws.title}", "\n".join(lines), {"sheet": ws.title})
    finally:
        wb.close()
    return out


def _x_csv(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    text = decode_text(data)
    sample = text[:8192]
    delimiter = "\t" if _ext(filename) == "tsv" else ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        logger.debug("CSV dialect not detected; using '%s'", delimiter)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    lines = _rows_to_lines(reader, ctx.limits.max_table_cols)
    out: list[Section] = []
    ctx.add(out, "table", "\n".join(lines), {"rows": len(lines)})
    return out


# --- PPTX -----------------------------------------------------------------
def _x_pptx(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    try:
        from pptx import Presentation
    except ImportError:
        ctx.note("PowerPoint support is off: install the optional 'python-pptx' package")
        return []
    ctx.account(guarded_ooxml(data, ctx.limits))
    prs = Presentation(io.BytesIO(data))
    out: list[Section] = []
    for i, slide in enumerate(prs.slides, 1):
        if not ctx.room():
            break
        title = None
        parts: list[str] = []
        try:
            if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                title = slide.shapes.title.text_frame.text.strip() or None
        except Exception:
            title = None
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t and t != title:
                    parts.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
        try:
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    parts.append("Speaker notes: " + notes)
        except Exception:
            logger.debug("could not read speaker notes of slide %d", i, exc_info=True)
        label = f"slide {i}" + (f": {title[:120]}" if title else "")
        ctx.add(out, label, "\n".join(([title] if title else []) + parts), {"slide": i})
    return out


# --- XML / ODF ------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]{0,2000}>")


def _local(tag) -> str:
    return str(tag).rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _xml_root(data: bytes):
    """Parse XML with entity expansion and external references disabled."""
    from defusedxml import ElementTree as DET

    return DET.fromstring(data, forbid_dtd=False, forbid_entities=True,
                          forbid_external=True)


def _elem_record(el) -> dict | str:
    """A small dict view of an element (children + attributes) for flattening."""
    kids = list(el)
    if not kids:
        return (el.text or "").strip()
    rec: dict = {}
    for k, v in el.attrib.items():
        rec[f"@{_local(k)}"] = v
    for child in kids:
        key = _local(child.tag)
        val = _elem_record(child)
        if key in rec:
            if not isinstance(rec[key], list):
                rec[key] = [rec[key]]
            rec[key].append(val)
        else:
            rec[key] = val
    return rec


def _x_xml(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    out: list[Section] = []
    if not _has("defusedxml"):
        ctx.note("XML read as plain text: install the optional 'defusedxml' package "
                 "for structured parsing")
        ctx.add(out, "document", _TAG_RE.sub(" ", decode_text(data)))
        return out
    root = _xml_root(data)
    # Feeds: one section per <item>/<entry>.
    records = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    if not records:
        # Generic: repeated children of the root (or of its single child) are records.
        container = root
        if len(list(root)) == 1:
            container = next(iter(root))
        kids = list(container)
        tags = [_local(k.tag) for k in kids]
        common = max(set(tags), key=tags.count) if tags else None
        if common and tags.count(common) >= 3:
            records = [k for k in kids if _local(k.tag) == common]
    if records:
        for n, rec in enumerate(records, 1):
            if not ctx.room():
                break
            view = _elem_record(rec)
            title = guess_title(view) if isinstance(view, dict) else None
            text = flatten_to_text(view) if isinstance(view, dict) else str(view)
            ctx.add(out, title or f"record {n}", text, {"record": n})
        return out
    view = _elem_record(root)
    text = flatten_to_text({_local(root.tag): view})
    ctx.add(out, _local(root.tag) or "document", text)
    return out


def _x_odf(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    ctx.account(guarded_ooxml(data, ctx.limits))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        content = read_member(zf, zf.getinfo("content.xml"), ctx.limits)
    out: list[Section] = []
    if not _has("defusedxml"):
        ctx.note("OpenDocument read as plain text: install 'defusedxml' for structure")
        ctx.add(out, "document", _TAG_RE.sub(" ", decode_text(content)))
        return out
    root = _xml_root(content)
    label = "document"
    buf: list[str] = []
    n = 0

    def flush() -> None:
        nonlocal n
        if buf:
            n += 1
            ctx.add(out, label, "\n".join(buf), {"section": n})

    for el in root.iter():
        tag = _local(el.tag)
        if tag in ("h", "table", "page"):
            name = el.get("{urn:oasis:names:tc:opendocument:xmlns:table:1.0}name") or \
                el.get("{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}name")
            heading = "".join(el.itertext()).strip() if tag == "h" else None
            flush()
            buf = []
            label = (heading or (f"{'sheet' if tag == 'table' else 'slide'} {name}" if name
                                 else f"part {n + 1}"))[:200]
            if heading:
                continue
        elif tag == "p":
            t = "".join(el.itertext()).strip()
            if t:
                buf.append(t)
    flush()
    return out


# --- HTML / MHTML ---------------------------------------------------------
def _html_sections(markup: str, ctx: _Ctx, out: list[Section], base_label: str,
                   meta: dict) -> None:
    doc = extract_html(markup)
    title = doc.title or base_label
    page_meta = dict(meta)
    page_meta["page_title"] = doc.title
    if doc.links:
        page_meta["links"] = [{"href": h, "text": t} for h, t in doc.links[:100]]
    # Short pages stay whole; long ones split at their headings.
    if len(doc.text) <= ctx.limits.chunk_chars or len(doc.sections) <= 1:
        ctx.add(out, title, doc.text, page_meta)
        return
    for s in doc.sections:
        label = f"{title} — {s.heading}" if s.heading else title
        ctx.add(out, label[:240], (s.heading + "\n" if s.heading else "") + s.text,
                dict(page_meta, heading=s.heading))


def _x_html(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    out: list[Section] = []
    _html_sections(decode_text(data), ctx, out, safe_display_name(filename), {})
    return out


def _part_text(part) -> str:
    try:
        payload = part.get_payload(decode=True) or b""
    except Exception:
        return ""
    charset = part.get_content_charset() or ""
    if charset:
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            pass
    return decode_text(payload)


def _x_mhtml(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    msg = email.message_from_bytes(data, policy=email.policy.compat32)
    out: list[Section] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain"):
            continue
        location = part.get("Content-Location") or ""
        meta = {"url": location} if location else {}
        label = location.rsplit("/", 1)[-1] or safe_display_name(filename)
        if ctype == "text/html":
            _html_sections(_part_text(part), ctx, out, label, meta)
        else:
            ctx.add(out, label, _part_text(part), meta)
    return out


# --- Text / Markdown / RTF ------------------------------------------------
_MD_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _x_text(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    text = decode_text(data)
    out: list[Section] = []
    matches = list(_MD_HEADING_RE.finditer(text))
    if len(text) <= ctx.limits.chunk_chars or not matches:
        ctx.add(out, "text", text)
        return out
    if matches[0].start() > 0:
        ctx.add(out, "intro", text[: matches[0].start()])
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        ctx.add(out, m.group(2)[:200], text[m.start():end], {"heading": m.group(2)[:200]})
    return out


_RTF_GROUP_RE = re.compile(r"\{\\\*[^{}]*\}")
_RTF_CTRL_RE = re.compile(r"\\[a-zA-Z]+-?\d* ?")
_RTF_HEX_RE = re.compile(r"\\'([0-9a-fA-F]{2})")


def _x_rtf(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    text = data.decode("latin-1", errors="replace")
    for _ in range(3):
        text = _RTF_GROUP_RE.sub("", text)
    text = text.replace("\\par", "\n").replace("\\line", "\n")
    text = _RTF_HEX_RE.sub(lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"),
                           text)
    text = _RTF_CTRL_RE.sub("", text).replace("{", "").replace("}", "")
    out: list[Section] = []
    ctx.add(out, "document", text)
    return out


# --- JSON / NDJSON --------------------------------------------------------
def _records_sections(records: list, ctx: _Ctx, out: list[Section]) -> None:
    for n, rec in enumerate(records, 1):
        if not ctx.room() or n > 50_000:
            break
        title = guess_title(rec)
        ctx.add(out, title or f"record {n}", flatten_to_text(rec), {"record": n})


def _x_json(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    obj = json.loads(decode_text(data))
    out: list[Section] = []
    # A top-level list, or the first big list inside an object, holds records.
    records = obj if isinstance(obj, list) else None
    if records is None and isinstance(obj, dict):
        lists = [v for v in obj.values() if isinstance(v, list) and len(v) >= 2]
        records = max(lists, key=len) if lists else None
    if records and all(isinstance(r, dict) for r in records[:20]):
        _records_sections(records, ctx, out)
    else:
        ctx.add(out, guess_title(obj) or "document", "\n".join(flatten_lines(obj, max_lines=20_000)))
    return out


def _x_ndjson(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    records = []
    bad = 0
    for line in decode_text(data).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            bad += 1
    if bad:
        ctx.note(f"{bad} NDJSON line(s) were not valid JSON and were skipped")
    out: list[Section] = []
    _records_sections(records, ctx, out)
    return out


# --- E-mail ---------------------------------------------------------------
def _x_eml(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    out: list[Section] = []
    headers = []
    for h in ("From", "To", "Cc", "Date", "Subject"):
        try:
            v = msg.get(h)
        except Exception:
            v = None
        if v:
            headers.append(f"{h}: {v}")
    subject = str(msg.get("Subject") or "").strip() or "e-mail"
    body = ""
    if isinstance(msg, EmailMessage):
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = _part_text(part)
            if part.get_content_type() == "text/html":
                content = extract_html(content).text
            body = content
    meta = {"email_subject": subject[:200], "email_from": str(msg.get("From") or "")[:200],
            "email_date": str(msg.get("Date") or "")[:80]}
    ctx.add(out, subject[:200], "\n".join(headers) + "\n\n" + body, meta)
    if isinstance(msg, EmailMessage):
        for att in msg.iter_attachments():
            name = safe_display_name(att.get_filename() or "attachment")
            if depth + 1 > ctx.limits.max_nesting:
                ctx.note(f"attachment '{name}' skipped (nested too deep)")
                continue
            try:
                payload = att.get_payload(decode=True) or b""
            except Exception:
                logger.debug("could not decode attachment %s", name, exc_info=True)
                continue
            if not isinstance(payload, bytes) or not payload:
                continue
            if len(payload) > ctx.limits.max_file_bytes:
                ctx.note(f"attachment '{name}' skipped (too large)")
                continue
            sub_kind = detect_kind(name, payload)
            for s in _dispatch(sub_kind, payload, name, ctx, depth + 1):
                s.meta.setdefault("attachment", name)
                out.append(Section(f"{name} — {s.label}", s.text, s.meta))
    return out


# --- Images ---------------------------------------------------------------
def _x_image(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    from nexus.ocr import extract_text_from_bytes, ocr_available

    if not ocr_available():
        ctx.note("image stored without text: OCR needs the Tesseract engine")
        return []
    text = extract_text_from_bytes(data) or ""
    out: list[Section] = []
    if not text:
        ctx.note(f"no text found in image '{safe_display_name(filename)}'")
    ctx.add(out, "image text (OCR)", text, {"ocr": True})
    return out


# --- ZIP (incl. exported web sites) ---------------------------------------
_ASSET_EXT = {"css", "js", "mjs", "map", "woff", "woff2", "ttf", "otf", "eot", "ico",
              "svg", "scss", "less", "lock", "pyc", "class", "exe", "dll", "so",
              "dylib", "bin"}
_PAGE_KINDS = {"html", "mhtml"}


def _resolve_link(member: str, href: str) -> str | None:
    """Map a relative link inside an exported site to another member path."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or not parts.path:
        return None
    target = posixpath.normpath(posixpath.join(posixpath.dirname(member), unquote(parts.path)))
    return safe_member_path(target)


def _x_zip(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    if depth > ctx.limits.max_nesting:
        ctx.note(f"archive '{safe_display_name(filename)}' skipped (nested too deep)")
        return []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsafeFileError("not a valid ZIP archive") from exc
    out: list[Section] = []
    with zf:
        members = check_zip(zf, ctx.limits)
        is_vault = any(
            any(part == ".obsidian" for part in m.filename.split("/")[:-1]) for m in members)
        members = [m for m in members
                   if not m.filename.startswith("__MACOSX/")
                   and not posixpath.basename(m.filename).startswith(".")
                   and not (is_vault and any(part.startswith(".")
                                             for part in m.filename.split("/")[:-1]))]
        kinds = {m.filename: detect_kind(m.filename, b"") for m in members}
        is_site = sum(1 for k in kinds.values() if k in _PAGE_KINDS) >= 2
        page_paths = {safe_member_path(m.filename) for m in members
                      if kinds[m.filename] in _PAGE_KINDS}
        skipped = 0
        for info in sorted(members, key=lambda m: m.filename):
            if not ctx.room():
                break
            path = safe_member_path(info.filename) or info.filename
            ext = _ext(path)
            kind = kinds[info.filename]
            if ext in _ASSET_EXT or (is_site and kind == "image"):
                skipped += 1
                continue
            payload = read_member(zf, info, ctx.limits)
            ctx.account(len(payload))
            if kind == "unknown":
                kind = detect_kind(path, payload)
            if is_vault and _ext(path) in ("md", "markdown"):
                out.extend(_vault_note_sections(path, payload, ctx))
                continue
            sub = _dispatch(kind, payload, path, ctx, depth + 1)
            for s in sub:
                s.meta.setdefault("member", path)
                if is_site and kind in _PAGE_KINDS:
                    links = s.meta.pop("links", None) or []
                    internal = []
                    for link in links:
                        target = _resolve_link(path, link.get("href", ""))
                        if target and target in page_paths and target not in internal:
                            internal.append(target)
                    s.meta["site_links"] = internal[:50]
                    s.meta["external_links"] = sum(
                        1 for link in links if urlsplit(link.get("href", "")).scheme
                        in ("http", "https"))
                    label = s.label
                else:
                    label = f"{path} — {s.label}" if s.label not in (path,) else path
                out.append(Section(label[:240], s.text, s.meta))
        if skipped:
            ctx.note(f"{skipped} asset file(s) in the archive (styles, scripts, "
                     "fonts, site images) were skipped")
        if is_site:
            ctx.note(f"read as an exported web site ({len(page_paths)} pages)")
        if is_vault:
            ctx.note("read as an Obsidian vault (frontmatter, tags and [[links]] kept)")
    return out


def _vault_note_sections(path: str, payload: bytes, ctx: _Ctx) -> list[Section]:
    """One Obsidian note from a zipped vault: frontmatter, tags and wikilinks
    become metadata, long notes split at headings."""
    from nexus.ingest.obsidian_notes import parse_note

    title = posixpath.basename(path).rsplit(".", 1)[0]
    note = parse_note(decode_text(payload), title)
    from nexus.sources.obsidian import is_nexus_written

    if is_nexus_written(note.frontmatter):
        ctx.note(f"skipped {path}: a note Nexus wrote itself (already in Nexus)")
        return []
    meta = {"member": path, "note": note.title, "tags": note.tags[:50],
            "links": note.links[:50], "embeds": note.embeds[:50]}
    out: list[Section] = []
    parts = note.sections or []
    if note.links:
        parts = [*parts, ("links", "Linked notes: " + ", ".join(note.links))]
    for label, text in parts:
        if not ctx.room():
            break
        ctx.add(out, f"{note.title} — {label}" if label else note.title, text, dict(meta))
    return out


# --- Unknown --------------------------------------------------------------
def _x_unknown(data: bytes, filename: str, ctx: _Ctx, depth: int) -> list[Section]:
    out: list[Section] = []
    if not data:
        return out
    if not looks_binary(data):
        ctx.add(out, "text", decode_text(data))
        return out
    text = strings_text(data)
    if text:
        ctx.note(f"'{safe_display_name(filename)}' is an unrecognised binary format; "
                 "kept the readable text found inside it")
        ctx.add(out, "text (best effort)", text, {"best_effort": True})
    else:
        ctx.note(f"'{safe_display_name(filename)}' has no readable text; "
                 "the file was stored but nothing was extracted")
    return out


_HANDLERS = {
    "pdf": _x_pdf,
    "docx": _x_docx,
    "xlsx": _x_xlsx,
    "csv": _x_csv,
    "pptx": _x_pptx,
    "odf": _x_odf,
    "html": _x_html,
    "mhtml": _x_mhtml,
    "text": _x_text,
    "rtf": _x_rtf,
    "json": _x_json,
    "ndjson": _x_ndjson,
    "xml": _x_xml,
    "eml": _x_eml,
    "image": _x_image,
    "zip": _x_zip,
    "unknown": _x_unknown,
}
