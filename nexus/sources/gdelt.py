"""GDELT source — global, multilingual topic/entity monitoring (no API key).

GDELT's Document 2.0 API indexes worldwide news coverage in 100+ languages,
refreshed every 15 minutes, over a rolling ~3-month window — and it's free, with
no key or registration. That makes it the backbone of the monitoring pivot:
instead of deep-diving one person, an analyst watches a topic or entity ("Neymar",
"Google", a company, a place) across the whole world's press.

Language rule (product-critical): **the language you search in is the language
you get back.** For each query we resolve its language (an explicit ``lang:<code>``
tag, else the script it's written in — see :mod:`nexus.lang`) and constrain GDELT
with ``sourcelang:`` so a Chinese query returns Chinese-source articles only, an
Arabic query Arabic-source, and so on. Latin-script queries with no tag run
unfiltered (broadest recall). Either way the unified feed renders English (via the
analysis translation layer) and the original is preserved on the item.

Graceful degradation, like every source: no query terms -> ``is_available()`` is
False and the collector skips it; a per-query network error is swallowed so one
bad term never aborts the run.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import httpx

from nexus.config import Settings, get_settings
from nexus.lang import resolve_query_language
from nexus.models import RawItem
from nexus.sources.base import Source

logger = logging.getLogger("nexus.sources.gdelt")

_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
# Cap entries kept per query so a broad term can't flood a scan, in line with
# the other search sources. GDELT allows up to 250 records per request.
_PER_QUERY_LIMIT = 50
# GDELT's public DOC API asks callers to "limit requests to one every 5 seconds"
# (its own 429 body). A scan with several query terms would otherwise 429 itself,
# so we space requests at their stated limit and back off once on a 429 rather
# than losing the term. Tunable, but kept polite by default.
_PACING_SECONDS = 5.0
_BACKOFF_SECONDS = 6.0
# Once a 429 survives the backoff retry, GDELT is throttling us for a sustained
# window, not just a single burst — every remaining query this run would fail
# too, each wasting a pacing + backoff wait. After this many consecutive
# sustained-throttle hits we stop the run early instead of grinding through
# minutes of guaranteed failures. A success resets the count.
_MAX_CONSECUTIVE_THROTTLE = 2


class _RateLimited(Exception):
    """GDELT returned 429 even after a backoff retry — a sustained-throttle
    signal (distinct from a one-off network error) that should bail the run."""


def _parse_seendate(value: str | None) -> datetime | None:
    """GDELT 'seendate' looks like ``20260603T120000Z`` (UTC)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


class GdeltSource(Source):
    """Worldwide multilingual news monitoring for each investigation term."""

    name = "gdelt"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def queries(self) -> list[str]:
        return self.settings.investigation_query_targets()

    def is_available(self) -> bool:
        # Keyless: live whenever a capsule/term exists to monitor.
        return bool(self.queries)

    def _build_query(self, clean_query: str, gdelt_lang: str | None) -> str:
        """Compose the GDELT query string with an optional language filter.

        A multi-word term is phrase-quoted so it matches as a unit; the
        ``sourcelang:`` operator (when we resolved a language) restricts results
        to that language's sources — the heart of "search-language = result-
        language".
        """
        term = clean_query.strip()
        if " " in term and not (term.startswith('"') and term.endswith('"')):
            term = f'"{term}"'
        if gdelt_lang:
            return f"{term} sourcelang:{gdelt_lang}"
        return term

    def _timespan(self) -> str:
        """How far back to pull, reusing the shared news window (days)."""
        days = getattr(self.settings, "news_window_days", 0) or 0
        # GDELT only retains ~3 months; clamp and default to a sane 1-week window
        # when no limit is configured so a scan stays fresh and bounded.
        if days <= 0:
            return "1w"
        return f"{min(days, 90)}d"

    def _request(self, params: dict) -> dict | None:
        """One GDELT call with a single 429 backoff-retry. Returns parsed JSON or
        None on a generic failure (caller treats as "no items"). Raises
        ``_RateLimited`` when a 429 survives the retry, so the caller can stop the
        run instead of hammering an endpoint that is throttling us."""
        for attempt in (0, 1):
            try:
                resp = httpx.get(
                    _ENDPOINT,
                    params=params,
                    timeout=30.0,
                    headers={"User-Agent": "nexus-osint/1.0"},
                )
                if resp.status_code == 429:
                    if attempt == 0:
                        # Burst limit: wait once, then retry the same query.
                        time.sleep(_BACKOFF_SECONDS)
                        continue
                    # Still throttled after a backoff -> sustained rate-limit.
                    raise _RateLimited()
                resp.raise_for_status()
                # GDELT occasionally returns an HTML error page with a 200; guard
                # the JSON decode so that degrades to "no items", never a crash.
                return resp.json()
            except _RateLimited:
                raise
            except Exception:
                if attempt == 0:
                    time.sleep(_BACKOFF_SECONDS)
                    continue
                raise
        return None

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        timespan = self._timespan()
        queries = self.queries
        consecutive_throttle = 0
        for index, original in enumerate(queries):
            clean, iso, gdelt_lang = resolve_query_language(original)
            if not clean:
                continue
            # Space requests out (after the first) to respect GDELT's rate limit.
            if index > 0:
                time.sleep(_PACING_SECONDS)
            params = {
                "query": self._build_query(clean, gdelt_lang),
                "mode": "ArtList",
                "format": "json",
                "maxrecords": _PER_QUERY_LIMIT,
                "timespan": timespan,
                "sort": "DateDesc",
            }
            try:
                data = self._request(params)
            except _RateLimited:
                # Sustained throttle: count it, and bail the whole run once it
                # repeats — the remaining queries would only fail the same way.
                consecutive_throttle += 1
                logger.warning("GDELT rate-limited on query: %s", original)
                if consecutive_throttle >= _MAX_CONSECUTIVE_THROTTLE:
                    remaining = len(queries) - index - 1
                    logger.warning(
                        "GDELT is rate-limiting us; skipping %d remaining "
                        "quer%s this run", remaining, "y" if remaining == 1 else "ies",
                    )
                    break
                continue
            except Exception as exc:
                logger.warning("GDELT query failed: %s (%s)", original, exc)
                continue
            # A clean response means we're under the limit again — reset the breaker.
            consecutive_throttle = 0
            if not data:
                continue

            articles = data.get("articles") or []
            for art in articles:
                url = art.get("url")
                title = (art.get("title") or "").strip()
                if not (url or title):
                    continue
                published = _parse_seendate(art.get("seendate"))
                if since and published and published <= since:
                    continue
                # Prefer the language we resolved from the query (authoritative
                # for the search-language rule); fall back to GDELT's per-article
                # language name when the query was unfiltered.
                lang = iso or (art.get("language") or "").strip().lower() or None
                items.append(
                    RawItem(
                        source=self.name,
                        track="api",
                        external_id=url or title,
                        url=url,
                        author=art.get("domain"),
                        title=title or None,
                        content=title or "",
                        language=lang,
                        published_at=published,
                        media_urls=[art["socialimage"]] if art.get("socialimage") else [],
                        raw={
                            "query": original,
                            "query_lang": gdelt_lang,
                            "domain": art.get("domain"),
                            "sourcecountry": art.get("sourcecountry"),
                            "gdelt_language": art.get("language"),
                        },
                    )
                )
        logger.info(
            "GDELT fetched %d items for %d queries", len(items), len(self.queries)
        )
        return items
