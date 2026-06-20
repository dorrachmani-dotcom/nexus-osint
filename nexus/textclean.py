"""Shared plain-text cleaner for collected source content.

Sources hand us text in three messy flavours that all look terrible in the feed:

* **HTML** (RSS summaries) — ``<p>``, ``<a href=...>`` tags and entities.
* **Markdown** (Reddit ``selftext``) — ``![alt](url)`` images, ``[text](url)``
  links, ``>`` block-quotes, ``#`` headings.
* **Double-escaped HTML** — content that was entity-encoded twice, so the feed
  shows literal ``&gt;``, ``&lt;/a&gt;``, ``&lt;div style=`` garbage.

``clean_text`` turns any of these into a single line of readable plain text. It
is deliberately forgiving: it never raises, and on anything it doesn't recognise
it simply collapses whitespace and returns the input. Used by every source so
the feed reads cleanly regardless of where an item came from.
"""

from __future__ import annotations

import html
import re

# Markdown image: drop entirely — the alt text is usually a duplicate of the
# title and the URL is noise in a text feed.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# Markdown link: keep the visible text, discard the URL.
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Orphaned ``](url)`` tail with no matching ``[`` (left after a malformed image).
_ORPHAN_LINK_TAIL = re.compile(r"\]\([^)]*\)")
# Any HTML tag (surfaces after we unescape entities).
_TAG = re.compile(r"<[^>]+>")
# Leading block-quote (``>``) and heading (``#``) markers, line by line.
_BLOCKQUOTE = re.compile(r"(?m)^[ \t]*>+[ \t]?")
_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+")
# An unclosed tag fragment like ``<div style=`` with no ``>``. Bounded to the
# next whitespace so it can NEVER swallow legitimate text that follows — a
# trailing ``$`` anchor here once ate whole sentences.
_STRAY_ANGLE = re.compile(r"</?[a-zA-Z][^>\s]*")
_WS = re.compile(r"\s+")


def clean_text(value: str | None) -> str:
    """Return readable single-line plain text from HTML/Markdown/garbled input.

    Never raises — on unexpected input it returns the whitespace-collapsed
    original so a single bad item can't break collection or the feed.
    """
    if not value:
        return ""
    try:
        text = str(value)
        # 1) Decode entities, twice, to handle double-escaped HTML
        #    (``&amp;lt;`` -> ``&lt;`` -> ``<``).
        for _ in range(2):
            decoded = html.unescape(text)
            if decoded == text:
                break
            text = decoded
        # 2) Markdown images out, markdown links down to their text.
        text = _MD_IMAGE.sub(" ", text)
        text = _MD_LINK.sub(r"\1", text)
        # 3) Remove an orphaned ``](url)`` tail left by a malformed image/link.
        text = _ORPHAN_LINK_TAIL.sub(" ", text)
        # 4) Strip any real HTML tags that the unescape surfaced.
        text = _TAG.sub(" ", text)
        # 5) Drop a dangling partial tag like ``<div style=`` (whitespace-bounded).
        text = _STRAY_ANGLE.sub(" ", text)
        # 6) Remove block-quote and heading markers (keep their text).
        text = _BLOCKQUOTE.sub("", text)
        text = _HEADING.sub("", text)
        # 7) Collapse all whitespace to single spaces.
        return _WS.sub(" ", text).strip()
    except Exception:
        # Absolute last resort: never let cleaning break a collection run.
        try:
            return _WS.sub(" ", str(value)).strip()
        except Exception:
            return ""
