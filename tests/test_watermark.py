"""Tests for incremental "Update" via per-source watermarks.

Covers (a) watermark round-trip storage as aware UTC, (b) the collector passing
the stored ``since`` to a source, and (c) the keyless search sources filtering
out entries at or before ``since``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus.storage import (
    _parse_utc,
    get_source_watermark,
    set_source_watermark,
)


# --- (a) Watermark round-trip ----------------------------------------------


def test_watermark_roundtrip_aware_utc(temp_db):
    when = datetime(2026, 6, 3, 12, 30, 0, tzinfo=timezone.utc)
    with temp_db() as conn:
        set_source_watermark(conn, "rss", when)
    with temp_db() as conn:
        got = get_source_watermark(conn, "rss")
    assert got is not None
    assert got.tzinfo is not None
    assert got.utcoffset() == timedelta(0)  # UTC
    assert got == when


def test_watermark_naive_input_treated_as_utc(temp_db):
    naive = datetime(2026, 1, 1, 0, 0, 0)
    with temp_db() as conn:
        set_source_watermark(conn, "gnews", naive)
    with temp_db() as conn:
        got = get_source_watermark(conn, "gnews")
    assert got == naive.replace(tzinfo=timezone.utc)


def test_watermark_non_utc_offset_normalised(temp_db):
    # +02:00 -> stored/read back as the equivalent UTC instant.
    aware = datetime(2026, 6, 3, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    with temp_db() as conn:
        set_source_watermark(conn, "reddit", aware)
    with temp_db() as conn:
        got = get_source_watermark(conn, "reddit")
    assert got == datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)


def test_watermark_absent_source_is_none(temp_db):
    with temp_db() as conn:
        assert get_source_watermark(conn, "never_seen") is None


def test_parse_utc_handles_sqlite_now_format():
    # datetime('now') format: no offset, implicitly UTC.
    dt = _parse_utc("2026-06-03 12:30:00")
    assert dt == datetime(2026, 6, 3, 12, 30, 0, tzinfo=timezone.utc)


def test_parse_utc_never_raises_on_garbage():
    assert _parse_utc(None) is None
    assert _parse_utc("") is None
    assert _parse_utc("not a date") is None


# --- (b) Collector passes stored `since` to the source ----------------------


class _FakeSource:
    """Minimal Source double that records the ``since`` it was handed."""

    name = "fake"

    def __init__(self):
        self.received_since = "UNSET"

    def is_available(self) -> bool:
        return True

    def fetch(self, since=None):
        self.received_since = since
        return []


def test_collector_passes_stored_since(temp_db, monkeypatch):
    from nexus.collector import Collector

    fake = _FakeSource()
    watermark = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)
    with temp_db() as conn:
        set_source_watermark(conn, fake.name, watermark)

    collector = Collector()
    monkeypatch.setattr(collector, "available_sources", lambda: [fake])

    stats = collector._scan()

    assert fake.received_since == watermark
    assert stats[fake.name]["fetched"] == 0


def test_collector_first_run_since_is_none(temp_db, monkeypatch):
    from nexus.collector import Collector

    fake = _FakeSource()
    collector = Collector()
    monkeypatch.setattr(collector, "available_sources", lambda: [fake])

    collector._scan()

    assert fake.received_since is None  # no watermark yet -> full pull


def test_collector_advances_watermark_on_success(temp_db, monkeypatch):
    from nexus.collector import Collector

    fake = _FakeSource()
    collector = Collector()
    monkeypatch.setattr(collector, "available_sources", lambda: [fake])

    collector._scan()

    with temp_db() as conn:
        wm = get_source_watermark(conn, fake.name)
    assert wm is not None  # advanced even though it returned zero items


def test_collector_keeps_watermark_when_source_errors(temp_db, monkeypatch):
    from nexus.collector import Collector

    old = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    with temp_db() as conn:
        set_source_watermark(conn, "boom", old)

    class _Boom:
        name = "boom"

        def is_available(self) -> bool:
            return True

        def fetch(self, since=None):
            raise RuntimeError("source down")

    collector = Collector()
    monkeypatch.setattr(collector, "available_sources", lambda: [_Boom()])

    stats = collector._scan()

    assert "error" in stats["boom"]
    with temp_db() as conn:
        assert get_source_watermark(conn, "boom") == old  # unchanged


# --- (c) Keyless search since-filtering -------------------------------------


def _fake_parsed(entries):
    class _P:
        bozo = 0

        def __init__(self, e):
            self.entries = e
            self.feed = type("F", (), {"title": "Feed"})()

    return _P(entries)


def _entry(title, struct_time):
    return type(
        "E",
        (),
        {
            "title": title,
            "summary": title,
            "published_parsed": struct_time,
            "id": title,
            "link": f"https://example.test/{title}",
        },
    )()


@pytest.mark.parametrize("source_name", ["gnews", "reddit"])
def test_freesearch_since_filtering(monkeypatch, source_name):
    import nexus.sources.freesearch as fs

    # One entry before `since`, one after.
    old = (2026, 1, 1, 0, 0, 0, 0, 0, 0)
    new = (2026, 6, 1, 0, 0, 0, 0, 0, 0)
    parsed = _fake_parsed([_entry("old", old), _entry("new", new)])
    monkeypatch.setattr(fs, "fetch_feed", lambda url: parsed)

    since = datetime(2026, 3, 1, tzinfo=timezone.utc)
    items = fs._parse_feed("https://x.test", source_name, "q", since=since)

    titles = {it.title for it in items}
    assert titles == {"new"}  # old dropped, new kept


def test_freesearch_no_since_keeps_all(monkeypatch):
    import nexus.sources.freesearch as fs

    old = (2026, 1, 1, 0, 0, 0, 0, 0, 0)
    new = (2026, 6, 1, 0, 0, 0, 0, 0, 0)
    parsed = _fake_parsed([_entry("old", old), _entry("new", new)])
    monkeypatch.setattr(fs, "fetch_feed", lambda url: parsed)

    items = fs._parse_feed("https://x.test", "gnews", "q", since=None)
    assert {it.title for it in items} == {"old", "new"}


def test_freesearch_keeps_undated_entries(monkeypatch):
    import nexus.sources.freesearch as fs

    # An entry with no parseable date must not be silently dropped by `since`.
    parsed = _fake_parsed([_entry("undated", None)])
    monkeypatch.setattr(fs, "fetch_feed", lambda url: parsed)

    since = datetime(2026, 3, 1, tzinfo=timezone.utc)
    items = fs._parse_feed("https://x.test", "gnews", "q", since=since)
    assert {it.title for it in items} == {"undated"}
