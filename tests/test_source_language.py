"""Tests for source-declared content language.

The keyless-translation pass trusts a source-declared ``language`` over guessing
from short text. Two sources can declare it reliably:

  * RSS — from the feed/entry language tag (covered in test_rss.py).
  * Google News — its ``hl=en-US`` edition fixes results to English, so every
    gnews item is stamped ``en``.

The keyless Reddit search edition does NOT fix a language (Reddit posts are
multilingual), so it must leave ``language`` unset and fall back to detection.
"""

from __future__ import annotations


def _fake_parsed(entries):
    class _P:
        bozo = 0

        def __init__(self, e):
            self.entries = e
            self.feed = type("F", (), {"title": "Feed"})()

    return _P(entries)


def _entry(title):
    return type(
        "E",
        (),
        {
            "title": title,
            "summary": title,
            "published_parsed": (2026, 6, 1, 0, 0, 0, 0, 0, 0),
            "id": title,
            "link": f"https://example.test/{title}",
        },
    )()


def test_google_news_items_are_stamped_english(monkeypatch):
    import nexus.sources.freesearch as fs

    monkeypatch.setattr(fs, "fetch_feed", lambda url: _fake_parsed([_entry("a")]))
    src = fs.GoogleNewsSource()
    # Drive a query so fetch() actually pulls.
    monkeypatch.setattr(type(src), "queries", property(lambda self: ["tesla"]))

    items = src.fetch()
    assert items and all(it.language == "en" for it in items)


def test_reddit_search_items_have_no_declared_language(monkeypatch):
    import nexus.sources.freesearch as fs

    monkeypatch.setattr(fs, "fetch_feed", lambda url: _fake_parsed([_entry("b")]))
    src = fs.RedditSearchSource()
    monkeypatch.setattr(type(src), "queries", property(lambda self: ["tesla"]))

    items = src.fetch()
    # Multilingual edition -> leave it to script detection downstream.
    assert items and all(it.language is None for it in items)


def test_parse_feed_passes_language_through(monkeypatch):
    import nexus.sources.freesearch as fs

    monkeypatch.setattr(fs, "fetch_feed", lambda url: _fake_parsed([_entry("c")]))
    items = fs._parse_feed("https://x.test", "gnews", "q", language="en")
    assert items and items[0].language == "en"

    none_items = fs._parse_feed("https://x.test", "reddit", "q")
    assert none_items and none_items[0].language is None
