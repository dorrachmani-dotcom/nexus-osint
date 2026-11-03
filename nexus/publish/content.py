"""What gets published: an item (with the analyst's edit on top) or a write-up,
rendered for each target.

* :func:`to_storage` - Confluence storage format (XHTML). Every piece of text,
  including collected content, is XML-escaped; links are only emitted for
  http(s) URLs. No raw HTML or macro markup from any input ever reaches the
  page.
* :func:`to_markdown` - an Obsidian note with YAML frontmatter written by
  ``yaml.safe_dump`` and ``[[wikilinks]]`` for entities.
* :func:`to_text` - plain text for the knowledge pool.

The text body of a write-up (and an analyst note) is treated as plain text
with a tiny subset of structure: blank lines separate paragraphs and lines
starting with ``- `` or ``* `` become a bullet list.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from nexus.storage import decode_entities
from nexus.storage.publishing import get_writeup, item_for_publish

FOOTER = "Collected by Nexus-OSINT"
MARKER_KEY = "generated_by"
MARKER_VALUE = "Nexus-OSINT"
ID_KEY = "nexus_id"
KEEP_LINE = ("%% nexus:end - anything you write below this line is kept when "
             "Nexus updates this note %%")

# Characters XML 1.0 forbids (Confluence rejects a body containing them).
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_HTTP = re.compile(r"^https?://", re.I)


@dataclass
class SourceRef:
    title: str
    url: str | None = None
    collected_at: str | None = None


@dataclass
class PublishDoc:
    """One publishable subject, already resolved to its effective text."""

    kind: str                     # item | writeup
    subject_id: int
    title: str
    summary: str = ""             # edited summary / AI summary (items)
    note: str = ""                # analyst note (items)
    body: str = ""                # write-up text
    source_name: str | None = None
    source_url: str | None = None
    collected_at: str | None = None
    edited_at: str | None = None
    cases: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    sources: list[SourceRef] = field(default_factory=list)
    edited: bool = False

    @property
    def nexus_id(self) -> str:
        return f"{self.kind}-{self.subject_id}"


def clean_text(value) -> str:
    """Strip characters no XML / Markdown file should carry; normalise newlines."""
    return _XML_BAD.sub("", str(value or "").replace("\r\n", "\n").replace("\r", "\n"))


def _http(url) -> str | None:
    url = clean_text(url).strip()
    return url if url and _HTTP.match(url) and not any(c.isspace() for c in url) else None


def _entities(raw) -> list[str]:
    groups = decode_entities(raw) or {}
    out: list[str] = []
    for key in ("people", "organizations", "locations", "identifiers"):
        for name in groups.get(key) or []:
            name = clean_text(name).strip()
            if name and name not in out:
                out.append(name[:120])
    return out[:40]


def item_doc(conn, item_id: int) -> PublishDoc | None:
    it = item_for_publish(conn, item_id)
    if it is None:
        return None
    title = (it.get("edit_title") or it.get("title") or f"Item {item_id}").strip()
    summary = it.get("edit_summary") or it.get("summary") or (it.get("content") or "")[:1500]
    return PublishDoc(
        kind="item", subject_id=int(item_id), title=clean_text(title)[:300],
        summary=clean_text(summary).strip(), note=clean_text(it.get("edit_note")).strip(),
        source_name=it.get("source"), source_url=_http(it.get("url")),
        collected_at=(it.get("fetched_at") or "")[:19] or None,
        edited_at=it.get("edited_at"), cases=list(it.get("cases") or []),
        entities=_entities(it.get("entities")),
        edited=bool(it.get("edited_at")),
    )


def writeup_doc(conn, writeup_id: int) -> PublishDoc | None:
    w = get_writeup(conn, writeup_id)
    if w is None:
        return None
    sources: list[SourceRef] = []
    entities: list[str] = []
    for iid in w["item_ids"]:
        d = item_doc(conn, iid)
        if d is None:
            continue
        sources.append(SourceRef(d.title, d.source_url, d.collected_at))
        entities.extend(e for e in d.entities if e not in entities)
    return PublishDoc(
        kind="writeup", subject_id=int(writeup_id), title=clean_text(w["title"])[:300],
        body=clean_text(w.get("body")).strip(),
        cases=[w["case_name"]] if w.get("case_name") else [],
        edited_at=w.get("edited_at"), entities=entities[:40], sources=sources, edited=True,
    )


def load_doc(conn, kind: str, subject_id: int) -> PublishDoc | None:
    if kind == "item":
        return item_doc(conn, subject_id)
    if kind == "writeup":
        return writeup_doc(conn, subject_id)
    return None


def compose_writeup_text(conn, item_ids: list[int]) -> str:
    """A first draft from the selected items; the analyst edits it before use."""
    lines = ["Summary of the selected items (edit before publishing).", ""]
    for iid in item_ids:
        d = item_doc(conn, iid)
        if d is None:
            continue
        gist = " ".join(d.summary.split())[:600]
        lines.append(f"- {d.title}: {gist}" if gist else f"- {d.title}")
        if d.note:
            lines.append(f"  Analyst note: {' '.join(d.note.split())[:400]}")
    return "\n".join(lines).strip()


# ------------------------------------------------------------- plain blocks
def _blocks(text: str) -> list[tuple[str, list[str]]]:
    """``[("p", [line]) | ("ul", [item, ...])]`` from plain text."""
    out: list[tuple[str, list[str]]] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        nonlocal para, bullets
        if para:
            out.append(("p", [" ".join(para)]))
            para = []
        if bullets:
            out.append(("ul", bullets))
            bullets = []

    for raw in clean_text(text).split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        if line[:2] in ("- ", "* "):
            if para:
                out.append(("p", [" ".join(para)]))
                para = []
            bullets.append(line[2:].strip())
        elif bullets and raw[:1].isspace():
            bullets[-1] += " " + line  # continuation of a bullet
        else:
            if bullets:
                out.append(("ul", bullets))
                bullets = []
            para.append(line)
    flush()
    return out


# ------------------------------------------------- Confluence storage format
def _x(text) -> str:
    return html.escape(clean_text(text), quote=True)


def _storage_blocks(text: str) -> str:
    parts = []
    for kind, lines in _blocks(text):
        if kind == "p":
            parts.append(f"<p>{_x(lines[0])}</p>")
        else:
            parts.append("<ul>" + "".join(f"<li>{_x(li)}</li>" for li in lines) + "</ul>")
    return "".join(parts)


def _link(url: str | None, label: str) -> str:
    safe = _http(url)
    if not safe:
        return _x(label)
    return f'<a href="{_x(safe)}">{_x(label)}</a>'


def anchor(name: str) -> str:
    """An invisible Confluence anchor macro (used to find our appended section)."""
    return ('<ac:structured-macro ac:name="anchor" ac:schema-version="1">'
            f'<ac:parameter ac:name="">{_x(name)}</ac:parameter></ac:structured-macro>')


def section_markers(doc: PublishDoc) -> tuple[str, str]:
    return f"nexus-{doc.nexus_id}-begin", f"nexus-{doc.nexus_id}-end"


def to_storage(doc: PublishDoc, *, as_section: bool = False) -> str:
    """Confluence storage XHTML for ``doc`` (a whole page, or a section to append).

    Both are wrapped in two invisible anchors, so a republish replaces only
    Nexus's own part and keeps whatever colleagues added to the page.
    """
    out: list[str] = []
    if as_section:
        out.append(f"<h2>{_x(doc.title)}</h2>")
    if doc.kind == "item":
        if doc.summary:
            out.append("<p><strong>Summary</strong></p>" + _storage_blocks(doc.summary))
        if doc.note:
            out.append("<p><strong>Analyst note</strong></p>" + _storage_blocks(doc.note))
    else:
        out.append(_storage_blocks(doc.body))
        if doc.sources:
            lis = []
            for s in doc.sources:
                when = f" (collected {_x(s.collected_at[:10])})" if s.collected_at else ""
                lis.append(f"<li>{_link(s.url, s.title)}{when}</li>")
            out.append("<p><strong>Sources</strong></p><ul>" + "".join(lis) + "</ul>")
    meta: list[str] = []
    if doc.source_url or doc.source_name:
        meta.append("Source: " + _link(doc.source_url, doc.source_name or doc.source_url or ""))
    if doc.collected_at:
        meta.append(f"Collected: {_x(doc.collected_at[:10])}")
    if doc.cases:
        meta.append("Case: " + _x(", ".join(doc.cases)))
    if doc.edited_at:
        meta.append(f"Edited: {_x(doc.edited_at[:16])}")
    if meta:
        out.append("<p>" + "<br />".join(meta) + "</p>")
    if doc.entities:
        out.append("<p>Entities: " + _x(", ".join(doc.entities)) + "</p>")
    out.append(f"<p><em>{_x(FOOTER)}</em></p>")
    begin, end = section_markers(doc)
    return anchor(begin) + "".join(out) + anchor(end)


def _anchor_re(name: str) -> str:
    # Bounded: page bodies are untrusted, so no unbounded backtracking.
    return (r'<ac:structured-macro\b[^>]{0,400}ac:name="anchor"[^>]{0,400}>\s{0,20}'
            r'<ac:parameter\b[^>]{0,200}>' + re.escape(name) +
            r'</ac:parameter>\s{0,20}</ac:structured-macro>')


def replace_section(page_body: str, doc: PublishDoc, section: str) -> tuple[str, bool]:
    """Replace our earlier section in ``page_body`` (``True``) or append (``False``)."""
    begin, end = section_markers(doc)
    b = re.search(_anchor_re(begin), page_body)
    if b:
        e = re.search(_anchor_re(end), page_body[b.end():])
        if e:
            stop = b.end() + e.end()
            return page_body[:b.start()] + section + page_body[stop:], True
    return page_body + section, False


# ------------------------------------------------------------- Markdown
def _md_escape(text: str) -> str:
    # Our end-of-note marker is an Obsidian comment; content may never fake it.
    return clean_text(text).replace("%%", "% %")


def _wikilink(name: str) -> str:
    from nexus.obsidian import _wikilink as wl

    return wl(name)


def _tag(text: str) -> str:
    slug = re.sub(r"[^\w/-]+", "-", text.strip().lower(), flags=re.U).strip("-")
    return slug[:60]


def frontmatter(doc: PublishDoc) -> dict:
    data: dict = {MARKER_KEY: MARKER_VALUE, ID_KEY: doc.nexus_id, "title": doc.title}
    if doc.source_name:
        data["source"] = doc.source_name
    if doc.source_url:
        data["url"] = doc.source_url
    if doc.cases:
        data["case"] = doc.cases[0] if len(doc.cases) == 1 else list(doc.cases)
    tags = ["nexus", f"nexus/{doc.kind}"] + [t for t in (_tag(c) for c in doc.cases) if t]
    data["tags"] = list(dict.fromkeys(tags))
    if doc.collected_at:
        data["collected"] = doc.collected_at
    if doc.edited_at:
        data["edited"] = doc.edited_at
    return data


def dump_frontmatter(data: dict) -> str:
    import yaml

    def esc(v):
        if isinstance(v, str):
            return _md_escape(v)
        if isinstance(v, list):
            return [esc(x) for x in v]
        return v

    data = {k: esc(v) for k, v in data.items()}
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{text}---\n"


def _md_blocks(text: str) -> str:
    parts = []
    for kind, lines in _blocks(_md_escape(text)):
        if kind == "p":
            parts.append(lines[0])
        else:
            parts.append("\n".join(f"- {li}" for li in lines))
    return "\n\n".join(parts)


def _md_link(url: str | None, label: str) -> str:
    safe = _http(url)
    label = _md_escape(label).replace("[", "(").replace("]", ")")
    return f"[{label}](<{safe}>)" if safe else label


def markdown_body(doc: PublishDoc) -> str:
    """The generated part of the note (without frontmatter and end marker)."""
    out = [f"# {_md_escape(doc.title)}", ""]
    if doc.kind == "item":
        if doc.summary:
            out += ["## Summary", "", _md_blocks(doc.summary), ""]
        if doc.note:
            out += ["## Analyst note", "", _md_blocks(doc.note), ""]
    else:
        out += [_md_blocks(doc.body), ""]
        if doc.sources:
            out += ["## Sources", ""]
            for s in doc.sources:
                when = f" (collected {s.collected_at[:10]})" if s.collected_at else ""
                out.append(f"- {_md_link(s.url, s.title)}{when}")
            out.append("")
    if doc.source_url:
        out.append(f"Source: {_md_link(doc.source_url, doc.source_name or doc.source_url)}")
    if doc.collected_at:
        out.append(f"Collected: {doc.collected_at[:10]}")
    if doc.cases:
        out.append("Case: " + ", ".join(_md_escape(c) for c in doc.cases))
    if doc.entities:
        out += ["", "Entities: " + ", ".join(_wikilink(e) for e in doc.entities)]
    out += ["", f"_{FOOTER}_"]
    return "\n".join(out).strip() + "\n"


def to_markdown(doc: PublishDoc, *, keep_tail: str = "") -> str:
    """A full Obsidian note. ``keep_tail`` is what the analyst wrote below our marker."""
    tail = keep_tail if keep_tail.strip() else "\n"
    return dump_frontmatter(frontmatter(doc)) + "\n" + markdown_body(doc) + "\n" + \
        KEEP_LINE + "\n" + tail.lstrip("\n")


# ------------------------------------------------------------- plain text
def to_text(doc: PublishDoc) -> str:
    out = [doc.title, ""]
    if doc.kind == "item":
        if doc.summary:
            out += [doc.summary, ""]
        if doc.note:
            out += ["Analyst note: " + doc.note, ""]
    else:
        out += [doc.body, ""]
        for s in doc.sources:
            out.append(f"- {s.title}" + (f" <{s.url}>" if s.url else ""))
    meta = []
    if doc.source_url:
        meta.append(f"Source: {doc.source_url}")
    if doc.collected_at:
        meta.append(f"Collected: {doc.collected_at[:10]}")
    if doc.cases:
        meta.append("Case: " + ", ".join(doc.cases))
    if doc.entities:
        meta.append("Entities: " + ", ".join(doc.entities))
    return clean_text("\n".join(out + meta)).strip()
