"""SERPAPI source — Google News results for configured queries.

Key-gated: needs SERPAPI_KEY and at least one query in SERPAPI_QUERIES. Each
news result becomes a RawItem. Per-query errors are isolated so one bad query
never aborts the run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from nexus.config import Settings, get_settings
from nexus.models import RawItem
from nexus.sources.base import Source

logger = logging.getLogger("nexus.sources.serpapi")

_ENDPOINT = "https://serpapi.com/search.json"
# Cap entries kept per query. SERPAPI is the paid Google News source and returns
# many results per term; without a cap it floods the analysis backlog (and it
# overlaps heavily with the free, time-bounded Google News RSS source). Keep it
# in line with the keyless sources so no single term dominates a scan.
_PER_QUERY_LIMIT = 25


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    # SERPAPI returns human strings ("2 hours ago") or RFC-ish dates; accept the
    # parseable ones and silently ignore the rest.
    for fmt in ("%m/%d/%Y, %I:%M %p, %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


class SerpApiSource(Source):
    name = "serpapi"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def is_available(self) -> bool:
        return bool(self.settings.serpapi_key and self.settings.serpapi_query_targets())

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        for query in self.settings.serpapi_query_targets():
            before = len(items)
            try:
                resp = httpx.get(
                    _ENDPOINT,
                    params={
                        "engine": "google_news",
                        "q": query,
                        "api_key": self.settings.serpapi_key,
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("SERPAPI query failed: %s (%s)", query, exc)
                continue

            for result in data.get("news_results", []):
                if len(items) - before >= _PER_QUERY_LIMIT:
                    break  # keep one term from flooding the scan
                # Some results nest the real story under "stories".
                stories = result.get("stories") or [result]
                for story in stories:
                    if len(items) - before >= _PER_QUERY_LIMIT:
                        break
                    link = story.get("link")
                    title = story.get("title")
                    if not (title or link):
                        continue
                    snippet = story.get("snippet") or ""
                    published = _parse_date(story.get("date"))
                    items.append(
                        RawItem(
                            source=self.name,
                            track="api",
                            external_id=link or title,
                            url=link,
                            author=(story.get("source") or {}).get("name")
                            if isinstance(story.get("source"), dict)
                            else story.get("source"),
                            title=title,
                            content=snippet or title or "",
                            published_at=published,
                            raw={"query": query, "result": story},
                        )
                    )
        logger.info("SERPAPI fetched %d items", len(items))
        return items
