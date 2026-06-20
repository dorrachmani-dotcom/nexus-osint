"""Tests for the RSS source — focused on the language-tag normalisation that
feeds the keyless-translation pass. A feed/entry language like ``en-GB`` must
collapse to the short ISO code ``en`` the translation pass expects, and absent
tags must yield ``None`` so the pass falls back to script-based detection.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en", "en"),
        ("en-GB", "en"),
        ("EN-US", "en"),
        ("ru-RU", "ru"),
        ("AR", "ar"),
        ("  fr-CA  ", "fr"),
        ("", None),
        (None, None),
        ("-", None),
    ],
)
def test_normalize_lang(raw, expected):
    from nexus.sources.rss import _normalize_lang

    assert _normalize_lang(raw) == expected
