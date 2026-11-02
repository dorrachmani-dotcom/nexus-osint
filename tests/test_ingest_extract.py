"""Format extraction for uploaded files.

Every fixture is generated in the test itself (no binary files in the repo, no
network). Optional parser libraries are skipped cleanly when absent.
"""

from __future__ import annotations

import io
import json
import zipfile
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from nexus.ingest.extract import detect_kind, extract, format_support
from nexus.ingest.html_text import extract_html
from nexus.ingest.safety import (
    Limits,
    UnsafeFileError,
    safe_display_name,
    safe_member_path,
)

# Non-Latin sample text built from code points (no literal non-ASCII in source).
HEB = "".join(chr(c) for c in (0x05E9, 0x05DC, 0x05D5, 0x05DD))


def _labels(ex) -> list[str]:
    return [s.label for s in ex.sections]


def _all_text(ex) -> str:
    return "\n".join(s.text for s in ex.sections)


# ----------------------------------------------------------------- detection
def test_detect_by_extension_and_magic():
    assert detect_kind("a.PDF", b"") == "pdf"
    assert detect_kind("noext", b"%PDF-1.7 ...") == "pdf"
    assert detect_kind("noext", b"<!DOCTYPE html><html>") == "html"
    assert detect_kind("noext", b'{"a": 1}') == "json"
    assert detect_kind("noext", b"\x00\x01\x02binary") == "unknown"


# ---------------------------------------------------------------- formats
def test_pdf_pages():
    pytest.importorskip("pypdf")
    canvas_mod = pytest.importorskip("reportlab.pdfgen.canvas")
    buf = io.BytesIO()
    c = canvas_mod.Canvas(buf)
    c.drawString(72, 720, "Quarterly logistics report page one")
    c.showPage()
    c.drawString(72, 720, "Second page about supply routes")
    c.showPage()
    c.save()
    ex = extract(buf.getvalue(), "report.pdf")
    assert ex.kind == "pdf"
    assert _labels(ex) == ["p.1", "p.2"]
    assert "supply routes" in ex.sections[1].text
    assert ex.sections[1].meta["page"] == 2


def test_docx_sections_and_tables():
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Overview", level=1)
    d.add_paragraph("The vendor network spans three regions. " + HEB)
    d.add_heading("Findings", level=1)
    d.add_paragraph("Shipment delays were observed.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Site", "Status"
    t.cell(1, 0).text, t.cell(1, 1).text = "North depot", "closed"
    buf = io.BytesIO()
    d.save(buf)
    ex = extract(buf.getvalue(), "brief.docx")
    assert ex.kind == "docx"
    assert "Overview" in _labels(ex) and "Findings" in _labels(ex)
    assert HEB in _all_text(ex)
    assert "North depot | closed" in _all_text(ex)


def test_xlsx_one_section_per_sheet():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Q1"
    ws.append(["Name", "Amount"])
    ws.append(["Alpha Ltd", 1200])
    ws2 = wb.create_sheet("Q2")
    ws2.append(["Name", "Amount"])
    ws2.append(["Beta Inc", 900])
    buf = io.BytesIO()
    wb.save(buf)
    ex = extract(buf.getvalue(), "ledger.xlsx")
    assert _labels(ex) == ["sheet Q1", "sheet Q2"]
    assert "Name: Alpha Ltd; Amount: 1200" in ex.sections[0].text


def test_pptx_slides_with_notes():
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Threat landscape"
    slide.placeholders[1].text = "Phishing volume doubled"
    slide.notes_slide.notes_text_frame.text = "Mention the March spike"
    buf = io.BytesIO()
    prs.save(buf)
    ex = extract(buf.getvalue(), "deck.pptx")
    assert ex.sections[0].label == "slide 1: Threat landscape"
    assert "Phishing volume doubled" in ex.sections[0].text
    assert "Speaker notes: Mention the March spike" in ex.sections[0].text


def test_csv_and_tsv():
    ex = extract(b"city;count\nRiverton;4\nLakeside;7\n", "data.csv")
    assert "city: Riverton; count: 4" in _all_text(ex)
    ex = extract(b"a\tb\n1\t2\n", "data.tsv")
    assert "a: 1; b: 2" in _all_text(ex)


def test_html_drops_scripts_and_chrome_and_splits_headings():
    body = "<p>" + ("Detailed paragraph about the topic. " * 80) + "</p>"
    page = (
        "<html><head><title>Runbook</title><script>alert(1)</script></head><body>"
        "<nav>Home | About</nav><h1>Install</h1>" + body + "<h2>Configure</h2>" + body
        + "<footer>Copyright</footer></body></html>"
    )
    ex = extract(page.encode(), "runbook.html")
    text = _all_text(ex)
    assert "alert(1)" not in text and "Home | About" not in text
    assert any(lbl.endswith("Install") for lbl in _labels(ex))
    assert any(lbl.endswith("Configure") for lbl in _labels(ex))


def test_markdown_split_by_heading():
    md = "# One\n" + "alpha " * 900 + "\n# Two\n" + "beta " * 900
    ex = extract(md.encode(), "notes.md")
    assert _labels(ex) == ["One", "Two"]


def test_json_records_and_ndjson():
    data = json.dumps({"results": [{"title": "First", "body": "x"},
                                   {"title": "Second", "body": "y"}]}).encode()
    ex = extract(data, "dump.json")
    assert _labels(ex) == ["First", "Second"]
    assert "body: x" in ex.sections[0].text
    ex = extract(b'{"name": "a"}\nnot json\n{"name": "b"}\n', "log.ndjson")
    assert _labels(ex) == ["a", "b"]
    assert any("not valid JSON" in n for n in ex.notes)


def test_xml_feed_items():
    rss = (b"<?xml version='1.0'?><rss><channel><title>T</title>"
           b"<item><title>One</title><description>first</description></item>"
           b"<item><title>Two</title><description>second</description></item>"
           b"</channel></rss>")
    ex = extract(rss, "feed.xml")
    assert _labels(ex) == ["One", "Two"]


def test_xml_entity_bomb_is_never_expanded():
    pytest.importorskip("defusedxml")
    bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
            b"<lolz>&lol3;</lolz>")
    ex = extract(bomb, "bomb.xml")
    # Refused by the parser, degraded to best-effort raw text: tiny, no expansion.
    assert len(_all_text(ex)) < 1000
    assert "lollollol" not in _all_text(ex)


