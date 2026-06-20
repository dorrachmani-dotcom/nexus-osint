"""Collection orchestration.

Trigger-based by design (no 24/7 scraping): `scan()` runs on a manual trigger
from the dashboard, and `boot_sync()` runs once at startup to fill gaps. Each
source is isolated — one failing source never aborts the run (graceful
degradation), and its error is reported back in the per-source stats.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from nexus.analysis.claude_core import analyze_pending
from nexus.analysis.keyless_translate import translate_pending
from nexus.analysis.requirements import score_pending
from nexus.config import Settings, get_settings
from nexus.db import get_connection
from nexus.sources.auto_adapt import maybe_adapt
from nexus.sources.base import Source
from nexus.sources.custom import CustomApiSource, load_custom_sources
from nexus.sources.freesearch import GoogleNewsSource, RedditSearchSource
from nexus.sources.gdelt import GdeltSource
from nexus.sources.google_cse import GoogleCseSource
from nexus.sources.reddit import RedditSource
from nexus.sources.rss import RSSSource
from nexus.sources.serpapi import SerpApiSource
from nexus.sources.telegram import TelegramSource
from nexus.sources.twitter import TwitterSource
from nexus.storage import (
    get_source_watermark,
    match_watchlists,
    record_custom_source_health,
    set_source_watermark,
    upsert_item,
)

logger = logging.getLogger("nexus.collector")


class Collector:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # Registry of known sources. API-only for v1; more plug in here later.
        # Each is self-gating via is_available(); unconfigured ones are skipped.
        self.sources: list[Source] = [
            RSSSource(self.settings),
            # Keyless search sources — work out of the box, no API key required.
            GoogleNewsSource(self.settings),
            # GDELT: worldwide, multilingual news monitoring (search-language =
            # result-language). The backbone of topic/entity monitoring.
            GdeltSource(self.settings),
            RedditSearchSource(self.settings),
            # Key-gated sources — self-skip when their credentials are absent.
            SerpApiSource(self.settings),
            GoogleCseSource(self.settings),
            RedditSource(self.settings),
            TwitterSource(self.settings),
            TelegramSource(self.settings),
        ]
        # Serialize scans so Boot Sync and a manual scan never collide.
        self._lock = threading.Lock()

    def available_sources(self) -> list[Source]:
        # Built-in sources plus any user-defined custom API sources. The latter
        # are loaded fresh each call so a source added from the dashboard is
        # picked up on the next scan without a restart.
        custom = load_custom_sources(self.settings)
        return [s for s in (*self.sources, *custom) if s.is_available()]

    def scan(self) -> dict:
        """Fetch from every available source and persist. Returns per-source stats."""
        with self._lock:
            return self._scan()

    def _scan(self) -> dict:
        stats: dict[str, dict] = {}
        available = self.available_sources()
        if not available:
            logger.info("Scan requested but no sources are available.")
            return {"_note": "no sources available — configure RSS_FEEDS or API keys"}

        # Stamp the run's start BEFORE fetching: items published during the scan
        # must be visible to the next run, so a successful source advances its
        # watermark to this instant (never to "now after fetching").
        scan_started = datetime.now(timezone.utc)
        # Earliest watermark across sources drives the UI's "since" line; None
        # (a first run for any source) means we did a full window-bounded pull.
        prev_watermarks: list[datetime] = []
        total_new = 0

        for source in available:
            # First run for this source -> watermark is None -> full pull.
            try:
                with get_connection() as conn:
                    watermark = get_source_watermark(conn, source.name)
            except Exception:
                logger.exception("Reading watermark for '%s' failed", source.name)
                watermark = None

            try:
                items = source.fetch(since=watermark)
            except Exception as exc:  # source-level failure: isolate it
                logger.exception("Source '%s' failed", source.name)
                stats[source.name] = {"error": str(exc)}
                # Keep the old watermark untouched so nothing is skipped next run.
                continue

            try:
                new, alerts = self._persist(source.name, items, scan_started)
            except Exception as exc:
                logger.exception("Persisting items from '%s' failed", source.name)
                stats[source.name] = {"error": str(exc), "fetched": len(items)}
                continue

            if watermark is not None:
                prev_watermarks.append(watermark)
            total_new += new
            stats[source.name] = {"fetched": len(items), "new": new, "alerts": alerts}

            # Auto-Adapt: a custom API that responded but yielded nothing may have
            # drifted; self-heal its field mapping (cost-guarded, AI-only).
            if isinstance(source, CustomApiSource):
                try:
                    self._adapt_custom(source, len(items), stats)
                except Exception:
                    logger.exception("Auto-Adapt handling failed for '%s'", source.name)

        # Enrich freshly collected items. No-op (and harmless) without a provider.
        try:
            stats["_analysis"] = analyze_pending(self.settings)
        except Exception as exc:
            logger.exception("Analysis pass failed")
            stats["_analysis"] = {"error": str(exc)}

        # Keyless English-translation fallback: guarantees the feed reads English
        # even with no AI provider. Runs AFTER analysis so it only fills items the
        # AI pass left untranslated; non-destructive to any AI translation. No-op
        # (and harmless) when no keyless backend is configured/installed.
        try:
            stats["_translation"] = translate_pending(self.settings)
        except Exception as exc:
            logger.exception("Keyless translation pass failed")
            stats["_translation"] = {"error": str(exc)}

        # Score items against standing intelligence requirements. No-op without a
        # provider or when no requirements are defined.
        try:
            stats["_requirements"] = score_pending(self.settings)
        except Exception as exc:
            logger.exception("Requirement scoring pass failed")
            stats["_requirements"] = {"error": str(exc)}

        # Incremental-update summary for the UI: how much is new and the
        # previous run's watermark we measured against (None on a first run).
        since = min(prev_watermarks) if prev_watermarks else None
        stats["_update"] = {
            "new": total_new,
            "since": since.isoformat() if since else None,
        }

        logger.info("Scan complete: %s", stats)
        return stats

    def _persist(
        self, source_name: str, items: list, watermark: datetime | None = None
    ) -> tuple[int, int]:
        """Store fetched items, run watchlists, and advance the source watermark.

        Returns ``(new_count, alert_count)``. Raises on a DB failure (caller
        isolates). ``watermark`` is the scan-start time: on a successful persist
        the source's watermark advances to it so the next scan only fetches what
        is newer. A source that raised never reaches here, so its watermark
        stays put and nothing is skipped.
        """
        new = 0
        alerts = 0
        when = watermark or datetime.now(timezone.utc)
        with get_connection() as conn:
            for item in items:
                _id, is_new = upsert_item(conn, item)
                new += int(is_new)
                if is_new:
                    # Alert on fresh content only, against current watchlists.
                    # A pathological user watchlist (e.g. a bad regex) must never
                    # abort the persist and discard items already stored this
                    # loop — isolate it and keep collecting.
                    try:
                        text = " ".join(filter(None, [item.title, item.content]))
                        alerts += match_watchlists(conn, _id, text)
                    except Exception:
                        logger.exception(
                            "Watchlist matching failed for item %s (source '%s')",
                            _id,
                            source_name,
                        )
            set_source_watermark(conn, source_name, when)
        return new, alerts

    def _adapt_custom(self, source: CustomApiSource, mapped: int, stats: dict) -> None:
        """Record a custom source's drift health and, if it has drifted, ask the
        AI planner to re-map it — then re-fetch once so the fix lands this scan."""
        with get_connection() as conn:
            count = record_custom_source_health(
                conn,
                source.id,
                responded=source.responded,
                mapped=mapped,
                sample=source.last_sample,
            )
        # Nothing to do unless the API answered but mapped nothing repeatedly.
        if mapped > 0 or count < 1:
            return

        with get_connection() as conn:
            result = maybe_adapt(conn, source.id, self.settings)
        stats[source.name]["auto_adapt"] = result
        if not result.get("adapted"):
            return

        # Re-build the source from its freshly re-mapped config and try once more,
        # so a successful adapt yields data without waiting for the next scan.
        try:
            from nexus.storage import get_custom_source

            with get_connection() as conn:
                cfg = get_custom_source(conn, source.id)
            if not cfg:
                return
            healed = CustomApiSource(cfg, self.settings)
            if not healed.is_available():
                return
            items = healed.fetch()
            new, alerts = self._persist(healed.name, items)
            with get_connection() as conn:
                record_custom_source_health(
                    conn, healed.id,
                    responded=healed.responded, mapped=len(items),
                    sample=healed.last_sample,
                )
            stats[source.name].update(
                {"fetched": len(items), "new": new, "alerts": alerts}
            )
            stats[source.name]["auto_adapt"]["refetched"] = len(items)
        except Exception:
            logger.exception("Re-fetch after Auto-Adapt failed for '%s'", source.name)

    def boot_sync(self) -> dict:
        """Silent gap-fill at startup. Same path as a manual scan for now."""
        logger.info("Boot sync starting…")
        return self.scan()
