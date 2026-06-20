"""Unit tests for query-language resolution (the search-language rule).

These pin the core product behaviour: the language a query is written in (or an
explicit ``lang:`` tag) decides the language filter applied to results. No
network — pure string/script logic.
"""

from __future__ import annotations

from nexus.lang import (
    detect_script_iso,
    parse_lang_tag,
    resolve_query_language,
)


# ----------------------------------------------------------- explicit lang tag
def test_parse_lang_tag_colon_and_equals():
    assert parse_lang_tag("lang:es Neymar") == ("es", "Neymar")
    assert parse_lang_tag("lang=zh Google") == ("zh", "Google")


def test_parse_lang_tag_accepts_name_and_3letter_alias():
    assert parse_lang_tag("lang:spanish Messi") == ("es", "Messi")
    assert parse_lang_tag("lang:zho 内马尔") == ("zh", "内马尔")


def test_parse_lang_tag_unknown_code_is_left_untouched():
    # A typo must not silently eat the token or filter everything out.
    assert parse_lang_tag("lang:zz hello") == (None, "lang:zz hello")


def test_parse_lang_tag_absent():
    assert parse_lang_tag("just a query") == (None, "just a query")


# ----------------------------------------------------------- script detection
def test_detect_chinese():
    assert detect_script_iso("内马尔") == "zh"


def test_detect_japanese_wins_over_han_via_kana():
    # Japanese mixes Han + kana; kana must disambiguate it from Chinese.
    assert detect_script_iso("ネイマール選手") == "ja"


def test_detect_arabic_cyrillic_hebrew_greek():
    assert detect_script_iso("نيمار") == "ar"
    assert detect_script_iso("Неймар") == "ru"
    # Hebrew-script sample built from code points so no literal Hebrew ships in source.
    _he = chr(0x05d2) + chr(0x05d5) + chr(0x05d2) + chr(0x05dc)
    assert detect_script_iso(_he) == "he"
    assert detect_script_iso("Νεϊμάρ") == "el"


def test_detect_latin_is_unresolved():
    assert detect_script_iso("Neymar") is None
    assert detect_script_iso("climate change") is None


# ----------------------------------------------------------- full resolution
def test_resolve_tag_beats_autodetect():
    clean, iso, gdelt = resolve_query_language("lang:fr Neymar")
    assert (clean, iso, gdelt) == ("Neymar", "fr", "french")


def test_resolve_autodetects_when_no_tag():
    clean, iso, gdelt = resolve_query_language("内马尔")
    assert (clean, iso, gdelt) == ("内马尔", "zh", "chinese")


def test_resolve_latin_unfiltered():
    clean, iso, gdelt = resolve_query_language("Neymar")
    assert (clean, iso, gdelt) == ("Neymar", None, None)
