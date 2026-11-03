"""Response-format handling for user-defined API sources.

A custom API may answer in JSON, newline-delimited JSON, XML, CSV, an RSS/Atom
feed or plain HTML. This module detects which (from the Content-Type and a
sniff of the body) and turns the body into one JSON-like structure, so the rest
of the engine (dotted-path field mapping, Auto-Adapt) works the same way for
every format:

  * ``json``   — the decoded document, unchanged.
  * ``ndjson`` — a list of the decoded lines.
  * ``xml``    — ``{root_tag: {...}}`` (children and ``@attributes``), parsed
    with ``defusedxml`` (entity expansion and external references refused).
  * ``csv``    — a list of row dicts (header row = keys).
  * ``rss``    — a list of ``{title, link, summary, author, published, id}``.
  * ``html``   — one record ``{title, text, url}`` (main readable text).
  * ``text``   — one record ``{text}``.

Record formats (ndjson, csv, rss, html, text) yield the list of items
directly; JSON and XML are walked with the source's ``items_path``. Never
raises: an unparseable body becomes ``None``.
"""

from __future__ import annotations

import csv
import io
import json
import logging

from nexus.ingest.extract import decode_text

logger = logging.getLogger("nexus.sources.api_formats")

FORMATS = ("auto", "json", "ndjson", "xml", "csv", "rss", "html")
RECORD_FORMATS = {"ndjson", "csv", "rss", "html", "text"}
_MAX_RECORDS = 2000


def detect_format(content_type: str | None, body: bytes) -> str:
    """Best guess at the response format: Content-Type first, then a sniff."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    head = decode_text(body[:4096]).lstrip("﻿ \t\r\n")
    low = head[:1024].lower()
    if "ndjson" in ctype or "jsonl" in ctype or "json-seq" in ctype:
        return "ndjson"
    if ctype.endswith("json") or ctype.endswith("+json"):
        return "ndjson" if _looks_ndjson(body) else "json"
    if "rss" in ctype or "atom" in ctype:
        return "rss"
    if "csv" in ctype:
        return "csv"
    if "html" in ctype:
        return "html"
    if ctype.endswith("xml") or ctype.endswith("+xml"):
        return "rss" if _looks_feed(low) else "xml"
    # Unknown / text/plain / octet-stream: sniff the body.
    if head.startswith(("{", "[")):
        return "ndjson" if _looks_ndjson(body) else "json"
    if head.startswith("<"):
        if "<html" in low or "<!doctype html" in low:
            return "html"
        return "rss" if _looks_feed(low) else "xml"
    first = head.splitlines()[0] if head else ""
    if head.count("\n") >= 1 and any(sep in first for sep in (",", ";", "\t")):
        return "csv"
    return "text"


def _looks_feed(low: str) -> bool:
    return "<rss" in low or "<feed" in low or "<rdf:rdf" in low


def _looks_ndjson(body: bytes) -> bool:
    """Several non-empty lines that each decode as a JSON object."""
    lines = [ln for ln in decode_text(body[:65536]).splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    try:
        json.loads(decode_text(body))
        return False  # the whole body is one valid JSON document
    except ValueError:
        pass
    sample = lines[:5]
    try:
        return all(isinstance(json.loads(ln), dict) for ln in sample)
    except ValueError:
        return False


def parse_body(fmt: str, body: bytes, url: str = "") -> object | None:
    """Decode ``body`` in the given format to a JSON-like structure (or None)."""
    try:
        if fmt == "json":
            return json.loads(decode_text(body))
        if fmt == "ndjson":
            out: list = []
            for line in decode_text(body).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
                if len(out) >= _MAX_RECORDS:
                    break
            return out
        if fmt == "xml":
            return _parse_xml(body)
        if fmt == "csv":
            return _parse_csv(decode_text(body))
        if fmt == "rss":
            return _parse_feed(body)
        if fmt == "html":
            from nexus.ingest.html_text import extract_html

            doc = extract_html(decode_text(body))
            return [{"title": doc.title or "", "text": doc.text, "url": url}]
        return [{"text": decode_text(body).strip()}]
    except Exception as exc:  # malformed body: graceful
        logger.info("Could not parse a %s response (%s)", fmt, type(exc).__name__)
        return None


def _parse_xml(body: bytes) -> object | None:
    try:
        from nexus.ingest.extract import _elem_record, _local, _xml_root
    except ImportError:  # pragma: no cover - defensive
        return None
    try:
        root = _xml_root(body)
    except ImportError:
        logger.info("XML responses need the 'defusedxml' package")
        return None
    return {_local(root.tag) or "root": _elem_record(root)}


def xml_records(data: object) -> list | None:
    """Auto-find the repeated records in a converted XML document.

    Used when no ``items_path`` is set: the most common repeated child of the
    root (or of its single child) is treated as the list of items.
    """
    if not isinstance(data, dict) or len(data) != 1:
        return None
    node = next(iter(data.values()))
    for _ in range(3):
        if not isinstance(node, dict):
            return None
        lists = [v for k, v in node.items() if isinstance(v, list) and not k.startswith("@")]
        if lists:
            return max(lists, key=len)
        kids = [v for k, v in node.items() if not k.startswith("@")]
        if len(kids) != 1:
            return None
        node = kids[0]
    return None


def _parse_csv(text: str) -> list[dict]:
    sample = text[:4096]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows: list[dict] = []
    for row in reader:
        rows.append({str(k or "").strip(): (v or "").strip() if isinstance(v, str) else v
                     for k, v in row.items() if k is not None})
        if len(rows) >= _MAX_RECORDS:
            break
    return rows


def _parse_feed(body: bytes) -> list[dict]:
    import feedparser

    parsed = feedparser.parse(body)
    out: list[dict] = []
    for e in parsed.entries[:_MAX_RECORDS]:
        content = ""
        if e.get("content"):
            content = " ".join(str(c.get("value") or "") for c in e["content"])
        out.append({
            "title": e.get("title") or "",
            "link": e.get("link") or "",
            "summary": content or e.get("summary") or "",
            "author": e.get("author") or "",
            "published": e.get("published") or e.get("updated") or "",
            "id": e.get("id") or e.get("link") or "",
        })
    return out


def to_sample(data: object, limit: int = 8000) -> str | None:
    """A JSON text view of a parsed response, for Auto-Adapt / the planner."""
    try:
        return json.dumps(data, ensure_ascii=False, default=str)[:limit]
    except (TypeError, ValueError):
        return None


def sample_preview(sample: str, limit: int = 8000) -> tuple[str, str]:
    """``(format, json_text)`` for a pasted sample of any supported format.

    JSON is returned unchanged; anything else is converted the same way a live
    response would be, so the planner maps fields on what the engine sees.
    """
    raw = (sample or "").strip()
    body = raw.encode("utf-8")
    fmt = detect_format(None, body)
    if fmt == "json":
        return "json", raw[:limit]
    parsed = parse_body(fmt, body)
    if parsed is None:
        return fmt, raw[:limit]
    return fmt, to_sample(parsed, limit) or raw[:limit]
