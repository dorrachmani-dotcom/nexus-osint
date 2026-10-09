"""Intelligence-Requirement (PIR) scoring pass.

Standing intelligence requirements are questions the analyst wants answered.
This pass scores each collected item for relevance to every enabled requirement
so the Intel view can rank material by how well it answers those questions.

Same design constraints as the analysis pass:

  * Graceful degradation — no usable provider, or no requirements defined, makes
    this a silent no-op.
  * Cache by pair — an (item, requirement) is scored at most once; the queue only
    contains items with at least one unscored enabled requirement.
  * Budget guard — ANALYSIS_MAX_ITEMS_PER_RUN caps items per run.
  * Provider-agnostic — uses the same selected backend as analysis.
"""

from __future__ import annotations

import json
import logging
import re

from nexus.analysis.claude_core import _is_rate_limit
from nexus.analysis.prefilter import should_analyze
from nexus.analysis.prompts import (
    build_requirements_system_prompt,
    build_requirements_user_prompt,
)
from nexus.analysis.providers import LLMProvider, get_provider
from nexus.config import Settings, get_settings
from nexus.db import get_connection
from nexus.storage import (
    pending_requirement_scoring,
    save_requirement_hit,
    unscored_requirements_for_item,
)

logger = logging.getLogger("nexus.analysis")

# Pull the first JSON array out of a model reply, tolerating stray prose.
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _clamp_score(value) -> int:
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return 0


def _parse_scores(text: str, valid_ids: set[int]) -> dict[int, dict]:
    """Map requirement id -> {score, rationale}, defensively.

    Unknown ids are dropped; malformed elements are ignored. Returns whatever
    could be parsed (callers default the rest to 0).
    """
    match = _JSON_ARRAY_RE.search(text or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.warning("Requirement scoring returned non-JSON; treating as empty.")
        return {}
    if not isinstance(data, list):
        return {}

    out: dict[int, dict] = {}
    for el in data:
        if not isinstance(el, dict):
            continue
        raw_id = el.get("id")
        if raw_id is None:
            continue
        try:
            rid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if rid not in valid_ids:
            continue
        rationale = (el.get("rationale") or "").strip() or None
        out[rid] = {"score": _clamp_score(el.get("score")), "rationale": rationale}
    return out


def _score_one(
    provider: LLMProvider,
    system_prompt: str,
    row: dict,
    requirements: list[dict],
) -> dict[int, dict]:
    valid_ids = {int(r["id"]) for r in requirements}
    text = provider.complete(
        system_prompt,
        build_requirements_user_prompt(
            row.get("title"), row.get("content"), row.get("language"), requirements
        ),
        max_tokens=1024,
    )
    return _parse_scores(text, valid_ids)


def score_pending(settings: Settings | None = None) -> dict:
    """Score the backlog of items against enabled requirements.

    Returns a small stats dict. No-op (enabled=False) when no provider is
    usable; no-op (scored=0) when no requirements are defined.
    """
    settings = settings or get_settings()
    provider = get_provider(settings)
    if provider is None:
        return {"enabled": False, "scored": 0}

    system_prompt = build_requirements_system_prompt(settings.translation_target_lang)

    with get_connection() as conn:
        queue = pending_requirement_scoring(
            conn, limit=settings.analysis_max_items_per_run
        )

    scored = 0
    hits = 0
    errors = 0
    rate_limited = False
    for row in queue:
        item_id = int(row["id"])
        with get_connection() as conn:
            reqs = unscored_requirements_for_item(conn, item_id)
        if not reqs:
            continue

        # Noise the analysis prefilter rejects gets a 0 across the board so it
        # leaves the queue instead of being re-evaluated (and re-charged) forever.
        if not should_analyze(row.get("title"), row.get("content"), settings):
            with get_connection() as conn:
                for r in reqs:
                    save_requirement_hit(
                        conn, item_id, int(r["id"]), 0, None, provider.model
                    )
            continue

        try:
            results = _score_one(provider, system_prompt, row, reqs)
        except Exception as exc:
            if _is_rate_limit(exc):
                rate_limited = True
                logger.warning(
                    "Provider rate limit reached after scoring %d items; pausing "
                    "(the rest will be picked up on the next scan).",
                    scored,
                )
                break
            errors += 1
            logger.exception("Requirement scoring failed for item %s", item_id)
            continue

        with get_connection() as conn:
            for r in reqs:
                rid = int(r["id"])
                res = results.get(rid, {"score": 0, "rationale": None})
                save_requirement_hit(
                    conn, item_id, rid, res["score"], res["rationale"], provider.model
                )
                if res["score"] >= 1:
                    hits += 1
        scored += 1

    stats = {
        "enabled": True,
        "provider": provider.name,
        "scored": scored,
        "hits": hits,
        "errors": errors,
        "queued": len(queue),
        "rate_limited": rate_limited,
    }
    logger.info("Requirement scoring pass complete: %s", stats)
    return stats
