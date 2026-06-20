"""Query-language detection for language-scoped collection.

Core product rule: **the language you search in is the language of the results
you get back.** If an analyst types a query in Chinese, they want Chinese-source
coverage only; the same holds for Arabic, Russian, and every other language.
The unified dashboard still renders everything in English (via the analysis
translation layer); the original-language text is preserved on each item and
shown when the analyst opens the source.

This module turns a raw query string into the language filter a source should
apply. Two inputs, in priority order:

  1. **Explicit tag** — a leading ``lang:<code>`` (or ``lang=<code>``) token on
     the query. This is the precise control for Latin-script languages that
     can't be told apart by their alphabet (English vs. Spanish vs. French).
     Example: ``lang:es Neymar`` collects Spanish coverage of Neymar.

  2. **Script auto-detection** — when no tag is present, the dominant Unicode
     script of the query implies the language. Typing a query in Chinese,
     Arabic, Cyrillic, etc. is itself an unambiguous signal, so this needs zero
     configuration and matches how a non-technical user actually works.

Latin script is intentionally left *unresolved* (returns ``None``): too many
languages share it to guess safely, so without an explicit ``lang:`` tag the
query runs unfiltered (broadest recall) and the English dashboard does the rest.

Nothing here raises; a value it can't classify simply yields ``None`` (no
filter), which is the graceful, recall-maximising default.
"""

from __future__ import annotations

import re
import unicodedata

# GDELT's ``sourcelang:`` operator accepts language *names* (and 3-letter
# codes). We standardise on names since they're unambiguous and documented.
# This table is the single source of truth mapping an ISO-639-1 code -> the
# GDELT language name. Add a row to teach the system a new language; both the
# explicit ``lang:<code>`` tag and script detection feed through it.
ISO_TO_GDELT: dict[str, str] = {
    "en": "english",
    "es": "spanish",
    "pt": "portuguese",
    "fr": "french",
    "de": "german",
    "it": "italian",
    "nl": "dutch",
    "sv": "swedish",
    "no": "norwegian",
    "da": "danish",
    "fi": "finnish",
    "pl": "polish",
    "cs": "czech",
    "tr": "turkish",
    "id": "indonesian",
    "vi": "vietnamese",
    "ro": "romanian",
    "hu": "hungarian",
    "zh": "chinese",
    "ja": "japanese",
    "ko": "korean",
    "ar": "arabic",
    "fa": "persian",
    "ur": "urdu",
    "he": "hebrew",
    "ru": "russian",
    "uk": "ukrainian",
    "bg": "bulgarian",
    "sr": "serbian",
    "el": "greek",
    "hi": "hindi",
    "bn": "bengali",
    "ta": "tamil",
    "th": "thai",
    "ka": "georgian",
    "hy": "armenian",
}

# Friendly aliases users might type after ``lang:`` (names, 3-letter codes).
_ALIAS_TO_ISO: dict[str, str] = {name: iso for iso, name in ISO_TO_GDELT.items()}
_ALIAS_TO_ISO.update(
    {
        "eng": "en", "spa": "es", "por": "pt", "fra": "fr", "fre": "fr",
        "deu": "de", "ger": "de", "ita": "it", "nld": "nl", "dut": "nl",
        "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "pol": "pl",
        "ces": "cs", "cze": "cs", "tur": "tr", "ind": "id", "vie": "vi",
        "ron": "ro", "rum": "ro", "hun": "hu", "zho": "zh", "chi": "zh",
        "jpn": "ja", "kor": "ko", "ara": "ar", "fas": "fa", "per": "fa",
        "urd": "ur", "heb": "he", "rus": "ru", "ukr": "uk", "bul": "bg",
        "srp": "sr", "ell": "el", "gre": "el", "hin": "hi", "ben": "bn",
        "tam": "ta", "tha": "th", "kat": "ka", "geo": "ka", "hye": "hy",
        "arm": "hy",
    }
)

# A leading explicit language tag: ``lang:es``, ``lang=zh``, ``lang: chinese``.
_LANG_TAG_RE = re.compile(r"^\s*lang\s*[:=]\s*([A-Za-z]{2,12})\b[\s,]*", re.IGNORECASE)


def _script_of(ch: str) -> str | None:
    """Coarse script bucket for one character, by Unicode block name.

    We only care about scripts that cleanly imply a language (or small family);
    everything else (notably Latin) returns ``None`` so it doesn't sway the vote.
    """
    if not ch.isalpha():
        return None
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    # The block name's first word is a reliable, dependency-free script signal.
    if name.startswith("CJK"):
        return "han"
    for token, script in (
        ("HIRAGANA", "kana"),
        ("KATAKANA", "kana"),
        ("HANGUL", "hangul"),
        ("ARABIC", "arabic"),
        ("HEBREW", "hebrew"),
        ("CYRILLIC", "cyrillic"),
        ("GREEK", "greek"),
        ("DEVANAGARI", "devanagari"),
        ("BENGALI", "bengali"),
        ("TAMIL", "tamil"),
        ("THAI", "thai"),
        ("GEORGIAN", "georgian"),
        ("ARMENIAN", "armenian"),
    ):
        if name.startswith(token):
            return script
    return None


# A non-Latin script maps to its most likely language (ISO-639-1). Kana present
# anywhere implies Japanese; bare Han implies Chinese; Hangul implies Korean.
_SCRIPT_TO_ISO: dict[str, str] = {
    "han": "zh",
    "kana": "ja",
    "hangul": "ko",
    "arabic": "ar",
    "hebrew": "he",
    "cyrillic": "ru",
    "greek": "el",
    "devanagari": "hi",
    "bengali": "bn",
    "tamil": "ta",
    "thai": "th",
    "georgian": "ka",
    "armenian": "hy",
}


