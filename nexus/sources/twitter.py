"""Twitter / X source — official API v2 recent search.

Key-gated: needs TWITTER_BEARER_TOKEN and at least one query in TWITTER_QUERIES.
Each tweet becomes a RawItem. Per-query errors are isolated.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from nexus.config import Settings, get_settings
from nexus.models import RawItem
from nexus.sources.base import Source

logger = logging.getLogger("nexus.sources.twitter")

_ENDPOINT = "https://api.twitter.com/2/tweets/search/recent"
_MAX_RESULTS = 50


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # API returns ISO 8601 with a trailing Z.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class TwitterSource(Source):
    name = "twitter"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def is_available(self) -> bool:
        return bool(
            self.settings.twitter_bearer_token and self.settings.twitter_query_targets()
        )

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        headers = {"Authorization": f"Bearer {self.settings.twitter_bearer_token}"}
        items: list[RawItem] = []
        for query in self.settings.twitter_query_targets():
            try:
                resp = httpx.get(
                    _ENDPOINT,
                    headers=headers,
                    params={
                        "query": query,
                        "max_results": _MAX_RESULTS,
                        "tweet.fields": "created_at,author_id,lang",
                        # Expand the author so we can show a real @handle (and a
                        # clean profile URL) instead of an opaque numeric id.
                        "expansions": "author_id",
                        "user.fields": "username,name",
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("Twitter query failed: %s (%s)", query, exc)
                continue

            # author_id -> username, from the expansion block (may be absent).
            users = (data.get("includes") or {}).get("users") or []
            handle_by_id = {
                u.get("id"): u.get("username")
                for u in users
                if u.get("id") and u.get("username")
            }

            for tweet in data.get("data", []):
                tid = tweet.get("id")
                text = tweet.get("text") or ""
                if not text:
                    continue
                username = handle_by_id.get(tweet.get("author_id"))
                # Prefer a human @handle; fall back to the numeric id if the
                # expansion was missing so the author is never blank.
                author = f"@{username}" if username else tweet.get("author_id")
                # A handle gives a clean canonical URL; otherwise use the
                # id-based permalink, which still resolves.
                if username and tid:
                    url = f"https://twitter.com/{username}/status/{tid}"
                elif tid:
                    url = f"https://twitter.com/i/web/status/{tid}"
                else:
                    url = None
                items.append(
                    RawItem(
                        source=self.name,
                        track="api",
                        external_id=tid,
                        url=url,
                        author=author,
                        title=None,
                        content=text,
                        language=tweet.get("lang"),
                        published_at=_parse_date(tweet.get("created_at")),
                        raw={"query": query, "tweet": tweet},
                    )
                )
        logger.info("Twitter fetched %d items", len(items))
        return items
