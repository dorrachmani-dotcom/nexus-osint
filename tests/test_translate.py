"""Tests for the keyless local translation fallback.

Covers the layered backends in ``nexus.translate`` (httpx monkeypatched so no
real network is touched), the script-based language detector in ``nexus.lang``,
and the storage guarantee that the keyless pass never clobbers an existing
(AI-produced) translation.
"""

from __future__ import annotations

import httpx

from nexus import translate
from nexus.config import Settings
from nexus.lang import detect_language, is_english, iso_to_name, name_to_iso

# --- nexus.lang -------------------------------------------------------------


def test_detect_language_latin_is_english():
    assert detect_language("The quick brown fox jumps over the lazy dog") == "en"
    assert is_english("Hello there, this is plainly English text.") is True


def test_detect_language_cyrillic():
    assert detect_language("Это сообщение на русском языке") == "ru"
    assert is_english("Это сообщение на русском языке") is False


def test_detect_language_insufficient_signal_is_none():
    assert detect_language("") is None
    assert detect_language("12 34 !!") is None
    # Too few letters -> unknown -> treated as English (don't waste a call).
    assert is_english("ok") is True


def test_iso_name_roundtrip():
    assert iso_to_name("ru") == "Russian"
    assert name_to_iso("Russian") == "ru"
    assert name_to_iso("nonsense") is None


# --- LibreTranslate HTTP backend -------------------------------------------


def _settings(url: str | None) -> Settings:
    # Build Settings explicitly so the test never reads the repo .env.
    return Settings(libretranslate_url=url)


def test_libretranslate_success(monkeypatch):
    """A well-formed LibreTranslate response yields the English string."""

    def fake_post(url, json=None, timeout=None):
        assert url.endswith("/translate")
        assert json["target"] == "en"
        request = httpx.Request("POST", url)
        return httpx.Response(
            200, json={"translatedText": "Hello world"}, request=request
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    # Use a public, routable host so the SSRF guard passes.
    out = translate.translate_to_english(
        "Привет мир", source_lang="ru", settings=_settings("https://libretranslate.com")
    )
    assert out == "Hello world"


def test_libretranslate_unreachable_returns_none(monkeypatch):
    """A connection error degrades quietly to None (never raises)."""

    def boom(url, json=None, timeout=None):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(httpx, "post", boom)
    out = translate.translate_to_english(
        "Привет мир", source_lang="ru", settings=_settings("https://libretranslate.com")
    )
    assert out is None


def test_no_backend_configured_returns_none(monkeypatch):
    """No LibreTranslate URL and no Argos package -> None."""
    # Ensure the optional Argos import path reports unavailable.
    monkeypatch.setattr(translate, "_argos", lambda text, src: None)
    out = translate.translate_to_english(
        "Привет мир", source_lang="ru", settings=_settings(None)
    )
    assert out is None


def test_already_english_skips_backends(monkeypatch):
    """English source is never sent to a backend (returns None, leave as-is)."""

    def fail(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("backend should not be called for English text")

    monkeypatch.setattr(httpx, "post", fail)
    out = translate.translate_to_english(
        "Plain English content here", settings=_settings("https://libretranslate.com")
    )
    assert out is None


def test_ssrf_guard_blocks_loopback(monkeypatch):
    """A loopback LibreTranslate URL is refused by the SSRF guard -> None."""

    def fail(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("must not fetch a refused URL")

    monkeypatch.setattr(httpx, "post", fail)
    out = translate.translate_to_english(
        "Привет мир", source_lang="ru", settings=_settings("http://127.0.0.1:5000")
    )
    assert out is None


# --- storage guard: keyless never clobbers an AI translation ----------------


def test_keyless_does_not_clobber_ai_translation(temp_db):
    from nexus.models import Analysis, RawItem, ThreatLevel
    from nexus.storage import save_analysis, save_keyless_translation, upsert_item

    with temp_db() as conn:
        item_id, _ = upsert_item(conn, RawItem(source="rss", content="Привет мир"))
        # An AI pass already produced a high-quality translation.
        save_analysis(
            conn,
            item_id,
            Analysis(
                threat_level=ThreatLevel.NONE,
                translation="AI rendering",
                target_lang="English",
                model="claude-opus-4-7",
            ),
        )
        # The keyless pass must refuse to overwrite it.
        wrote = save_keyless_translation(
            conn, item_id, "keyless rendering", "English", "libretranslate"
        )
        assert wrote is False
        row = conn.execute(
            "SELECT translation, model FROM analyses WHERE item_id = ?", (item_id,)
        ).fetchone()
        assert row["translation"] == "AI rendering"
        assert row["model"] == "claude-opus-4-7"


def test_keyless_fills_when_no_translation(temp_db):
    from nexus.models import RawItem
    from nexus.storage import save_keyless_translation, upsert_item

    with temp_db() as conn:
        item_id, _ = upsert_item(conn, RawItem(source="rss", content="Привет мир"))
        # No analysis row yet -> keyless creates a minimal one.
        wrote = save_keyless_translation(
            conn, item_id, "Hello world", "English", "libretranslate"
        )
        assert wrote is True
        row = conn.execute(
            "SELECT translation, target_lang, model FROM analyses WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        assert row["translation"] == "Hello world"
        assert row["target_lang"] == "English"
        assert row["model"] == "libretranslate"

        # A second keyless call must NOT clobber the first.
        again = save_keyless_translation(
            conn, item_id, "different", "English", "argos"
        )
        assert again is False


def test_pending_keyless_translation_lists_untranslated(temp_db):
    from nexus.models import Analysis, RawItem, ThreatLevel
    from nexus.storage import (
        pending_keyless_translation,
        save_analysis,
        upsert_item,
    )

    with temp_db() as conn:
        # Item A: no analysis at all -> needs translation.
        a, _ = upsert_item(conn, RawItem(source="rss", content="Привет"))
        # Item B: analysis with a translation -> excluded.
        b, _ = upsert_item(conn, RawItem(source="rss", content="Bonjour"))
        save_analysis(
            conn, b,
            Analysis(threat_level=ThreatLevel.NONE, translation="Hello", model="x"),
        )
        # Item C: analysis but NO translation -> needs translation.
        c, _ = upsert_item(conn, RawItem(source="rss", content="Hola"))
        save_analysis(
            conn, c,
            Analysis(threat_level=ThreatLevel.NONE, summary="sum", model="x"),
        )

        ids = {r["id"] for r in pending_keyless_translation(conn)}
        assert a in ids
        assert c in ids
        assert b not in ids


# --- end-to-end pass --------------------------------------------------------


def test_translate_pending_noop_without_backend(temp_db, monkeypatch):
    """With no keyless backend available, the pass is a silent no-op."""
    from nexus.analysis import keyless_translate

    monkeypatch.setattr(
        keyless_translate, "keyless_backend_available", lambda settings=None: False
    )
    stats = keyless_translate.translate_pending(_settings(None))
    assert stats == {"enabled": False, "translated": 0}
