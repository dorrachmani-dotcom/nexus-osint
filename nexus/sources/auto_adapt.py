"""Auto-Adapt — self-healing field mapping for user-defined API sources.

A custom API source (see ``nexus/sources/custom.py``) is driven by a stored
field mapping: where the list of items lives in the JSON response, and which
field is the title/text/link/author/date. APIs change their response shape over
time; when that happens the source keeps responding (HTTP 200, valid JSON) but
the now-stale mapping extracts **zero** items — a silent failure the analyst may
never notice.

Auto-Adapt closes that gap. The collector records, per source, how many
consecutive scans were "responsive but empty". Once that crosses a small
threshold, this module asks the analyst's AI provider (reusing the same planner
that set the source up originally) to re-map the fields from the latest raw
response — then applies **only** the mapping fields, never the connection or
auth settings the analyst configured.

Guardrails (this is opt-in, cost-sensitive automation):
  * AI-only: with no provider connected it is a no-op (returns a clear status).
  * Threshold: only after ``DRIFT_THRESHOLD`` consecutive empty-but-responsive
    scans — a single transient blip never triggers it.
  * Cooldown: at most one re-map per source per ``COOLDOWN_HOURS`` — bounds the
    AI spend to roughly one call per source per cooldown window.
  * Needs a sample: re-maps from the stored ``last_sample``; without one it
    waits rather than guessing.
  * Never raises: any failure logs and returns ``{"adapted": False, ...}`` so a
    scan is never interrupted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from nexus.analysis.providers import get_provider
from nexus.analysis.source_planner import plan_source
from nexus.config import Settings, get_settings
from nexus.storage import apply_custom_source_remap, get_custom_source

logger = logging.getLogger("nexus.sources.auto_adapt")

# Consecutive responsive-but-empty scans before we attempt an AI re-map.
DRIFT_THRESHOLD = 2
# Don't re-map the same source more than once per this many hours (cost guard).
COOLDOWN_HOURS = 6


def _cooldown_active(last_adapt_at: str | None) -> bool:
    """True if the source was auto-adapted within the cooldown window."""
    if not last_adapt_at:
        return False
    try:
        when = datetime.fromisoformat(last_adapt_at.replace("Z", "+00:00"))
    except ValueError:
        # SQLite datetime('now') has no offset; treat as UTC.
        try:
            when = datetime.strptime(last_adapt_at, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when < timedelta(hours=COOLDOWN_HOURS)


def maybe_adapt(conn, source_id: int, settings: Settings | None = None) -> dict:
    """Re-map a drifting custom source if it qualifies. Never raises.

    Returns a small status dict, e.g. ``{"adapted": True, "changed": [...]}`` or
    ``{"adapted": False, "reason": "..."}``. The caller (the collector) may
    re-fetch the source once after a successful adapt so the fix takes effect in
    the same scan.
    """
    settings = settings or get_settings()
    try:
        row = get_custom_source(conn, source_id)
        if row is None:
            return {"adapted": False, "reason": "source not found"}

        count = int(row.get("consecutive_empty") or 0)
        if count < DRIFT_THRESHOLD:
            return {"adapted": False, "reason": "below drift threshold"}
        if _cooldown_active(row.get("last_adapt_at")):
            return {"adapted": False, "reason": "cooldown active"}
        if get_provider(settings) is None:
            return {"adapted": False, "reason": "no AI provider connected"}
        sample = row.get("last_sample")
        if not sample:
            return {"adapted": False, "reason": "no response sample to learn from"}

        hint = (
            f"This source ('{row.get('name')}') stopped returning items even though "
            "the API still responds — its response shape likely changed. From the "
            "sample below, work out the CURRENT items_path and field mappings. "
            "Keep the existing auth/base_url; only the response-mapping fields matter."
        )
        result = plan_source(sample=sample, hint=hint, settings=settings)
        if not result.get("ok"):
            return {"adapted": False, "reason": result.get("error", "planner failed")}

        cfg = result.get("config") or {}
        changed = [
            f for f in ("items_path", "map_title", "map_content", "map_url",
                        "map_author", "map_published")
            if f in cfg and (cfg.get(f) or None) != (row.get(f) or None)
        ]
        apply_custom_source_remap(conn, source_id, cfg)
        logger.info(
            "Auto-Adapt re-mapped custom source %s ('%s'); changed=%s",
            source_id, row.get("name"), changed,
        )
        return {"adapted": True, "changed": changed}
    except Exception as exc:  # never let self-healing break a scan
        logger.exception("Auto-Adapt failed for custom source %s", source_id)
        return {"adapted": False, "reason": f"error: {exc}"}