def test_eml_with_attachment():
    msg = EmailMessage()
    msg["From"] = "analyst@example.org"
    msg["To"] = "team@example.org"
    msg["Subject"] = "Weekly summary"
    msg.set_content("Body text of the weekly summary.")
    msg.add_attachment(b"attached notes about the depot", maintype="text",
                       subtype="plain", filename="notes.txt")
    ex = extract(bytes(msg), "mail.eml")
    assert ex.sections[0].label == "Weekly summary"
    assert "From: analyst@example.org" in ex.sections[0].text
    assert any("attached notes" in s.text for s in ex.sections[1:])


def test_mhtml_saved_page():
    root = MIMEMultipart("related")
    html_part = MIMEText("<html><head><title>Saved wiki page</title></head>"
                         "<body><p>Internal procedure text.</p></body></html>", "html")
    html_part["Content-Location"] = "http://wiki.corp.example/procedure"
    root.attach(html_part)
    ex = extract(root.as_bytes(), "page.mhtml")
    assert ex.kind == "mhtml"
    assert "Internal procedure text." in _all_text(ex)
    assert ex.sections[0].meta["url"] == "http://wiki.corp.example/procedure"


def test_rtf():
    ex = extract(rb"{\rtf1\ansi{\fonttbl\f0 Arial;}\f0 Hello \b world\b0\par Next}", "x.rtf")
    assert "Hello" in _all_text(ex) and "world" in _all_text(ex)


def test_odt():
    content = (b'<?xml version="1.0"?><office:document-content '
               b'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               b'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
               b"<office:body><office:text><text:h>Scope</text:h>"
               b"<text:p>Open document body.</text:p></office:text></office:body>"
               b"</office:document-content>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", content)
    ex = extract(buf.getvalue(), "doc.odt")
    assert "Open document body." in _all_text(ex)


def test_unknown_binary_best_effort():
    blob = b"\x00\x01\x02" * 50 + b"Readable sentence inside a proprietary blob. " * 10
    ex = extract(blob, "thing.qqq")
    assert "Readable sentence" in _all_text(ex)
    assert any("unrecognised" in n for n in ex.notes)
    ex = extract(b"\x00\x01\x02\x03" * 100, "noise.bin")
    assert ex.sections == []


def test_broken_file_never_crashes():
    ex = extract(b"%PDF-1.4 garbage that is not a pdf", "broken.pdf")
    assert ex.kind == "pdf"  # handled, with a note; no exception


