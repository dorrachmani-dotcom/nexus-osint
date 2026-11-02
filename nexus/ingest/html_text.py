"""Readable text from HTML using only the standard library.

Used for uploaded ``.html`` files, saved MHTML pages, ZIP exports of a wiki or
documentation site, and (later) watched web pages. No third-party parser and
no network: the page is only *read*, never rendered — scripts, styles and
embedded objects are dropped, nothing is fetched.

The extractor keeps the structure an analyst cares about:
  * the page ``<title>``,
  * headings (``h1`` to ``h3``) as section boundaries, so one long page splits
    into one item per section,
  * links (``href`` + anchor text), so an exported site keeps its internal
    link structure as metadata.

Boilerplate (``nav``, ``header``, ``footer``, ``aside``, ``form``) is skipped
when the page also has real body text, which is what "main readable text"
means in practice for wiki/docs pages.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Elements whose text is never content.
_DROP = {"script", "style", "noscript", "template", "svg", "math", "iframe",
         "object", "embed", "canvas"}
# Page chrome: skipped as long as the page has other text.
_CHROME = {"nav", "header", "footer", "aside", "form", "menu"}
# Block-level tags that should break lines.
_BLOCK = {"p", "div", "section", "article", "main", "li", "ul", "ol", "tr", "table",
          "br", "hr", "pre", "blockquote", "dd", "dt", "dl", "figure", "figcaption",
          "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "caption", "summary",
          "details", "body"}
_HEADINGS = {"h1", "h2", "h3"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col",
         "embed", "param", "source", "track", "wbr"}

logger = logging.getLogger("nexus.ingest.html")

_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MANY_NL_RE = re.compile(r"\n{3,}")
_MAX_LINKS = 500


@dataclass
class HtmlSection:
    heading: str | None
    text: str


@dataclass
class HtmlDoc:
    title: str | None = None
    sections: list[HtmlSection] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (href, anchor text)

    @property
    def text(self) -> str:
        parts = []
        for s in self.sections:
            if s.heading:
                parts.append(s.heading)
            if s.text:
                parts.append(s.text)
        return "\n\n".join(parts).strip()


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.drop_depth = 0
        self.chrome_depth = 0
        # Two parallel streams: everything, and everything outside page chrome.
        self.sections_all: list[list] = [[None, []]]
        self.sections_main: list[list] = [[None, []]]
        self.heading_tag: str | None = None
        self.heading_buf: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.link_href: str | None = None
        self.link_buf: list[str] = []

    # -- helpers -----------------------------------------------------------
    def _emit(self, text: str) -> None:
        self.sections_all[-1][1].append(text)
        if not self.chrome_depth:
            self.sections_main[-1][1].append(text)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _DROP:
            if tag not in _VOID:
                self.drop_depth += 1
            return
        if self.drop_depth:
            return
        if tag == "title":
            self.in_title = True
            return
        if tag in _CHROME:
            self.chrome_depth += 1
        if tag in _HEADINGS:
            self.heading_tag = tag
            self.heading_buf = []
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.link_href = href.strip()
                self.link_buf = []
        if tag in _BLOCK:
            self._emit("\n")

    def handle_startendtag(self, tag, attrs):
        # <br/> and friends: a line break, never a depth change.
        if tag.lower() in _BLOCK and not self.drop_depth:
            self._emit("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _DROP:
            if tag not in _VOID and self.drop_depth:
                self.drop_depth -= 1
            return
        if self.drop_depth:
            return
        if tag == "title":
            self.in_title = False
            return
        if tag in _CHROME and self.chrome_depth:
            self.chrome_depth -= 1
        if tag in _HEADINGS and self.heading_tag == tag:
            heading = _clean_inline("".join(self.heading_buf))
            self.heading_tag = None
            if heading:
                self.sections_all.append([heading, []])
                if not self.chrome_depth:
                    self.sections_main.append([heading, []])
            return
        if tag == "a" and self.link_href is not None:
            if len(self.links) < _MAX_LINKS:
                self.links.append((self.link_href, _clean_inline("".join(self.link_buf))))
            self.link_href = None
        if tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self.drop_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.heading_tag:
            self.heading_buf.append(data)
            return
        if self.link_href is not None:
            self.link_buf.append(data)
        self._emit(data)


def _clean_inline(text: str) -> str:
    return _WS_RE.sub(" ", html.unescape(text or "").replace("\n", " ")).strip()


def _clean_block(text: str) -> str:
    lines = [_WS_RE.sub(" ", ln).strip() for ln in (text or "").split("\n")]
    out = "\n".join(lines)
    return _MANY_NL_RE.sub("\n\n", out).strip()


def _finish(raw_sections: list[list]) -> list[HtmlSection]:
    out: list[HtmlSection] = []
    for heading, parts in raw_sections:
        text = _clean_block("".join(parts))
        if heading or text:
            out.append(HtmlSection(heading=heading, text=text))
    return out


def extract_html(markup: str) -> HtmlDoc:
    """Parse ``markup`` into title, heading-delimited sections and links.

    Never raises: malformed markup yields whatever text could be recovered.
    """
    parser = _Extractor()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:  # HTMLParser is lenient; this is belt and braces.
        logger.debug("HTML parse stopped early; keeping recovered text", exc_info=True)
    main = _finish(parser.sections_main)
    full = _finish(parser.sections_all)
    main_chars = sum(len(s.text) for s in main)
    # Prefer chrome-free text unless that strips (nearly) everything.
    sections = main if main_chars >= 40 else full
    title = _clean_inline("".join(parser.title_parts)) or None
    return HtmlDoc(title=title, sections=sections, links=list(parser.links))


def html_to_text(markup: str) -> str:
    """Flat readable text of an HTML document."""
    return extract_html(markup).text
