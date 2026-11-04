"""Notion (read-only): the pages shared with an internal integration.

Notion decides the scope: an integration sees only the pages someone shared
with it, so sharing a page in Notion is how it is chosen. Pages are listed by
the search API, newest edit first, and stop at the previous scan's watermark;
each new or changed page's blocks are then read into plain text. Sub-pages are
pages in their own right and are listed on their own. Database rows are pages
too, and keep their properties as text.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from functools import partial

from nexus.ingest.connector import ConnectorDoc
from nexus.sources.cloud import OVERLAP, CloudError, CloudSource, Entry, parse_when

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
_PAGE_SIZE = 100
_MAX_DEPTH = 6        # nested blocks
_MAX_CHARS = 2_000_000
_SKIP_CHILDREN = {"child_page", "child_database"}  # listed on their own


def _plain(rich: object) -> str:
    if not isinstance(rich, list):
        return ""
    return "".join(str(r.get("plain_text") or "") for r in rich if isinstance(r, dict))


def page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title = _plain(prop.get("title"))
            if title:
                return title
    return "Untitled"


def _property_text(prop: dict) -> str:
    kind = prop.get("type")
    value = prop.get(kind) if kind else None
    if kind == "rich_text":
        return _plain(value)
    if kind in ("select", "status") and isinstance(value, dict):
        return str(value.get("name") or "")
    if kind == "multi_select" and isinstance(value, list):
        return ", ".join(str(v.get("name") or "") for v in value if isinstance(v, dict))
    if kind == "date" and isinstance(value, dict):
        return " to ".join(str(v) for v in (value.get("start"), value.get("end")) if v)
    if kind == "checkbox":
        return "yes" if value else "no"
    if kind in ("number", "url", "email", "phone_number") and value is not None:
        return str(value)
    return ""


def properties_text(page: dict) -> str:
    lines = []
    for name, prop in (page.get("properties") or {}).items():
        if isinstance(prop, dict) and prop.get("type") != "title":
            text = _property_text(prop)
            if text:
                lines.append(f"{name}: {text}")
    return "\n".join(lines)


def block_text(block: dict) -> str:
    kind = str(block.get("type") or "")
    body = block.get(kind)
    if kind == "child_page" and isinstance(body, dict):
        return f"Sub-page: {body.get('title') or ''}".rstrip()
    if kind == "table_row" and isinstance(body, dict):
        return " | ".join(_plain(c) for c in body.get("cells") or [])
    if not isinstance(body, dict):
        return ""
    text = _plain(body.get("rich_text"))
    if not text:
        return ""
    if kind.startswith("heading_"):
        return f"{'#' * int(kind[-1])} {text}"
    if kind in ("bulleted_list_item", "numbered_list_item"):
        return f"- {text}"
    if kind == "to_do":
        return f"[{'x' if body.get('checked') else ' '}] {text}"
    return text


class NotionSource(CloudSource):
    KIND = "notion"
    LABEL = "Notion"
    HOSTS = ("api.notion.com",)
    INTRO = ("Reads the pages shared with an internal integration. In Notion, open a page, "
             "then Connections, and add the integration: that is how pages are chosen.")
    KEY_LABEL = "Integration secret"
    KEY_HINT = "From notion.so/my-integrations. Read content is the only capability needed."

    def headers(self) -> dict[str, str]:
        return {**super().headers(), "Notion-Version": NOTION_VERSION}

    def _search(self, *, limit: int | None = None) -> Iterator[dict]:
        """Pages this integration can see, most recently edited first."""
        cursor = None
        seen = 0
        while True:
            body: dict = {"filter": {"property": "object", "value": "page"},
                          "sort": {"timestamp": "last_edited_time", "direction": "descending"},
                          "page_size": _PAGE_SIZE}
            if cursor:
                body["start_cursor"] = cursor
            data = self.json("POST", f"{API}/search", json=body)  # read-only search
            for page in data.get("results") or []:
                if isinstance(page, dict):
                    yield page
                    seen += 1
                    if limit and seen >= limit:
                        return
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return

    def test_connection(self) -> dict:
        try:
            me = self.json("GET", f"{API}/users/me")
            pages = list(self._search(limit=50))
        except CloudError as exc:
            return {"ok": False, "error": str(exc)}
        note = ("" if pages else "No pages are shared with this integration yet: "
                "add it to a page under Connections in Notion.")
        return {"ok": True, "account": str(me.get("name") or "integration"),
                "items": [{"key": str(p.get("id")), "name": page_title(p)} for p in pages],
                "note": note}

    def _blocks(self, block_id: str, depth: int, lines: list[str]) -> None:
        cursor = None
        while True:
            params: dict = {"page_size": _PAGE_SIZE}
            if cursor:
                params["start_cursor"] = cursor
            data = self.json("GET", f"{API}/blocks/{block_id}/children", params=params)
            for block in data.get("results") or []:
                if not isinstance(block, dict):
                    continue
                text = block_text(block)
                if text:
                    lines.append(text)
                if (block.get("has_children") and depth < _MAX_DEPTH
                        and block.get("type") not in _SKIP_CHILDREN):
                    self._blocks(str(block.get("id")), depth + 1, lines)
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return

    def _load(self, page: dict) -> ConnectorDoc:
        page_id = str(page.get("id"))
        lines: list[str] = []
        props = properties_text(page)
        if props:
            lines.append(props)
        self._blocks(page_id, 0, lines)
        parent = page.get("parent") or {}
        return ConnectorDoc(
            key=page_id, title=page_title(page), text="\n".join(lines)[:_MAX_CHARS],
            url=str(page.get("url") or "") or None,
            modified=parse_when(page.get("last_edited_time")),
            version=str(page.get("last_edited_time") or "") or None,
            meta={"parent_type": parent.get("type"), "archived": bool(page.get("archived"))},
        )

    def listing(self, since: datetime | None) -> Iterator[Entry]:
        floor = since - OVERLAP if since else None
        for page in self._search():
            modified = parse_when(page.get("last_edited_time"))
            if floor and modified and modified < floor:
                return  # newest first: the rest is older
            yield Entry(key=str(page.get("id")),
                        version=str(page.get("last_edited_time") or "") or None,
                        modified=modified, load=partial(self._load, page))
