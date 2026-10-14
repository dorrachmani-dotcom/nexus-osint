"""Local, zero-cost replies for Sherlock: small talk and manipulation attempts.

Greetings, thanks, "who are you" and similar messages never need a language
model, so they are answered here with a fixed message and no API call (and they
work even when no AI provider is configured). Obvious prompt-injection or
jailbreak attempts ("ignore your instructions", "show your system prompt") and
questions about who built the software are refused here too, before anything
reaches the model; the tool allow-list in nexus.assistant remains the hard
boundary for everything else.

Hebrew phrases are written as escapes so the source stays ASCII.
"""

from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger("nexus.assistant")

# --- English vocabulary -----------------------------------------------------
_EN_GREET = ("hi", "hey", "hello", "hiya", "yo", "sup", "good morning", "good afternoon",
             "good evening", "howdy", "whats up", "how are you")
_EN_THANKS = ("thanks", "thank you", "thx", "ty", "cheers", "great", "awesome", "perfect", "ok", "okay")
_EN_BYE = ("bye", "goodbye", "see you", "see ya", "good night", "later")
_EN_WHO = ("who are you", "what are you", "what can you do", "help", "how can you help",
           "what do you do")
_EN_MAKER = re.compile(
    r"\bwho\s+(?:made|built|created|developed|wrote|coded|is\s+behind|owns)\b"
    r"|\b(?:your|the)\s+(?:creator|developer|author|maker)\b"
)
_EN_INJECT = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,40}\b(?:instructions?|rules|prompts?|guidelines|restrictions)\b"
    r"|\bsystem\s+prompt\b|\b(?:reveal|show|print|repeat|leak)\b.{0,30}\b(?:prompt|instructions|rules)\b"
    r"|\bdeveloper\s+mode\b|\bjailbreak\b|\bdo\s+anything\s+now\b|\byou\s+are\s+now\b"
    r"|\bpretend\s+(?:to\s+be|you\s+are)\b|\bact\s+as\s+(?:an?\s+)?(?:unrestricted|unfiltered|evil)\b"
)

# --- Hebrew vocabulary (escaped) ----------------------------------------------
_HE_GREET = ("\u05d4\u05d9\u05d9", "\u05d4\u05d9", "\u05e9\u05dc\u05d5\u05dd", "\u05d0\u05d4\u05dc\u05df", "\u05d1\u05d5\u05e7\u05e8 \u05d8\u05d5\u05d1", "\u05e2\u05e8\u05d1 \u05d8\u05d5\u05d1", "\u05e6\u05d4\u05e8\u05d9\u05d9\u05dd \u05d8\u05d5\u05d1\u05d9\u05dd", "\u05de\u05d4 \u05e0\u05e9\u05de\u05e2", "\u05de\u05d4 \u05e7\u05d5\u05e8\u05d4", "\u05de\u05d4 \u05d4\u05e2\u05e0\u05d9\u05d9\u05e0\u05d9\u05dd",)
_HE_THANKS = ("\u05ea\u05d5\u05d3\u05d4", "\u05ea\u05d5\u05d3\u05d4 \u05e8\u05d1\u05d4", "\u05ea\u05d5\u05d3\u05d4 \u05dc\u05da", "\u05d0\u05d7\u05dc\u05d4", "\u05de\u05e2\u05d5\u05dc\u05d4",)
_HE_BYE = ("\u05d1\u05d9\u05d9", "\u05dc\u05d4\u05ea\u05e8\u05d0\u05d5\u05ea", "\u05dc\u05d9\u05dc\u05d4 \u05d8\u05d5\u05d1",)
_HE_WHO = ("\u05de\u05d9 \u05d0\u05ea\u05d4", "\u05de\u05d4 \u05d0\u05ea\u05d4", "\u05de\u05d4 \u05d0\u05ea\u05d4 \u05d9\u05d5\u05d3\u05e2 \u05dc\u05e2\u05e9\u05d5\u05ea", "\u05de\u05d4 \u05d0\u05e4\u05e9\u05e8 \u05dc\u05e2\u05e9\u05d5\u05ea", "\u05e2\u05d6\u05e8\u05d4", "\u05d0\u05d9\u05da \u05d0\u05ea\u05d4 \u05d9\u05db\u05d5\u05dc \u05dc\u05e2\u05d6\u05d5\u05e8",)
_HE_MAKER = ("\u05de\u05d9 \u05d1\u05e0\u05d4 \u05d0\u05d5\u05ea\u05da", "\u05de\u05d9 \u05d9\u05e6\u05e8 \u05d0\u05d5\u05ea\u05da", "\u05de\u05d9 \u05e4\u05d9\u05ea\u05d7 \u05d0\u05d5\u05ea\u05da", "\u05de\u05d9 \u05e2\u05d5\u05de\u05d3 \u05de\u05d0\u05d7\u05d5\u05e8\u05d9\u05d9\u05da", "\u05de\u05d9 \u05e2\u05e9\u05d4 \u05d0\u05d5\u05ea\u05da",)
_HE_INJECT = ("\u05d4\u05ea\u05e2\u05dc\u05dd \u05de\u05d4\u05d4\u05d5\u05e8\u05d0\u05d5\u05ea", "\u05d4\u05ea\u05e2\u05dc\u05dd \u05de\u05db\u05dc \u05d4\u05d4\u05d5\u05e8\u05d0\u05d5\u05ea", "\u05e9\u05db\u05d7 \u05d0\u05ea \u05d4\u05d4\u05d5\u05e8\u05d0\u05d5\u05ea", "\u05d4\u05d5\u05e8\u05d0\u05d5\u05ea \u05d4\u05de\u05e2\u05e8\u05db\u05ea", "\u05d4\u05e4\u05e8\u05d5\u05de\u05e4\u05d8 \u05e9\u05dc\u05da", "\u05ea\u05e6\u05d9\u05d2 \u05d0\u05ea \u05d4\u05d4\u05e0\u05d7\u05d9\u05d5\u05ea",)