def parse_lang_tag(query: str) -> tuple[str | None, str]:
    """Split a leading ``lang:<code>`` tag off a query.

    Returns ``(iso_code_or_None, remaining_query)``. The remaining query is what
    a source should actually search for; the code (if any) is the explicit
    language override. Unknown codes are ignored (treated as no tag) so a typo
    never silently filters out every result.
    """
    if not query:
        return None, ""
    m = _LANG_TAG_RE.match(query)
    if not m:
        return None, query.strip()
    token = m.group(1).lower()
    iso = _ALIAS_TO_ISO.get(token) or (token if token in ISO_TO_GDELT else None)
    remaining = query[m.end():].strip()
    if iso is None:
        # Not a language we know — leave the query untouched rather than eat the
        # token, so the user still searches for what they typed.
        return None, query.strip()
    return iso, remaining


def detect_script_iso(text: str) -> str | None:
    """Best-effort ISO-639-1 language from the dominant non-Latin script.

    Returns ``None`` for Latin-only / unclassifiable text (the unfiltered,
    maximum-recall default). Kana anywhere wins for Japanese (Japanese mixes
    Han + kana), so we check it before falling back to the plain majority.
    """
    counts: dict[str, int] = {}
    for ch in text or "":
        script = _script_of(ch)
        if script:
            counts[script] = counts.get(script, 0) + 1
    if not counts:
        return None
    # Japanese text is Han + kana; the presence of kana disambiguates it from
    # Chinese regardless of which script has more characters.
    if "kana" in counts:
        return "ja"
    dominant = max(counts, key=counts.get)
    return _SCRIPT_TO_ISO.get(dominant)


def resolve_query_language(query: str) -> tuple[str, str | None, str | None]:
    """Resolve a raw query into ``(clean_query, iso_code, gdelt_name)``.

    ``clean_query`` is the text to search (any ``lang:`` tag stripped).
    ``iso_code`` / ``gdelt_name`` are ``None`` when the language is unresolved
    (Latin script, no tag) — meaning "search every language", which the English
    dashboard then normalises. Explicit ``lang:`` tags win over auto-detection.
    """
    iso, clean = parse_lang_tag(query or "")
    if iso is None:
        iso = detect_script_iso(clean)
    if iso is None:
        return clean, None, None
    return clean, iso, ISO_TO_GDELT.get(iso)


# ---------------------------------------------------------------------------
#  Content-language detection (display / translation side)
# ---------------------------------------------------------------------------
# The query side above answers "what language should we *search* in?". The feed
# side asks the mirror question — "what language is this fetched text, and does
# it need translating to English?". Both lean on the same script heuristic, but
# the content detector buckets Latin script as English (so it can answer "is
# this already English?") whereas the search side leaves Latin unresolved.

# ISO 639-1 code -> human-readable English name, for labelling and for the
# translation layer (which speaks both codes and names).
ISO_TO_NAME: dict[str, str] = {
    "en": "English",
    "ar": "Arabic",
    "ru": "Russian",
    "uk": "Ukrainian",
    "fa": "Persian",
    "he": "Hebrew",
    "el": "Greek",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "th": "Thai",
    "ka": "Georgian",
    "hy": "Armenian",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
    "tr": "Turkish",
}

# Reverse map (lower-cased name -> code) for callers that hold a display name.
NAME_TO_ISO: dict[str, str] = {name.lower(): code for code, name in ISO_TO_NAME.items()}


def iso_to_name(code: str | None) -> str | None:
    """Map an ISO 639-1 code to its English name, or return the input unchanged
    if it already looks like a name (and ``None`` for empty input)."""
    if not code:
        return None
    key = code.strip().lower()
    if key in ISO_TO_NAME:
        return ISO_TO_NAME[key]
    return code.strip() or None


def name_to_iso(name: str | None) -> str | None:
    """Map an English language name to its ISO 639-1 code, or ``None``."""
    if not name:
        return None
    return NAME_TO_ISO.get(name.strip().lower())


def detect_language(text: str | None) -> str | None:
    """Heuristically detect a text's dominant-script language as an ISO code.

    Reuses the same script buckets as the search side, but maps Latin script to
    ``en`` so the answer is usable for "does this need translating to English?".
    Returns ``None`` when there is too little signal (fewer than 4 letters, or no
    classifiable script). Kana anywhere wins for Japanese. Never raises.
    """
    if not text:
        return None
    counts: dict[str, int] = {}
    letters = 0
    for ch in text:
        if not ch.isalpha():
            continue
        bucket = _script_of(ch)
        # Latin (and any alpha char our buckets don't name) reads as English;
        # a named non-Latin script maps through to its ISO language.
        iso = "en" if bucket is None else _SCRIPT_TO_ISO.get(bucket)
        if iso is None:
            continue
        letters += 1
        counts[iso] = counts.get(iso, 0) + 1
    if letters < 4 or not counts:
        return None
    # Japanese mixes Han + kana; kana's presence disambiguates it from Chinese.
    if "ja" in counts:
        return "ja"
    return max(counts, key=counts.get)


def is_english(text: str | None) -> bool:
    """True when the text is (heuristically) already English / Latin script.

    Treats unknown / insufficient-signal input as English so the translation
    layer never spends a call on noise. Callers wanting the opposite gate should
    check ``detect_language(text) not in (None, "en")`` explicitly.
    """
    return detect_language(text) in (None, "en")
