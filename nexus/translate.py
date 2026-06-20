"""Keyless local translation fallback — guarantee an English feed without an AI key.

When no AI provider is configured, the analysis pass never produces a
translation, so non-English items would otherwise surface in their original
language — violating the product's English-first feed requirement. This module
provides a lightweight, layered fallback that turns text into English using
backends that need NO API key.

Backends, tried in order; the first that yields text wins:

  (a) LibreTranslate over HTTP — endpoint from ``Settings.libretranslate_url``.
      NOTE ON LICENSING: LibreTranslate is AGPL-licensed. We DO NOT import,
      bundle, link, or vendor any of its code. It is used ONLY as an external
      network service via its public HTTP API (POST /translate), exactly like
      any other third-party web API — so the AGPL's source-distribution
      obligations are not triggered for this project. The operator points us at
      a self-hosted or public instance of their choosing.

  (b) Argos Translate — an OPTIONAL, offline, MIT-licensed Python package. It is
      imported behind a guarded try/except; if it (or its language packages) are
      not installed, this backend is simply unavailable. Nothing here installs
      or downloads models.

Graceful degradation is absolute: any timeout, network error, unreachable
service, missing package, or unexpected failure results in ``None`` — never an
exception. When every backend returns ``None`` the caller leaves the item in its
original language and the feed shows the original text with its language tag.
"""

from __future__ import annotations

import logging

from nexus.config import Settings, get_settings
from nexus.lang import detect_language
from nexus.netguard import safe_http_url

logger = logging.getLogger("nexus.translate")

# Backend identifiers stamped onto Analysis.model so the UI / operator can see
# which keyless engine produced a given rendering.
BACKEND_LIBRETRANSLATE = "libretranslate"
BACKEND_ARGOS = "argos"

# Keep keyless requests bounded — a slow service must never stall a scan.
_HTTP_TIMEOUT_SECONDS = 15.0
# LibreTranslate accepts "auto" for source-language auto-detection.
_AUTO = "auto"


def _libretranslate(text: str, source_lang: str, settings: Settings) -> str | None:
    """Translate via a LibreTranslate HTTP instance, or ``None`` on any problem."""
    endpoint = (settings.libretranslate_url or "").strip()
    if not endpoint:
        return None

    url = endpoint.rstrip("/") + "/translate"

    # The endpoint is operator-supplied config, but we still route it through the
    # shared SSRF guard so a misconfigured/hostile value can't be used to probe
    # internal hosts. A self-hosted instance on a private/loopback address will
    # be (correctly) refused by the guard; operators wanting that must expose it
    # on a routable address.
    ok, reason = safe_http_url(url)
    if not ok:
        logger.warning("LibreTranslate endpoint refused by URL safety check: %s", reason)
        return None

    try:
        import httpx  # always installed (used across the codebase)
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        return None

    try:
        resp = httpx.post(
            url,
            json={
                "q": text,
                "source": source_lang or _AUTO,
                "target": "en",
                "format": "text",
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # Timeout, connection error, bad status, non-JSON body — all degrade
        # quietly to "no translation available".
        logger.debug("LibreTranslate request failed; skipping.", exc_info=True)
        return None

    translated = (data or {}).get("translatedText")
    if isinstance(translated, str) and translated.strip():
        return translated.strip()
    return None


def _argos(text: str, source_lang: str) -> str | None:
    """Translate via the offline Argos Translate package, if installed.

    Optional + guarded: a missing package, missing language pair, or any runtime
    error degrades to ``None``.
    """
    try:
        import argostranslate.translate as argos  # optional offline backend
    except ImportError:
        return None

    src = (source_lang or "").strip().lower()
    if not src or src == _AUTO:
        # Argos needs a concrete source language; fall back to our detector.
        src = detect_language(text) or ""
    if not src or src == "en":
        return None

    try:
        result = argos.translate(text, src, "en")
    except Exception:
        logger.debug("Argos translation failed; skipping.", exc_info=True)
        return None

    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def translate_to_english(
    text: str | None,
    source_lang: str | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Render ``text`` into English using keyless backends, or ``None``.

    ``source_lang`` is an ISO 639-1 code when known; when missing it is
    auto-detected via :mod:`nexus.lang` (and passed as "auto" to LibreTranslate).
    Returns ``None`` whenever no backend is available or all of them fail — the
    caller then leaves the item in its original language. Never raises.
    """
    if not text or not text.strip():
        return None

    settings = settings or get_settings()

    src = (source_lang or "").strip().lower()
    if not src:
        # Best-effort detection; LibreTranslate can also auto-detect on its own.
        src = detect_language(text) or _AUTO

    # Already English (or unknowable) -> nothing to do.
    if src == "en":
        return None

    # Backend (a): LibreTranslate over HTTP.
    out = _libretranslate(text, src, settings)
    if out:
        return out

    # Backend (b): offline Argos Translate, if the optional package is present.
    out = _argos(text, src)
    if out:
        return out

    return None


def keyless_backend_available(settings: Settings | None = None) -> bool:
    """True when at least one keyless backend could plausibly run.

    Used to skip the fallback pass entirely when nothing is configured/installed.
    LibreTranslate counts as available whenever a URL is set; Argos counts when
    its package imports. Never raises.
    """
    settings = settings or get_settings()
    if settings.libretranslate_enabled:
        return True
    try:
        import argostranslate.translate  # noqa: F401
        return True
    except Exception:
        return False
