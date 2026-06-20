"""Keyless search sources — Google News and Reddit, via their public RSS.

Most of the web is reachable without an API key: Google News and Reddit both
expose full-text *search* as an RSS/Atom feed. These two sources turn every
investigation query term (from the Topics page or .env) into such a feed URL and
reuse the exact same feedparser pipeline as :mod:`nexus.sources.rss`.

Why this matters:
  * **Zero setup** — no keys, no quotas, no registration. They are available the
    moment a capsule has any terms, so a brand-new install collects real results.
  * **Reddit without the API gate** — Reddit's developer-API registration is
    heavily gated, but ``reddit.com/search.rss`` needs nothing at all.
  * **A free alternative to SERPAPI** — Google News RSS covers the same ground
    for news discovery.

Same graceful-degradation contract as every other source: no query terms means
``is_available()`` is False and the collector simply skips it. Per-feed network
errors are swallowed so one bad term never aborts the run.
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote_plus

from nexus.config import Settings, get_settings
from nexus.models import RawItem
from nexus.sources.base import Source, fetch_feed
from nexus.sources.rss import _strip_html, _to_datetime

logger = logging.getLogger("nexus.sources.freesearch")

# Cap how many entries we keep per query term, so a broad term can't flood a scan.
_PER_QUERY_LIMIT = 25


def _parse_feed(
    url: str,
    source_name: str,
    query: str,
    since: datetime | None = None,
    language: str | None = None,
) -> list[RawItem]:
    """Fetch one search-RSS URL and map its entries to RawItems.

    When ``since`` is given, entries published at or before it are skipped so a
    scan surfaces only what is new (mirroring :mod:`nexus.sources.rss`). Entries
    with no parseable date are always kept rather than silently dropped.

    ``language`` stamps a known ISO code on every item when the feed edition
    fixes the result language (e.g. Google News' English edition), so the
    translation pass trusts it instead of guessing from short headlines.
    """
    try:
        parsed = fetch_feed(url)
    except Exception as exc:  # network/parse error on a single query
        logger.warning("%s feed failed: %s (%s)", source_name, url, exc)
        return []
    if parsed.bozo and not parsed.entries:
        logger.warning("%s feed unparseable for query %r", source_name, query)
        return []

    items: list[RawItem] = []
    for entry in parsed.entries[:_PER_QUERY_LIMIT]:
        published = _to_datetime(getattr(entry, "published_parsed", None)) or _to_datetime(
            getattr(entry, "updated_parsed", None)
        )
        if since and published and published <= since:
            continue
        title = _strip_html(getattr(entry, "title", None))
        summary = _strip_html(getattr(entry, "summary", None))
        content = summary or title
        if not content:
            continue
        items.append(
            RawItem(
                source=source_name,
                track="api",
                external_id=getattr(entry, "id", None) or getattr(entry, "link", None),
                url=getattr(entry, "link", None),
                author=getattr(entry, "author", None),
                title=title or None,
                content=content,
                language=language,
                published_at=published,
                raw={"query": query, "feed": url, "link": getattr(entry, "link", None)},
            )
        )
    return items


class GoogleNewsSource(Source):
    """Google News search results for each investigation term, via free RSS."""

    name = "gnews"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def queries(self) -> list[str]:
        return self.settings.investigation_query_targets()

    def is_available(self) -> bool:
        return bool(self.queries)

    def _url(self, query: str) -> str:
        # hl/gl/ceid keep results in English; the feed is keyless.
        # A "when:Nd" operator bounds results to the last N days — this both
        # keeps the feed fresh and caps how many items a broad term can return.
        days = getattr(self.settings, "news_window_days", 0) or 0
        q = f"{query} when:{days}d" if days > 0 else query
        return (
            "https://news.google.com/rss/search?q="
            f"{quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
        )

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        for q in self.queries:
            # The hl=en-US edition fixes results to English, so declare it.
            items.extend(_parse_feed(self._url(q), self.name, q, since=since, language="en"))
        logger.info("Google News fetched %d items for %d queries", len(items), len(self.queries))
        return items


class RedditSearchSource(Source):
    """Reddit search results for each investigation term, via free RSS.

    Uses ``reddit.com/search.rss`` which, unlike the developer API, needs no
    credentials — so it works even when Reddit API registration is unavailable.
    """

    name = "reddit"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def queries(self) -> list[str]:
        return self.settings.investigation_query_targets()

    def is_available(self) -> bool:
        # Only used as the keyless fallback when the official API is not set up.
        # If Reddit API credentials exist, the RedditSource handles it instead.
        return bool(self.queries) and not self.settings.reddit_enabled

    def _url(self, query: str) -> str:
        return (
            "https://www.reddit.com/search.rss?q="
            f"{quote_plus(query)}&sort=new&limit={_PER_QUERY_LIMIT}"
        )

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        for q in self.queries:
            items.extend(_parse_feed(self._url(q), self.name, q, since=since))
        logger.info("Reddit (RSS) fetched %d items for %d queries", len(items), len(self.queries))
        return items
