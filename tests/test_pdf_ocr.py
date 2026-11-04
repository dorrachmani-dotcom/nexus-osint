"""OCR for scanned PDF pages: rendering, per-document limits, degradation (F2).

Scanned PDFs are real image-only files made with Pillow. The Tesseract engine
is mocked so the suite runs without it; one test uses it when it is installed.
"""

from __future__ import annotations

import io
import sys

import pytest

pytest.importorskip("pypdf")
pytest.importorskip("pypdfium2")
pytesseract = pytest.importorskip("pytesseract")
Image = pytest.importorskip("PIL.Image")

from nexus import ocr as O  # noqa: E402
from nexus.ingest.extract import _MIN_PAGE_TEXT, extract, format_support  # noqa: E402
from nexus.ingest.safety import Limits  # noqa: E402


def _scanned(pages: int = 1, resolution: float = 100.0) -> bytes:
    images = [Image.new("RGB", (200, 280), "white") for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=resolution)
    return buf.getvalue()


def _with_text(text: str) -> bytes:
    canvas_mod = pytest.importorskip("reportlab.pdfgen.canvas")
    buf = io.BytesIO()
    c = canvas_mod.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def engine(monkeypatch):
    """A fake Tesseract: records what it was given, reads 'scanned text N'."""
    seen: list = []

    def read(image, timeout=0, **_kw):
        seen.append((image.mode, image.size, timeout))
        return f"scanned text {len(seen)}\n"

    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.0")
    monkeypatch.setattr(pytesseract, "image_to_string", read)
    return seen


def test_scanned_pages_are_read_with_page_labels(engine):
    ex = extract(_scanned(2), "scan.pdf")
    assert [s.label for s in ex.sections] == ["p.1 (OCR)", "p.2 (OCR)"]
    assert [s.text for s in ex.sections] == ["scanned text 1", "scanned text 2"]
    assert ex.sections[1].meta == {"page": 2, "ocr": True}
    mode, size, timeout = engine[0]
    assert mode == "L" and timeout == Limits().ocr_page_seconds
    assert size[0] > 200  # rendered at OCR resolution, not the image's own size


def test_only_scanned_pages_are_ocred(engine):
    from pypdf import PdfReader, PdfWriter

    words = "Quarterly findings summary for the northern district"
    writer = PdfWriter()
    for blob in (_with_text(words), _scanned(1)):
        writer.add_page(PdfReader(io.BytesIO(blob)).pages[0])
    buf = io.BytesIO()
    writer.write(buf)

    ex = extract(buf.getvalue(), "mixed.pdf")
    assert [s.label for s in ex.sections] == ["p.1", "p.2 (OCR)"]
    assert words in ex.sections[0].text and len(engine) == 1


def test_stray_page_number_counts_as_scanned(engine):
    assert len("12") < _MIN_PAGE_TEXT
    ex = extract(_with_text("12"), "numbered.pdf")
    assert [s.label for s in ex.sections] == ["p.1 (OCR)"]


def test_short_text_is_kept_when_ocr_finds_nothing(engine, monkeypatch):
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *_a, **_k: "  ")
    ex = extract(_with_text("12"), "numbered.pdf")
    assert [(s.label, s.text) for s in ex.sections] == [("p.1", "12")]
    assert not ex.notes


def test_no_engine_degrades_with_a_clear_note(monkeypatch):
    def missing():
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", missing)
    ex = extract(_scanned(2), "scan.pdf")
    assert ex.sections == []
    assert any("2 page(s) have no text layer" in n and "Tesseract" in n for n in ex.notes)
    pdf = next(f for f in format_support() if f["kind"] == "pdf")
    assert pdf["ok"] and "Tesseract" in pdf["note"]


def test_no_pdfium_degrades(engine, monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    assert O.ocr_available() and not O.pdf_ocr_available()
    ex = extract(_scanned(1), "scan.pdf")
    assert ex.sections == [] and engine == []
    assert any("pypdfium2" in n for n in ex.notes)


def test_page_limit(engine):
    ex = extract(_scanned(3), "scan.pdf", Limits(max_ocr_pages=2))
    assert len(ex.sections) == 2 and len(engine) == 2
    assert any("OCR stopped after 2" in n for n in ex.notes)


def test_time_limit(engine):
    ex = extract(_scanned(2), "scan.pdf", Limits(max_ocr_seconds=-1))
    assert ex.sections == [] and engine == []
    assert any("time limit" in n for n in ex.notes)


def test_page_timeout_skips_that_page(engine, monkeypatch):
    calls: list[int] = []

    def flaky(image, timeout=0, **_kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Tesseract process timeout")
        return "second page text"

    monkeypatch.setattr(pytesseract, "image_to_string", flaky)
    ex = extract(_scanned(2), "scan.pdf")
    assert [s.label for s in ex.sections] == ["p.2 (OCR)"]
    assert any("1 scanned page(s) gave no readable text" in n for n in ex.notes)


def test_pdf_that_will_not_open_for_ocr(engine, monkeypatch):
    def refuse(self):
        raise ValueError("cannot open")

    monkeypatch.setattr(O.PdfPageOcr, "__enter__", refuse)
    ex = extract(_scanned(2), "scan.pdf")
    assert ex.sections == [] and engine == []
    assert any("did not open for OCR" in n for n in ex.notes)


def test_huge_page_is_scaled_down(engine):
    extract(_scanned(1, resolution=1.0), "poster.pdf")  # 200 x 280 inches
    width, height = engine[0][1]
    assert width * height <= O._PDF_MAX_PIXELS * 1.01


def test_connector_attachments_are_parsed_before_any_write(temp_db, monkeypatch):
    """OCR can take minutes: it must not hold SQLite's write lock."""
    from nexus.ingest import connector as C, extract as X

    state: list[bool] = []
    real = X.extract

    with temp_db() as conn:
        def spy(data, name, limits=None):
            state.append(conn.in_transaction)
            return real(data, name, limits or Limits())

        monkeypatch.setattr(X, "extract", spy)
        doc = C.ConnectorDoc(key="a.md", title="A", text="Body text",
                             attachments=[("scan.txt", b"plain attachment text")])
        conn.commit()
        res = C.sync_document(conn, kind="obsidian", source_id=1, source_name="Field",
                              track="obsidian", doc=doc, target=C.Target())
    assert res["change"] == "new" and state == [False]


def test_real_engine_reads_a_page():
    if not O.pdf_ocr_available():
        pytest.skip("Tesseract is not installed")
    draw_mod = pytest.importorskip("PIL.ImageDraw")
    font_mod = pytest.importorskip("PIL.ImageFont")
    img = Image.new("RGB", (1200, 300), "white")
    draw_mod.Draw(img).text((40, 100), "NIGHTJAR REPORT", fill="black",
                            font=font_mod.load_default(size=80))
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150.0)
    ex = extract(buf.getvalue(), "real.pdf")
    assert "NIGHTJAR" in ex.sections[0].text.upper()
