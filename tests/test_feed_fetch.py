"""Regression tests for the timed feed fetch (nexus.sources.base.fetch_feed).

`feedparser.parse(url)` does its own networking with no timeout, so a single
hung host could stall an entire scan thread forever. `fetch_feed` must instead
pull the bytes over httpx with a bounded timeout and hand only the content to
feedparser. These tests lock that contract in.
"""

from __future__ import annotations

from nexus.sources import base


class _FakeResp:
    def __init__(self, content: bytes):
        self.content = content
        self.raised = False

    def raise_for_status(self):
        self.raised = True


def test_fetch_feed_uses_bounded_timeout_and_redirects(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _FakeResp(
            b"<rss><channel><title>t</title>"
            b"<item><title>hi</title></item></channel></rss>"
        )

    monkeypatch.setattr(base.httpx, "get", fake_get)

    parsed = base.fetch_feed("http://example.com/feed")

    # A real, positive network timeout must be passed (the whole point of the fix).
    assert isinstance(captured.get("timeout"), (int, float))
    assert captured["timeout"] > 0
    # Redirects followed and a UA sent (some feeds reject the default urllib UA).
    assert captured.get("follow_redirects") is True
    assert "User-Agent" in captured.get("headers", {})
    # The bytes were handed to feedparser, which parsed them (no network of its own).
    assert parsed.entries and parsed.entries[0].title == "hi"


def test_fetch_feed_propagates_network_errors(monkeypatch):
    """A failed fetch must raise so the per-feed caller can isolate that one
    feed (callers already wrap fetch_feed in try/except)."""

    def boom(url, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(base.httpx, "get", boom)

    raised = False
    try:
        base.fetch_feed("http://example.com/feed")
    except RuntimeError:
        raised = True
    assert raised
