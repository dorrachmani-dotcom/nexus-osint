"""Regression tests for the timed feed fetch (nexus.sources.base.fetch_feed).

`feedparser.parse(url)` does its own networking with no timeout, so a single
hung host could stall an entire scan thread forever. `fetch_feed` must instead
pull the bytes over httpx with a bounded timeout and hand only the content to
feedparser. Redirects must be re-checked hop by hop with the SSRF guard, so a
public feed cannot bounce the request to a loopback / private address. These
tests lock that contract in; no real network is used.
"""

from __future__ import annotations

import httpx
import pytest

from nexus import netguard
from nexus.sources import base

_RSS = b"<rss><channel><title>t</title><item><title>hi</title></item></channel></rss>"


@pytest.fixture
def public_dns(monkeypatch):
    """Treat *.example as public; everything else goes through the real guard."""
    real = netguard.safe_http_url

    def fake(url: str):
        if ".example" in url:
            return True, ""
        return real(url)

    monkeypatch.setattr(netguard, "safe_http_url", fake)


def _mock(monkeypatch, handler):
    """Route netguard's httpx.Client through a MockTransport; capture kwargs."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    captured: dict = {}

    def client(*args, **kwargs):
        captured.update(kwargs)
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(netguard.httpx, "Client", client)
    return captured


def test_fetch_feed_uses_bounded_timeout_and_parses(public_dns, monkeypatch):
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("User-Agent", "")
        return httpx.Response(200, content=_RSS)

    captured = _mock(monkeypatch, handler)
    parsed = base.fetch_feed("http://feeds.example/feed")

    # A real, positive network timeout must be passed (the whole point of the fix).
    assert isinstance(captured.get("timeout"), (int, float))
    assert captured["timeout"] > 0
    # Redirects are never auto-followed by httpx; netguard follows them by hand.
    assert captured.get("follow_redirects") is False
    # A UA is sent (some feeds reject the default urllib UA).
    assert seen["ua"].startswith("nexus-osint/")
    # The bytes were handed to feedparser, which parsed them (no network of its own).
    assert parsed.entries and parsed.entries[0].title == "hi"


def test_fetch_feed_follows_public_redirects(public_dns, monkeypatch):
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/feed"})
        return httpx.Response(200, content=_RSS)

    _mock(monkeypatch, handler)
    parsed = base.fetch_feed("http://feeds.example/old")
    assert parsed.entries and parsed.entries[0].title == "hi"


def test_fetch_feed_blocks_redirect_to_loopback(public_dns, monkeypatch):
    """SSRF: a public feed URL must not bounce the request to 127.0.0.1."""
    hits: list[str] = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/settings"})

    _mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="non-public"):
        base.fetch_feed("http://feeds.example/feed")
    # Only the public URL was ever requested; the loopback hop never was.
    assert hits == ["http://feeds.example/feed"]


def test_fetch_feed_propagates_network_errors(public_dns, monkeypatch):
    """A failed fetch must raise so the per-feed caller can isolate that one
    feed (callers already wrap fetch_feed in try/except)."""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    _mock(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError):
        base.fetch_feed("http://feeds.example/feed")


def test_fetch_feed_raises_on_http_error(public_dns, monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        base.fetch_feed("http://feeds.example/feed")