_REPLIES = {
    "en": {
        "greet": ("Hi! I'm Sherlock, your OSINT assistant. Ask me to open a case, search what "
                  "has been collected, summarise an investigation or build a report."),
        "thanks": "Happy to help. I'm here if the investigation needs anything else.",
        "bye": "Bye! Your scans and cases will be waiting here.",
        "who": ("I'm Sherlock, the built-in investigation assistant. I answer from the items "
                "Nexus collected, open cases and add tracking words and questions, search the "
                "feed and build reports. Try: \"open a case on Google DeepMind and add the "
                "related items\"."),
        "maker": "I don't have information about who built this software. Happy to help with the investigation itself.",
        "inject": ("I won't do that: I don't change or reveal my instructions. Happy to help "
                   "with a question about the data or the investigation."),
    },
    "he": {
        "greet": "\u05e9\u05dc\u05d5\u05dd! \u05d0\u05e0\u05d9 Sherlock, \u05e2\u05d5\u05d6\u05e8 \u05d4-OSINT \u05e9\u05dc\u05da. \u05d0\u05e4\u05e9\u05e8 \u05dc\u05d1\u05e7\u05e9 \u05de\u05de\u05e0\u05d9 \u05dc\u05e4\u05ea\u05d5\u05d7 \u05ea\u05d9\u05e7, \u05dc\u05d7\u05e4\u05e9 \u05d1\u05d9\u05d3\u05d9\u05e2\u05d5\u05ea \u05e9\u05e0\u05d0\u05e1\u05e4\u05d5, \u05dc\u05e1\u05db\u05dd \u05d7\u05e7\u05d9\u05e8\u05d4 \u05d0\u05d5 \u05dc\u05d4\u05db\u05d9\u05df \u05d3\u05d5\u05d7.",
        "thanks": "\u05d1\u05e9\u05de\u05d7\u05d4! \u05d0\u05dd \u05e6\u05e8\u05d9\u05da \u05e2\u05d5\u05d3 \u05de\u05e9\u05d4\u05d5 \u05d1\u05d7\u05e7\u05d9\u05e8\u05d4, \u05d0\u05e0\u05d9 \u05db\u05d0\u05df.",
        "bye": "\u05dc\u05d4\u05ea\u05e8\u05d0\u05d5\u05ea! \u05d4\u05e1\u05e8\u05d9\u05e7\u05d5\u05ea \u05d5\u05d4\u05ea\u05d9\u05e7\u05d9\u05dd \u05de\u05de\u05e9\u05d9\u05db\u05d9\u05dd \u05dc\u05d7\u05db\u05d5\u05ea \u05dc\u05da \u05db\u05d0\u05df.",
        "who": "\u05d0\u05e0\u05d9 Sherlock, \u05e2\u05d5\u05d6\u05e8 \u05d4\u05d7\u05e7\u05d9\u05e8\u05d4 \u05d4\u05de\u05d5\u05d1\u05e0\u05d4. \u05d0\u05e0\u05d9 \u05d9\u05d5\u05d3\u05e2 \u05dc\u05e2\u05e0\u05d5\u05ea \u05de\u05ea\u05d5\u05da \u05d4\u05d9\u05d3\u05d9\u05e2\u05d5\u05ea \u05e9\u05e0\u05d0\u05e1\u05e4\u05d5, \u05dc\u05e4\u05ea\u05d5\u05d7 \u05ea\u05d9\u05e7\u05d9\u05dd \u05d5\u05dc\u05d4\u05d5\u05e1\u05d9\u05e3 \u05dc\u05d4\u05dd \u05de\u05d9\u05dc\u05d5\u05ea \u05de\u05e2\u05e7\u05d1 \u05d5\u05e9\u05d0\u05dc\u05d5\u05ea, \u05dc\u05d7\u05e4\u05e9 \u05d1\u05e4\u05d9\u05d3 \u05d5\u05dc\u05d4\u05db\u05d9\u05df \u05d3\u05d5\u05d7\u05d5\u05ea. \u05e0\u05e1\u05d4 \u05dc\u05de\u05e9\u05dc: \"\u05e4\u05ea\u05d7 \u05ea\u05d9\u05e7 \u05e2\u05dc Google DeepMind \u05d5\u05d4\u05d5\u05e1\u05e3 \u05d0\u05ea \u05d4\u05d9\u05d3\u05d9\u05e2\u05d5\u05ea \u05d4\u05e8\u05dc\u05d5\u05d5\u05e0\u05d8\u05d9\u05d5\u05ea\".",
        "maker": "\u05d0\u05d9\u05df \u05dc\u05d9 \u05de\u05d9\u05d3\u05e2 \u05e2\u05dc \u05de\u05d9 \u05d1\u05e0\u05d4 \u05d0\u05ea \u05d4\u05ea\u05d5\u05db\u05e0\u05d4. \u05d0\u05e9\u05de\u05d7 \u05dc\u05e2\u05d6\u05d5\u05e8 \u05d1\u05d7\u05e7\u05d9\u05e8\u05d4 \u05e2\u05e6\u05de\u05d4.",
        "inject": "\u05d0\u05ea \u05d4\u05d1\u05e7\u05e9\u05d4 \u05d4\u05d6\u05d0\u05ea \u05d0\u05e0\u05d9 \u05dc\u05d0 \u05de\u05d1\u05e6\u05e2: \u05d0\u05e0\u05d9 \u05dc\u05d0 \u05de\u05e9\u05e0\u05d4 \u05d0\u05ea \u05d4\u05d4\u05e0\u05d7\u05d9\u05d5\u05ea \u05e9\u05dc\u05d9 \u05d5\u05dc\u05d0 \u05d7\u05d5\u05e9\u05e3 \u05d0\u05d5\u05ea\u05df. \u05d0\u05e9\u05de\u05d7 \u05dc\u05e2\u05d6\u05d5\u05e8 \u05d1\u05e9\u05d0\u05dc\u05d4 \u05e2\u05dc \u05d4\u05e0\u05ea\u05d5\u05e0\u05d9\u05dd \u05d0\u05d5 \u05e2\u05dc \u05d4\u05d7\u05e7\u05d9\u05e8\u05d4.",
    },
}

