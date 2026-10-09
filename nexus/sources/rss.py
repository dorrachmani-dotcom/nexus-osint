"""RSS source — the always-on collection backbone (no API key required).

Parses every feed URL configured in RSS_FEEDS and maps each entry to a RawItem.
HTML is stripped to plain text so the content is clean for analysis and search.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from nexus.config import Settings, get_settings
from nexus.models import RawItem
from nexus.netguard import safe_http_url
from nexus.sources.base import Source, fetch_feed
from nexus.textclean import clean_text as _strip_html

logger = logging.getLogger("nexus.sources.rss")


def _to_datetime(struct_time) -> datetime | None:
    """feedparser exposes parsed dates as time.struct_time in UTC."""
    if not struct_time:
        return None
    try:
        year, month, day, hour, minute, second = struct_time[:6]
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _normalize_lang(value: str | None) -> str | None:
    """Normalise a feed/entry language tag to a short ISO code.

    Feeds declare languages like ``en-GB``/``ar``/``ru-RU``; we keep only the
    primary subtag (``en``/``ar``/``ru``) so it matches the codes the
    translation pass expects. Returns ``None`` when absent so the pass falls
    back to script-based detection.
    """
    if not value:
        return None
    code = str(value).strip().lower().split("-")[0]
    return code or None


class RSSSource(Source):
    name = "rss"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def feeds(self) -> list[str]:
        # Union of .env feeds and feeds chosen on the Topics page.
        return self.settings.rss_feed_targets()

    def is_available(self) -> bool:
        return bool(self.feeds)

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        for url in self.feeds:
            # SSRF / local-file guard: feed URLs are operator-supplied but
            # feedparser will happily open file:// and internal hosts. Only let
            # public http(s) URLs through.
            safe, reason = safe_http_url(url)
            if not safe:
                logger.warning("RSS feed blocked (%s): %s", reason, url)
                continue
            try:
                parsed = fetch_feed(url)
            except Exception as exc:  # network/parse error on a single feed
                logger.warning("RSS feed failed: %s (%s)", url, exc)
                continue
            if parsed.bozo and not parsed.entries:
                logger.warning("RSS feed unparseable: %s", url)
                continue

            feed_title = getattr(parsed.feed, "title", None)
            # Feed-level language (e.g. "en-GB"); a per-entry tag overrides it.
            feed_lang = _normalize_lang(getattr(parsed.feed, "language", None))
            for entry in parsed.entries:
                published = _to_datetime(getattr(entry, "published_parsed", None)) or _to_datetime(
                    getattr(entry, "updated_parsed", None)
                )
                if since and published and published <= since:
                    continue

                summary = _strip_html(getattr(entry, "summary", None))
                title = _strip_html(getattr(entry, "title", None))
                content = summary or title
                if not content:
                    continue

                items.append(
                    RawItem(
                        source=self.name,
                        track="api",
                        external_id=getattr(entry, "id", None) or getattr(entry, "link", None),
                        url=getattr(entry, "link", None),
                        author=getattr(entry, "author", None) or feed_title,
                        title=title or None,
                        content=content,
                        language=_normalize_lang(getattr(entry, "language", None))
                        or feed_lang,
                        published_at=published,
                        raw={
                            "feed": url,
                            "feed_title": feed_title,
                            "id": getattr(entry, "id", None),
                            "link": getattr(entry, "link", None),
                        },
                    )
                )
        logger.info("RSS fetched %d items from %d feeds", len(items), len(self.feeds))
        return items
