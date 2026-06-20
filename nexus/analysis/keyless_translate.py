"""Keyless English-translation pass.

A lightweight companion to the AI analysis pass. It guarantees the dashboard
feed reads in English even when NO AI provider is configured, by rendering
non-English items into English with the keyless backends in
:mod:`nexus.translate` (LibreTranslate over HTTP, or an optional offline Argos
package) and storing the result in the existing ``Analysis.translation`` slot.

Design constraints (mirroring the other passes):

  * Graceful degradation — when no keyless backend is available this is a silent
    no-op; items keep their original language and the feed shows the original.
  * Non-destructive — only items WITHOUT an existing translation are processed,
    so a higher-quality AI translation is never overwritten (enforced again at
    the storage layer).
  * Budget guard — reuses ANALYSIS_MAX_ITEMS_PER_RUN as the per-run cap.
  * English-only target — the rendering is always English, stamped with the
    backend name (``libretranslate`` / ``argos``) so its provenance is visible.
"""

from __future__ import annotations

import logging

from nexus.analysis.prefilter import should_analyze
from nexus.config import Settings, get_settings
from nexus.db import get_connection
from nexus.lang import detect_language
from nexus.storage import pending_keyless_translation, save_keyless_translation
from nexus.translate import (
    BACKEND_ARGOS,
    BACKEND_LIBRETRANSLATE,
    keyless_backend_available,
    translate_to_english,
)

logger = logging.getLogger("nexus.translate")


def _resolve_source_lang(row: dict) -> str | None:
    """ISO code for the item's language: the stored value if usable, else a
    script-based guess. Returns ``None`` when it cannot be determined."""
    declared = (row.get("language") or "").strip().lower()
    if declared:
        # Stored languages are short ISO-ish codes; normalise "en-US" -> "en".
        return declared.split("-")[0]
    return detect_language(" ".join(filter(None, [row.get("title"), row.get("content")])))


def translate_pending(settings: Settings | None = None) -> dict:
    """Render the backlog of un-translated non-English items into English.

    Returns a small stats dict. No-op (enabled=False) when no keyless backend is
    available. Safe to call unconditionally and on every scan.
    """
    settings = settings or get_settings()
    if not keyless_backend_available(settings):
        return {"enabled": False, "translated": 0}

    with get_connection() as conn:
        queue = pending_keyless_translation(
            conn, limit=settings.analysis_max_items_per_run
        )

    target = settings.translation_target_lang or "English"
    translated = 0
    skipped = 0
    errors = 0
    for row in queue:
        title = row.get("title")
        content = row.get("content")
        # Skip noise the same way the AI pass does (don't burn calls on it).
        if not should_analyze(title, content, settings):
            skipped += 1
            continue

        src = _resolve_source_lang(row)
        # Already English (or unknowable script) -> leave it; the feed shows the
        # original. Only spend a call on clearly non-English material.
        if src in (None, "en"):
            skipped += 1
            continue

        text = content or title or ""
        try:
            english = translate_to_english(text, source_lang=src, settings=settings)
        except Exception:
            # translate_to_english already degrades to None, but belt-and-braces:
            # one bad item must never abort the pass.
            errors += 1
            logger.debug("Keyless translation raised for item %s", row.get("id"),
                         exc_info=True)
            continue

        if not english:
            # No backend reachable for this item right now; try again next scan.
            continue

        # Stamp the backend that produced it. LibreTranslate is preferred and
        # tried first; if it was unavailable the text came from Argos.
        model = (
            BACKEND_LIBRETRANSLATE
            if settings.libretranslate_enabled
            else BACKEND_ARGOS
        )
        try:
            with get_connection() as conn:
                wrote = save_keyless_translation(
                    conn, int(row["id"]), english, target, model
                )
            if wrote:
                translated += 1
        except Exception:
            errors += 1
            logger.exception("Saving keyless translation failed for item %s",
                             row.get("id"))

    stats = {
        "enabled": True,
        "translated": translated,
        "skipped": skipped,
        "errors": errors,
        "queued": len(queue),
    }
    logger.info("Keyless translation pass complete: %s", stats)
    return stats
