"""Tests for the .env secrets editor — especially the custom-source key rules.

Every test redirects ``_env_path`` to a tmp file so the real repo ``.env`` is
never touched.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def envfile(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr("nexus.envstore._env_path", lambda: path)
    return path


def test_write_and_read_custom_key(envfile):
    from nexus.envstore import (
        custom_source_env_name,
        custom_source_key_configured,
        read_env_value,
        update_env,
    )

    update_env({custom_source_env_name(5): "secret123"})
    assert read_env_value("CUSTOM_SOURCE_5_KEY") == "secret123"
    assert custom_source_key_configured(5) is True


def test_clearing_custom_key_removes_the_line(envfile):
    from nexus.envstore import custom_source_env_name, read_env_value, update_env

    update_env({custom_source_env_name(5): "secret123"})
    update_env({custom_source_env_name(5): ""})
    assert read_env_value("CUSTOM_SOURCE_5_KEY") is None
    assert "CUSTOM_SOURCE_5_KEY" not in envfile.read_text(encoding="utf-8")


def test_clearing_standing_key_keeps_the_line(envfile):
    from nexus.envstore import read_env_value, update_env

    update_env({"ANTHROPIC_API_KEY": "abc"})
    update_env({"ANTHROPIC_API_KEY": ""})
    text = envfile.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=" in text  # line preserved for the standing key
    assert read_env_value("ANTHROPIC_API_KEY") is None


def test_unlisted_key_is_rejected(envfile):
    from nexus.envstore import read_env_value, update_env

    update_env({"EVIL_KEY": "x"})
    assert read_env_value("EVIL_KEY") is None


def test_value_with_spaces_roundtrips(envfile):
    from nexus.envstore import custom_source_env_name, read_env_value, update_env

    update_env({custom_source_env_name(1): "a b c"})
    assert read_env_value("CUSTOM_SOURCE_1_KEY") == "a b c"


def test_custom_source_env_name_is_derived_from_int():
    from nexus.envstore import custom_source_env_name

    assert custom_source_env_name(7) == "CUSTOM_SOURCE_7_KEY"
    assert custom_source_env_name("9") == "CUSTOM_SOURCE_9_KEY"


def test_libretranslate_url_is_writable_and_shown_back(envfile):
    """The translation endpoint is allow-listed so the dashboard can set it, and
    (unlike a secret) its value is readable back for display."""
    from nexus.envstore import read_env_value, update_env

    update_env({"LIBRETRANSLATE_URL": "https://libretranslate.com"})
    assert read_env_value("LIBRETRANSLATE_URL") == "https://libretranslate.com"


def test_clearing_libretranslate_url_keeps_the_line(envfile):
    """Clearing it disables the HTTP backend but leaves the .env layout intact
    (it is a standing key, not a per-source key)."""
    from nexus.envstore import read_env_value, update_env

    update_env({"LIBRETRANSLATE_URL": "https://libretranslate.com"})
    update_env({"LIBRETRANSLATE_URL": ""})
    text = envfile.read_text(encoding="utf-8")
    assert "LIBRETRANSLATE_URL=" in text
    assert read_env_value("LIBRETRANSLATE_URL") is None


def test_settings_reads_libretranslate_url_and_enabled_flag():
    """Settings surfaces the URL and the derived enabled flag the UI shows."""
    from nexus.config import Settings

    off = Settings(libretranslate_url=None)
    assert off.libretranslate_enabled is False

    on = Settings(libretranslate_url="https://libretranslate.com")
    assert on.libretranslate_enabled is True
    assert on.libretranslate_url == "https://libretranslate.com"
