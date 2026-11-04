"""Optical character recognition — read text off images, gracefully.

Used to extract any text visible inside an evidence screenshot so that images
become full-text searchable alongside ordinary article text. This is a pure
enhancement: like every other capability in Nexus it degrades gracefully.

Two things must be present for OCR to run:
  * the ``pytesseract`` Python wrapper (a dependency), and
  * the Tesseract engine binary on the host (apt ``tesseract-ocr`` / a Windows
    install). The Docker image installs it; a bare local run may not have it.

When either is missing, every function here returns a clear "unavailable"
result instead of raising — callers simply store no OCR text and move on.
"""

from __future__ import annotations

import io
import logging
import math
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("nexus.ocr")

# Don't bother sending essentially-empty reads to storage / FTS.
_MIN_USEFUL_CHARS = 3
# Guard against a pathological screenshot producing a megabyte of garbage text.
_MAX_CHARS = 20_000

# Scanned PDFs: pages are rendered in memory (never written to disk).
_PDF_DPI = 200
_PDF_MAX_PIXELS = 25_000_000   # a huge page is scaled down, not rendered in full
_pdfium_lock = threading.Lock()  # PDFium is not thread-safe


def ocr_available() -> bool:
    """True only if both the Python wrapper and the Tesseract binary are present."""
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    try:
        # Raises (pytesseract.TesseractNotFoundError) if the binary is missing.
        pytesseract.get_tesseract_version()
    except Exception:
        return False
    return True


def pdf_ocr_available() -> bool:
    """True when scanned PDF pages can be read: Tesseract plus pypdfium2."""
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return False
    return ocr_available()


class PdfPageOcr:
    """Read pages of one PDF by rendering them and running OCR.

    Use as a context manager. Never raises while reading: a page that cannot
    be rendered or read gives ``None``. Opening a corrupt or password-protected
    file raises, which the caller treats as "no OCR for this file".
    """

    def __init__(self, data: bytes, timeout: int = 60) -> None:
        self.data = data
        self.timeout = timeout
        self._doc: Any = None

    def __enter__(self) -> PdfPageOcr:
        import pypdfium2

        with _pdfium_lock:
            self._doc = pypdfium2.PdfDocument(self.data)
        return self

    def __exit__(self, *exc: object) -> None:
        with _pdfium_lock:
            if self._doc is not None:
                self._doc.close()
                self._doc = None

    def _render(self, index: int) -> Any:
        """Page ``index`` (from 0) as a grayscale PIL image."""
        with _pdfium_lock:
            page = self._doc[index]
            try:
                width, height = page.get_size()
                scale = _PDF_DPI / 72
                area = max(width * height, 1.0)
                if area * scale * scale > _PDF_MAX_PIXELS:
                    scale = math.sqrt(_PDF_MAX_PIXELS / area)
                bitmap = page.render(scale=scale)
                try:
                    return bitmap.to_pil().convert("L")  # a copy, independent of the bitmap
                finally:
                    bitmap.close()
            finally:
                page.close()

    def read(self, index: int) -> str | None:
        """Text of page ``index`` (from 0), or ``None``. OCR runs unlocked."""
        try:
            image = self._render(index)
        except Exception as exc:
            logger.warning("Could not render PDF page %d: %s", index + 1, type(exc).__name__)
            return None
        try:
            return _read_image(image, self.timeout)
        finally:
            image.close()


def _read_image(image: Any, timeout: int = 0) -> str | None:
    """OCR one PIL image. ``None`` on any failure, a timeout or too little text."""
    try:
        import pytesseract

        raw = pytesseract.image_to_string(image, **({"timeout": timeout} if timeout else {}))
    except Exception as exc:  # engine missing, timed out, etc.
        logger.warning("OCR failed: %s", exc)
        return None
    text = _clean(raw)
    if len(text) < _MIN_USEFUL_CHARS:
        return None
    return text[:_MAX_CHARS]


def extract_text(image_path: str | Path) -> str | None:
    """Return the text read from ``image_path``, or ``None`` if unavailable/empty.

    Never raises: any failure (missing engine, unreadable image, decode error)
    is logged and turned into ``None`` so the surrounding capture flow is never
    interrupted by OCR.
    """
    path = Path(image_path)
    if not path.exists():
        return None
    try:
        from nexus.security.atrest import read_protected

        data = read_protected(path)
    except Exception as exc:  # unreadable, or encrypted while locked
        logger.warning("OCR skipped for %s: %s", path.name, exc)
        return None
    return extract_text_from_bytes(data)


def extract_text_from_bytes(data: bytes) -> str | None:
    """Like :func:`extract_text`, for image bytes held in memory (so an
    encrypted-at-rest screenshot is never written out as plaintext by us)."""
    try:
        import pytesseract  # noqa: F401
        from PIL import Image
    except ImportError:
        logger.info("OCR skipped: pytesseract/Pillow not installed.")
        return None

    try:
        with Image.open(io.BytesIO(data)) as img:
            return _read_image(img)
    except Exception as exc:  # bad image
        logger.warning("OCR failed: %s", exc)
        return None


def _clean(raw: str) -> str:
    """Collapse OCR whitespace noise into tidy, single-spaced lines."""
    lines = [" ".join(line.split()) for line in (raw or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()
