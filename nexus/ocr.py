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

import logging
from pathlib import Path

logger = logging.getLogger("nexus.ocr")

# Don't bother sending essentially-empty reads to storage / FTS.
_MIN_USEFUL_CHARS = 3
# Guard against a pathological screenshot producing a megabyte of garbage text.
_MAX_CHARS = 20_000


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
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.info("OCR skipped: pytesseract/Pillow not installed.")
        return None

    try:
        with Image.open(path) as img:
            raw = pytesseract.image_to_string(img)
    except Exception as exc:  # engine missing, bad image, etc.
        logger.warning("OCR failed for %s: %s", path.name, exc)
        return None

    text = _clean(raw)
    if len(text) < _MIN_USEFUL_CHARS:
        return None
    return text[:_MAX_CHARS]


def _clean(raw: str) -> str:
    """Collapse OCR whitespace noise into tidy, single-spaced lines."""
    lines = [" ".join(line.split()) for line in (raw or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()
