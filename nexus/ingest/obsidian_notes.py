"""Parse Obsidian Markdown notes: frontmatter, tags, wikilinks, embeds, headings.

Pure text in, structured note out — no filesystem or network access, so it is
shared by the vault scanner (:mod:`nexus.sources.obsidian`) and by the upload
path when a ZIP turns out to be a zipped vault.

  * Frontmatter: a leading ``---`` YAML block, parsed with ``yaml.safe_load``
    only (PyYAML is optional: without it a minimal ``key: value`` reader is
    used). Anything that is not a mapping is ignored.
  * Tags: ``tags``/``tag`` from the frontmatter (list or comma/space string) and
    inline ``#tags`` outside code (``#123`` and headings are not tags).
  * ``[[Note]]``, ``[[Note#Heading|alias]]`` → links; ``![[file.pdf]]`` →
    embeds. Both are kept as metadata; in the stored text a link reads as its
    alias (or the note name).
  * A large note is split at its Markdown headings into sections.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime

logger = logging.getLogger("nexus.ingest.obsidian_notes")

_FRONTMATTER = re.compile(r"\A﻿?---[ \t]*\r?\n(.{0,65536}?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)",
                          re.S)
_FENCE = re.compile(r"^(```|~~~)")
_INLINE_CODE = re.compile(r"`[^`\n]{0,2000}`")
_WIKI = re.compile(r"(!?)\[\[([^\[\]\n|#^]{1,300})((?:#|\^)[^\[\]\n|]{0,300})?(?:\|([^\[\]\n]{0,300}))?\]\]")
_TAG = re.compile(r"(?<![\w/&#])#([A-Za-z0-9_\-/]{0,100}[A-Za-z_\-/][A-Za-z0-9_\-/]{0,100})")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.{1,300}?)[ \t#]*$")
SPLIT_CHARS = 6000


@dataclass
class Note:
    title: str
    body: str                                   # Markdown without frontmatter
    text: str                                   # readable text (links as words)
    frontmatter: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    embeds: list[str] = field(default_factory=list)
    sections: list[tuple[str, str]] = field(default_factory=list)


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(v) for v in value[:100]]
    if isinstance(value, dict):
        return {str(k)[:100]: _jsonable(v) for k, v in list(value.items())[:100]}
    return str(value)[:500]


def _simple_yaml(block: str) -> dict:
    """Minimal ``key: value`` / ``- item`` reader used when PyYAML is absent."""
    out: dict = {}
    key = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t", "-")) and key and line.strip().startswith("- "):
            out.setdefault(key, [])
            if isinstance(out[key], list):
                out[key].append(line.strip()[2:].strip().strip("'\""))
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                out[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            else:
                out[key] = val.strip("'\"") if val else []
    return out


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """``(frontmatter, body)``. Never raises; bad YAML yields ``{}``."""
    m = _FRONTMATTER.match(text or "")
    if not m:
        return {}, text or ""
    block, body = m.group(1), text[m.end():]
    try:
        import yaml

        data = yaml.safe_load(block)
    except ImportError:
        data = _simple_yaml(block)
    except Exception:
        logger.debug("Unreadable frontmatter ignored", exc_info=True)
        data = {}
    if not isinstance(data, dict):
        data = {}
    return _jsonable(data), body


def _fm_tags(fm: dict) -> list[str]:
    raw = fm.get("tags", fm.get("tag"))
    if isinstance(raw, str):
        vals = re.split(r"[,\s]+", raw)
    elif isinstance(raw, list):
        vals = [str(v) for v in raw if v is not None]
    else:
        vals = []
    return [v.strip().lstrip("#") for v in vals if v and v.strip().lstrip("#")]


def _outside_code(body: str) -> list[str]:
    """Lines of ``body`` that are not inside fenced code blocks."""
    lines: list[str] = []
    fenced = False
    for line in body.splitlines():
        if _FENCE.match(line.lstrip()):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def _unique(seq) -> list[str]:
    out: list[str] = []
    for s in seq:
        if s and s not in out:
            out.append(s)
    return out


def _readable(body: str) -> str:
    def repl(m: re.Match) -> str:
        bang, target, _anchor, alias = m.groups()
        name = (alias or target).strip()
        return f"[embedded: {target.strip()}]" if bang else name

    text = _WIKI.sub(repl, body)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_sections(title: str, body: str, max_chars: int = SPLIT_CHARS) -> list[tuple[str, str]]:
    """Split a long note at its headings (outside code); short notes stay whole."""
    readable = _readable(body)
    if len(readable) <= max_chars:
        return [("", readable)] if readable else []
    sections: list[tuple[str, list[str]]] = [("", [])]
    fenced = False
    for line in body.splitlines():
        if _FENCE.match(line.lstrip()):
            fenced = not fenced
        m = None if fenced else _HEADING.match(line)
        if m:
            sections.append((m.group(2).strip(), [line]))
        else:
            sections[-1][1].append(line)
    out = [(label, _readable("\n".join(lines))) for label, lines in sections]
    return [(label, text) for label, text in out if text.strip()]


def parse_note(text: str, title: str) -> Note:
    fm, body = parse_frontmatter(text)
    plain = "\n".join(_INLINE_CODE.sub(" ", ln) for ln in _outside_code(body))
    # Headings need "# " (a space), so the tag pattern never matches them.
    tags = _fm_tags(fm) + [m.group(1) for m in _TAG.finditer(plain)]
    links: list[str] = []
    embeds: list[str] = []
    for m in _WIKI.finditer(plain):
        bang, target = m.group(1), m.group(2).strip()
        (embeds if bang else links).append(target)
    note_title = str(fm.get("title") or title).strip() or title
    return Note(
        title=note_title[:300], body=body, text=_readable(body), frontmatter=fm,
        tags=_unique(t.strip("/") for t in tags)[:100], links=_unique(links)[:200],
        embeds=_unique(embeds)[:100], sections=split_sections(note_title, body),
    )


def is_note_embed(target: str) -> bool:
    """An embed of another note (no file extension or .md) rather than a file."""
    name = target.rsplit("/", 1)[-1]
    return "." not in name or name.lower().endswith((".md", ".markdown"))