_MAX_SMALL_TALK_WORDS = 6


def _is_hebrew(text: str) -> bool:
    return any("\u0590" <= ch <= "\u05ff" for ch in text)


def _normalise(text: str) -> str:
    """Lower-case, drop punctuation/emoji, collapse whitespace."""
    out = []
    for ch in text.lower():
        cat = unicodedata.category(ch)
        out.append(ch if cat[0] in ("L", "N") or ch.isspace() else " ")
    return " ".join("".join(out).split())


def _matches(norm: str, phrases: tuple[str, ...]) -> bool:
    """Whole-message match (allowing a short tail like 'sherlock' or 'there')."""
    for p in phrases:
        if norm == p or (norm.startswith(p + " ") and len(norm.split()) <= len(p.split()) + 2):
            return True
    return False


def local_reply(question: str) -> tuple[str, str] | None:
    """Return ``(kind, reply)`` when the message can be answered without a model.

    ``kind`` is ``"small_talk"`` or ``"refused"``; ``None`` means: ask the model.
    """
    text = (question or "").strip()
    if not text:
        return None
    lang = "he" if _is_hebrew(text) else "en"
    replies = _REPLIES[lang]
    norm = _normalise(text)
    lowered = text.lower()

    # Manipulation attempts are refused whatever their length.
    if _EN_INJECT.search(lowered) or any(p in text for p in _HE_INJECT):
        logger.warning("Sherlock: refused a prompt-injection style request locally")
        return "refused", replies["inject"]
    if _EN_MAKER.search(lowered) or any(p in text for p in _HE_MAKER):
        return "refused", replies["maker"]

    # Small talk only when the WHOLE message is small talk (short).
    if len(norm.split()) > _MAX_SMALL_TALK_WORDS:
        return None
    for key, en, he in (("who", _EN_WHO, _HE_WHO), ("greet", _EN_GREET, _HE_GREET),
                        ("thanks", _EN_THANKS, _HE_THANKS), ("bye", _EN_BYE, _HE_BYE)):
        if _matches(norm, en) or _matches(norm, he):
            return "small_talk", replies[key]
    return None
