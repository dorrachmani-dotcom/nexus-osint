"""Abstract base class shared by every collection source.

A Source maps some external feed/API into a list of `RawItem`s. The contract is
deliberately tiny so adding a source (or a Plug & Play tool adapter) later is an
extension, not a rewrite. Implementations must never raise on a missing key —
they report unavailability via `is_available()` and the collector skips them.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime

import feedparser
import httpx

from nexus.models import RawItem

logger = logging.getLogger("nexus.sources")

# Some feed hosts reject the default urllib UA; a browser-ish UA is widely
# accepted and harmless.
_FEED_USER_AGENT = "nexus-osint/1.0 (+https://127.0.0.1)"


def fetch_feed(url: str, timeout: float = 30.0):
    """Fetch and parse a feed URL with a hard network timeout.

    ``feedparser.parse(url)`` does its own networking with **no timeout**, so a
    single slow or hung host can stall an entire scan thread indefinitely. Every
    other source in this package fetches over httpx with a bounded timeout; this
    helper does the same for the RSS/Atom backbone: it pulls the bytes through
    httpx (bounded timeout, redirects followed, a UA some feeds require) and then
    hands the content to feedparser, which only parses — it never touches the
    network. Raises on a network error / non-2xx so the caller (which already
    wraps the call in try/except) can isolate that one feed.
    """
    resp = httpx.get(
        url,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _FEED_USER_AGENT},
    )
    resp.raise_for_status()
    return feedparser.parse(resp.content)


class Source(ABC):
    #: Logical, stable identifier stored on every item (e.g. "rss").
    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """True when this source is configured and usable this run."""

    @abstractmethod
    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        """Return new items, optionally only those newer than `since`.

        Implementations should swallow per-item parse errors and return what
        they could collect; the collector handles source-level failures.
        """
