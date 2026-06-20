"""The AI analysis pass.

Decoupled from collection: items are stored first, then `analyze_pending` walks
the backlog (newest first), runs each through the local prefilter, calls the
selected LLM provider (Anthropic, Gemini, OpenAI, or a local Ollama model), and
writes the result back. Design constraints baked in here:

  * Graceful degradation — no usable provider means this is a silent no-op;
    items keep their raw form and the rest of the system works unchanged.
  * Cache by content — only items without an analysis row are processed, so a
    re-scan never pays to analyse the same content twice.
  * Budget guard — ANALYSIS_MAX_ITEMS_PER_RUN caps how many items reach the
    model per run, bounding token spend.
  * Provider-agnostic — the backend is chosen at runtime (Settings/dashboard);
    this module never imports an SDK directly (see analysis.providers).
"""

from __future__ import annotations

import json
import logging
import re

from nexus.analysis.prefilter import should_analyze
from nexus.analysis.prompts import build_system_prompt, build_user_prompt
from nexus.analysis.providers import LLMProvider, get_provider
from nexus.config import Settings, get_settings
from nexus.db import get_connection
from nexus.models import Analysis, Party, ThreatLevel, _utcnow
from nexus.storage import pending_analysis, save_analysis

logger = logging.getLogger("nexus.analysis")

# Pull the first JSON object out of a model reply, tolerating stray prose.
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# Substrings that mark a rate-limit / quota error across providers. When one of
# these is hit we stop the run cleanly rather than hammering the API for every
# remaining item — the unanalysed items simply wait for the next scan.
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "ratelimit",
    "quota",
    "resource_exhausted",
    "resource exhausted",
    "too many requests",
    "insufficient_quota",
)


def _is_rate_limit(exc: Exception) -> bool:
    """Heuristic: does this exception look like a provider rate-limit/quota hit?"""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _coerce_threat(value) -> ThreatLevel:
    try:
        return ThreatLevel(str(value).strip().lower())
    except (ValueError, AttributeError):
        return ThreatLevel.NONE


def _coerce_party(value) -> Party:
    try:
        return Party(str(value).strip().lower())
    except (ValueError, AttributeError):
        return Party.UNKNOWN


_ENTITY_GROUP_KEYS = ("people", "organizations", "locations", "identifiers")


def _coerce_confidence(value) -> str | None:
    """Normalize the model's self-confidence to low/medium/high (or None)."""
    text = str(value or "").strip().lower()
    return text if text in {"low", "medium", "high"} else None


def _parse_entities(value) -> tuple[dict[str, list[str]], list[str]]:
    """Parse the model's ``entities`` field into (typed groups, flat list).

    Accepts the new typed object {people, organizations, locations, identifiers}
    or the legacy flat array. Always returns both shapes so storage/UI can use
    whichever they need. De-dupes the flat list while preserving order.
    """
    groups: dict[str, list[str]] = {k: [] for k in _ENTITY_GROUP_KEYS}
    flat: list[str] = []
    seen: set[str] = set()

    def _add(name: str, bucket: str | None) -> None:
        name = str(name).strip()
        if not name:
            return
        if bucket and bucket in groups:
            groups[bucket].append(name)
        key = name.lower()
        if key not in seen:
            seen.add(key)
            flat.append(name)

    if isinstance(value, dict):
        for bucket in _ENTITY_GROUP_KEYS:
            members = value.get(bucket) or []
            if not isinstance(members, list):
                members = [members]
            for m in members:
                _add(m, bucket)
        # Tolerate stray keys the model may add: fold them into the flat list.
        for extra_key, members in value.items():
            if extra_key in groups:
                continue
            if not isinstance(members, list):
                members = [members]
            for m in members:
                _add(m, None)
    elif isinstance(value, list):
        for m in value:
            _add(m, None)
    elif value:
        _add(value, None)

    return groups, flat


def _parse_response(text: str, settings: Settings, model: str) -> Analysis:
    """Turn a model reply into an Analysis, defensively."""
    match = _JSON_RE.search(text or "")
    data: dict = {}
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("Model returned non-JSON analysis; storing minimal record.")

    entity_groups, entities = _parse_entities(data.get("entities"))

    translation = (data.get("translation") or "").strip() or None
    confidence = _coerce_confidence(data.get("confidence"))

    return Analysis(
        threat_level=_coerce_threat(data.get("threat_level")),
        summary=(data.get("summary") or "").strip() or None,
        translation=translation,
        target_lang=settings.translation_target_lang,
        entities=entities,
        entity_groups=entity_groups,
        party=_coerce_party(data.get("party")),
        contradiction=bool(data.get("contradiction", False)),
        confidence=confidence,
        model=model,
        analyzed_at=_utcnow(),
    )


def _analyze_one(
    provider: LLMProvider, system_prompt: str, row: dict, settings: Settings
) -> Analysis:
    """Single model call for one item via the active provider."""
    text = provider.complete(
        system_prompt,
        build_user_prompt(row.get("title"), row.get("content"), row.get("language")),
        max_tokens=1024,
    )
    return _parse_response(text, settings, provider.model)


def analyze_pending(settings: Settings | None = None) -> dict:
    """Analyse the backlog of un-analysed items. Returns a small stats dict.

    Safe to call unconditionally: with no usable provider it reports
    enabled=False and does nothing else.
    """
    settings = settings or get_settings()
    provider = get_provider(settings)
    if provider is None:
        return {"enabled": False, "analyzed": 0, "skipped": 0}

    system_prompt = build_system_prompt(settings.translation_target_lang)

    with get_connection() as conn:
        queue = pending_analysis(conn, limit=settings.analysis_max_items_per_run)

    analyzed = 0
    skipped = 0
    errors = 0
    rate_limited = False
    for row in queue:
        if not should_analyze(row.get("title"), row.get("content"), settings):
            skipped += 1
            continue
        try:
            analysis = _analyze_one(provider, system_prompt, row, settings)
        except Exception as exc:
            if _is_rate_limit(exc):
                # Hit the provider's rate/quota cap (e.g. Gemini free tier).
                # Stop cleanly: remaining items stay in the backlog for the next
                # scan, and we don't keep hammering an API that's saying "stop".
                rate_limited = True
                logger.warning(
                    "Provider rate limit reached after %d items; pausing analysis "
                    "(the rest will be picked up on the next scan).",
                    analyzed,
                )
                break
            errors += 1
            logger.exception("Analysis failed for item %s", row.get("id"))
            continue
        # Each write in its own short transaction so a later failure can't
        # discard analyses already paid for.
        with get_connection() as conn:
            save_analysis(conn, int(row["id"]), analysis)
        analyzed += 1

    stats = {
        "enabled": True,
        "provider": provider.name,
        "analyzed": analyzed,
        "skipped": skipped,
        "errors": errors,
        "queued": len(queue),
        "rate_limited": rate_limited,
    }
    logger.info("Analysis pass complete: %s", stats)
    return stats
