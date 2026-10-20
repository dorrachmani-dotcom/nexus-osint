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


# --- Assistant answers: Markdown -> readable plain text ----------------------
# Sherlock's answers are rendered as TEXT in the browser (textContent only —
# never HTML), so any Markdown the model emits shows up as literal ``**`` and
# ``- `` noise. ``markdown_to_plain`` strips that formatting while keeping the
# line structure. It never interprets or removes HTML: a ``<script>`` in an
# answer stays as inert literal text, exactly as before.
_MD_FENCE = re.compile(r"^\s*```[^\n]*$", re.MULTILINE)
_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_MD_RULE = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$", re.MULTILINE)
_MD_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_MD_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_MD_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_MD_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_MD_CODE = re.compile(r"`([^`\n]+)`")
_MD_ANSWER_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BLANK_RUNS = re.compile(r"\n{3,}")


def _link_to_text(m: re.Match[str]) -> str:
    label, url = m.group(1).strip(), m.group(2).strip()
    return label if label == url else f"{label} ({url})"


def markdown_to_plain(value: str | None) -> str:
    """Turn a Markdown-flavoured assistant answer into clean plain text.

    ``**bold**`` / ``__bold__`` / ``*em*`` / ```code``` lose their markers,
    ``#`` headings and fence lines are dropped, ``- `` / ``* `` / ``+ `` bullets
    become ``• `` and ``[label](url)`` becomes ``label (url)``. Newlines are
    preserved (the chat bubble uses ``white-space: pre-wrap``). HTML is left
    untouched on purpose — the client renders the result as text, so it is
    inert. Never raises.
    """
    if not value:
        return ""
    try:
        text = str(value).replace("\r\n", "\n")
        text = _MD_FENCE.sub("", text)
        text = _MD_RULE.sub("", text)
        text = _MD_HEADING.sub("", text)
        text = _MD_QUOTE.sub("", text)
        text = _MD_BULLET.sub(lambda m: m.group(1) + "• ", text)
        text = _MD_ANSWER_LINK.sub(_link_to_text, text)
        text = _MD_BOLD.sub(r"\2", text)
        text = _MD_ITALIC.sub(r"\1", text)
        text = _MD_CODE.sub(r"\1", text)
        text = _BLANK_RUNS.sub("\n\n", text)
        return text.strip()
    except Exception:  # pragma: no cover - defensive, regexes are static
        return str(value)
