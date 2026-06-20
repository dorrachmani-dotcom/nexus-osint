"""Tests for OCR-over-evidence (nexus.ocr) and its searchability wiring.

OCR is a pure, graceful enhancement: when the Tesseract engine / pytesseract
wrapper is absent it must return "unavailable" rather than raise, and the feed
search must behave exactly as before (an empty OCR column matches nothing).
These run with no engine installed, so they exercise the graceful path and the
"OCR text makes an item findable" path by inserting evidence rows directly.

No network, no AI, no real screenshots — everything hits the temp_db connection.
"""

from __future__ import annotations

from pathlib import Path

import nexus.ocr as ocr
from nexus.models import RawItem
from nexus.storage import count_matching_items, search_items, upsert_item


# --------------------------------------------------------------- module purity
def test_extract_text_missing_file_returns_none():
    assert ocr.extract_text(Path("does-not-exist-12345.png")) is None


def test_ocr_available_is_boolean():
    # Whatever the host has, this must answer True/False and never raise.
    assert isinstance(ocr.ocr_available(), bool)


def test_extract_text_never_raises_on_garbage(tmp_path):
    # A non-image file must be swallowed into None, not propagate an exception.
    junk = tmp_path / "not-an-image.png"
    junk.write_bytes(b"this is plainly not a PNG")
    assert ocr.extract_text(junk) is None


def test_clean_collapses_whitespace_noise():
    raw = "  hello   world \n\n\n  second   line  \n   "
    assert ocr._clean(raw) == "hello world\nsecond line"


def test_clean_empty_is_empty_string():
    assert ocr._clean("") == ""
    assert ocr._clean("   \n  \n") == ""


# ------------------------------------------------------ searchability of OCR text
def _add(conn, *, title, content="body"):
    item_id, _ = upsert_item(conn, RawItem(source="rss", title=title, content=content))
    return item_id


def test_ocr_text_makes_item_findable(temp_db):
    """A word that appears ONLY in a screenshot's OCR text should still match."""
    with temp_db() as conn:
        item_id = _add(conn, title="plain headline", content="nothing special here")
        conn.execute(
            "INSERT INTO evidence (item_id, screenshot, sha256, captured_at, ocr_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_id, "evidence/x.png", "deadbeef", "2026-01-01T00:00:00+00:00",
             "SECRETWALLET visible only inside the image"),
        )
        hits = search_items(conn, q="SECRETWALLET")
        assert [h["id"] for h in hits] == [item_id]
        # count stays in lock-step with the listing (pagination invariant).
        assert count_matching_items(conn, q="SECRETWALLET") == 1


def test_no_ocr_text_does_not_change_results(temp_db):
    """With no OCR rows, a search behaves exactly as the plain FTS search."""
    with temp_db() as conn:
        a = _add(conn, title="alpha report", content="one")
        _add(conn, title="beta report", content="two")
        hits = search_items(conn, q="alpha")
        assert [h["id"] for h in hits] == [a]
        assert count_matching_items(conn, q="alpha") == 1


def test_ocr_branch_only_on_free_text_not_topic_terms(temp_db):
    """The OCR OR-branch is for free-text q only; topic-term scans ignore it."""
    with temp_db() as conn:
        item_id = _add(conn, title="plain", content="body")
        conn.execute(
            "INSERT INTO evidence (item_id, screenshot, sha256, captured_at, ocr_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_id, "evidence/y.png", "cafe", "2026-01-01T00:00:00+00:00",
             "topicword"),
        )
        # Searching by capsule terms must NOT pull the item in via OCR.
        hits = search_items(conn, terms=["topicword"])
        assert hits == []
        assert count_matching_items(conn, terms=["topicword"]) == 0
