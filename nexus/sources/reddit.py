"""Reddit source — newest submissions from configured subreddits.

Key-gated: needs REDDIT_CLIENT_ID/SECRET and at least one subreddit in
REDDIT_SUBREDDITS. Uses PRAW in read-only mode. The import is guarded so the
platform runs even if praw is not installed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from nexus.config import Settings, get_settings
from nexus.models import RawItem
from nexus.sources.base import Source
from nexus.textclean import clean_text

logger = logging.getLogger("nexus.sources.reddit")

# How many newest posts to pull per subreddit per run.
_LIMIT_PER_SUB = 50
# How many newest matches to pull per unified investigation query (r/all search).
_LIMIT_PER_QUERY = 50


class RedditSource(Source):
    name = "reddit"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def is_available(self) -> bool:
        return bool(
            self.settings.reddit_client_id
            and self.settings.reddit_client_secret
            and (
                self.settings.reddit_subreddit_targets()
                or self.settings.investigation_query_targets()
            )
        )

    def _client(self):
        try:
            import praw
        except ImportError:
            logger.warning("praw not installed; Reddit source disabled.")
            return None
        reddit = praw.Reddit(
            client_id=self.settings.reddit_client_id,
            client_secret=self.settings.reddit_client_secret,
            user_agent=self.settings.reddit_user_agent,
        )
        reddit.read_only = True
        return reddit

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        client = self._client()
        if client is None:
            return []

        items: list[RawItem] = []
        for sub in self.settings.reddit_subreddit_targets():
            try:
                submissions = client.subreddit(sub).new(limit=_LIMIT_PER_SUB)
                for post in submissions:
                    published = datetime.fromtimestamp(
                        getattr(post, "created_utc", 0), tz=timezone.utc
                    )
                    if since and published <= since:
                        continue
                    # selftext is raw Markdown (images, links, >quotes) and the
                    # title can carry HTML entities — clean both to plain text.
                    body = clean_text(getattr(post, "selftext", "") or "")
                    title = clean_text(getattr(post, "title", None)) or None
                    items.append(
                        RawItem(
                            source=self.name,
                            track="api",
                            external_id=getattr(post, "id", None),
                            url="https://reddit.com" + getattr(post, "permalink", ""),
                            author=str(getattr(post, "author", "") or "") or None,
                            title=title,
                            content=body or title or "",
                            published_at=published,
                            raw={
                                "subreddit": sub,
                                "id": getattr(post, "id", None),
                                "score": getattr(post, "score", None),
                                "num_comments": getattr(post, "num_comments", None),
                            },
                        )
                    )
            except Exception as exc:
                logger.warning("Reddit subreddit failed: %s (%s)", sub, exc)
                continue

        # Unified investigation queries: search across all of Reddit (r/all).
        for query in self.settings.investigation_query_targets():
            try:
                results = client.subreddit("all").search(
                    query, sort="new", limit=_LIMIT_PER_QUERY
                )
                for post in results:
                    published = datetime.fromtimestamp(
                        getattr(post, "created_utc", 0), tz=timezone.utc
                    )
                    if since and published <= since:
                        continue
                    # selftext is raw Markdown (images, links, >quotes) and the
                    # title can carry HTML entities — clean both to plain text.
                    body = clean_text(getattr(post, "selftext", "") or "")
                    title = clean_text(getattr(post, "title", None)) or None
                    items.append(
                        RawItem(
                            source=self.name,
                            track="api",
                            external_id=getattr(post, "id", None),
                            url="https://reddit.com" + getattr(post, "permalink", ""),
                            author=str(getattr(post, "author", "") or "") or None,
                            title=title,
                            content=body or title or "",
                            published_at=published,
                            raw={
                                "query": query,
                                "subreddit": str(getattr(post, "subreddit", "") or ""),
                                "id": getattr(post, "id", None),
                                "score": getattr(post, "score", None),
                                "num_comments": getattr(post, "num_comments", None),
                            },
                        )
                    )
            except Exception as exc:
                logger.warning("Reddit search failed: %s (%s)", query, exc)
                continue

        logger.info("Reddit fetched %d items", len(items))
        return items
