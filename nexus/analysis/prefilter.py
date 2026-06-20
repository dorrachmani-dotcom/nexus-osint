"""Local, zero-cost gate run before any item reaches Claude.

The goal is purely cost control: drop obvious noise (empty stubs, link-only
posts, boilerplate) so tokens are spent only on content worth analysing. It is
deliberately conservative — when in doubt, let the item through and let the
model decide.
"""

from __future__ import annotations

import re

from nexus.config import Settings

# A post that is essentially just a URL carries no analysable text.
_URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)
# Collapse whitespace when measuring "real" length.
_WS_RE = re.compile(r"\s+")


def _meaningful_length(text: str) -> int:
    return len(_WS_RE.sub(" ", text).strip())


def should_analyze(title: str | None, content: str | None, settings: Settings) -> bool:
    """Return True if the item is worth sending to the model.

    Filters out: empty items, URL-only posts, and anything shorter than the
    configured minimum length (PREFILTER_MIN_LENGTH).
    """
    body = (content or "").strip()
    head = (title or "").strip()
    combined = (head + " " + body).strip()

    if not combined:
        return False
    if _URL_ONLY_RE.match(body) and not head:
        return False
    if _meaningful_length(combined) < settings.prefilter_min_length:
        return False
    return True
