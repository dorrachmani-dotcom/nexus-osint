"""Google Custom Search source — the official Programmable Search JSON API.

Key-gated: needs GOOGLE_CSE_KEY (API key) *and* GOOGLE_CSE_CX (the Search
Engine ID). Absent either, the source reports itself unavailable and is skipped
(graceful degradation). It searches every investigation query term from the
active capsules — the same unified terms Google News and SERPAPI use.

This complements the keyless Google News RSS source: SERPAPI and Google News
cover news discovery, while Custom Search reaches the broader indexed web (any
site the engine is configured to cover). Using both widens coverage.

Per-query/network errors are isolated so one bad term never aborts the run.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from nexus.config import Settings, get_settings
from nexus.models import RawItem
from nexus.sources.base import Source

logger = logging.getLogger("nexus.sources.google_cse")

_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
# Google CSE returns at most 10 results per request; we keep a single page per
# term to stay well within the free daily quota (100 queries/day).
_PER_QUERY_LIMIT = 10


def _date_restrict(days: int) -> str | None:
    """Map a day count to the API's dateRestrict value (e.g. 'd7'); None if 0."""
    return f"d{days}" if days and days > 0 else None


class GoogleCseSource(Source):
    name = "google_cse"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def queries(self) -> list[str]:
        return self.settings.investigation_query_targets()

    def is_available(self) -> bool:
        return bool(
            self.settings.google_cse_key
            and self.settings.google_cse_cx
            and self.queries
        )

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        restrict = _date_restrict(getattr(self.settings, "news_window_days", 0) or 0)
        for query in self.queries:
            params = {
                "key": self.settings.google_cse_key,
                "cx": self.settings.google_cse_cx,
                "q": query,
                "num": _PER_QUERY_LIMIT,
            }
            if restrict:
                params["dateRestrict"] = restrict
            try:
                resp = httpx.get(_ENDPOINT, params=params, timeout=30.0)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("Google CSE query failed: %s (%s)", query, exc)
                continue

            for result in data.get("items", []):
                link = result.get("link")
                title = result.get("title")
                if not (title or link):
                    continue
                snippet = result.get("snippet") or ""
                display = result.get("displayLink")
                items.append(
                    RawItem(
                        source=self.name,
                        track="api",
                        external_id=link or title,
                        url=link,
                        author=display,
                        title=title,
                        content=snippet or title or "",
                        published_at=None,
                        raw={"query": query, "result": result},
                    )
                )
        logger.info("Google CSE fetched %d items for %d queries", len(items), len(self.queries))
        return items