def test_image_without_ocr_engine(monkeypatch):
    monkeypatch.setattr("nexus.ocr.ocr_available", lambda: False)
    ex = extract(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "scan.png")
    assert ex.sections == []
    assert any("OCR" in n for n in ex.notes)


# -------------------------------------------------------------- archives
def _zip(entries: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_zip_site_export_keeps_link_structure():
    index = (b"<html><head><title>Docs home</title></head><body><p>Welcome to the docs.</p>"
             b"<a href='guide/setup.html'>Setup</a> <a href='https://example.org'>ext</a>"
             b"</body></html>")
    setup = (b"<html><head><title>Setup guide</title></head><body><p>Run the installer."
             b"</p><a href='../index.html'>Back</a></body></html>")
    data = _zip({"index.html": index, "guide/setup.html": setup,
                 "assets/site.css": b"body{}", "assets/logo.png": b"\x89PNG"})
    ex = extract(data, "site.zip")
    by_title = {s.label: s for s in ex.sections}
    assert set(by_title) == {"Docs home", "Setup guide"}
    assert by_title["Docs home"].meta["site_links"] == ["guide/setup.html"]
    assert by_title["Docs home"].meta["external_links"] == 1
    assert by_title["Setup guide"].meta["site_links"] == ["index.html"]
    assert any("exported web site" in n for n in ex.notes)


def test_zip_mixed_members_and_traversal_dropped():
    data = _zip({"a.txt": b"alpha text", "../../evil.txt": b"should be ignored",
                 "__MACOSX/._a.txt": b"junk"})
    ex = extract(data, "bundle.zip")
    text = _all_text(ex)
    assert "alpha text" in text
    assert "should be ignored" not in text


def test_zip_bomb_ratio_refused():
    data = _zip({"zeros.txt": b"\x00" * (20 * 1024 * 1024)})
    with pytest.raises(UnsafeFileError):
        extract(data, "bomb.zip")


def test_zip_entry_count_and_total_limits():
    data = _zip({f"f{i}.txt": b"x" for i in range(20)})
    with pytest.raises(UnsafeFileError):
        extract(data, "many.zip", Limits(max_zip_entries=10))
    data = _zip({"a.txt": b"a" * 5000, "b.txt": b"b" * 5000}, compression=zipfile.ZIP_STORED)
    with pytest.raises(UnsafeFileError):
        extract(data, "big.zip", Limits(max_zip_total_bytes=6000))


def test_nested_zip_depth_limited():
    inner = _zip({"deep.txt": b"deep text"})
    middle = _zip({"inner.zip": inner})
    outer = _zip({"middle.zip": middle})
    ex = extract(outer, "outer.zip", Limits(max_nesting=1))
    assert "deep text" not in _all_text(ex)
    assert any("nested too deep" in n for n in ex.notes)


def test_docx_bomb_checked_before_parsing():
    data = _zip({"word/document.xml": b"<" + b"a" * (20 * 1024 * 1024)})
    with pytest.raises(UnsafeFileError):
        extract(data, "evil.docx")


def test_text_limit_truncates():
    ex = extract(b"word " * 10_000, "big.txt", Limits(max_text_chars=1000))
    assert len(_all_text(ex)) <= 1000
    assert any("text limit" in n for n in ex.notes)


# ---------------------------------------------------------------- helpers
def test_safe_names():
    assert safe_display_name("../../etc/passwd") == "passwd"
    assert safe_display_name("C:\\Users\\x\\report.pdf") == "report.pdf"
    assert safe_display_name("bad\x00name<>.txt") == "badname__.txt"
    assert safe_display_name("") == "upload"
    assert safe_member_path("../x") is None
    assert safe_member_path("/abs") is None
    assert safe_member_path("C:/x") is None
    assert safe_member_path("a/./b/../c.html") == "a/c.html"


def test_html_extractor_links_and_title():
    doc = extract_html("<title>T</title><p>Hi <a href='/x'>there</a></p>")
    assert doc.title == "T"
    assert doc.links == [("/x", "there")]


def test_format_support_lists_every_family():
    kinds = {f["kind"] for f in format_support()}
    assert {"pdf", "docx", "xlsx", "pptx", "zip", "image", "eml", "mhtml"} <= kinds


def test_missing_library_disables_format(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ex = extract(b"%PDF-1.4", "x.pdf")
    assert ex.sections == []
    assert any("pypdf" in n for n in ex.notes)
