"""Per-item actions: bookmarks, read/dismiss state, detail, similar, verify
and evidence capture.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from nexus import wayback
from nexus.config import get_settings
from nexus.db import get_connection
from nexus.evidence import capture_evidence
from nexus.storage import (
    add_bookmark,
    decode_entities,
    dismiss_item,
    mark_read,
    mark_unread,
    remove_bookmark,
    undismiss_item,
)
from nexus.web.common import TEMPLATES, _item_archive_ctx, _render_card

router = APIRouter()


# --- Analyst workspace: bookmarks, cases, notes -----------------------------
@router.post("/items/{item_id}/bookmark", response_class=HTMLResponse)
def bookmark_item(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    """Bookmark an item. Returns the refreshed card for feed outerHTML swaps;
    callers with hx-swap=none (drawer, intel feed) ignore the response."""
    with get_connection() as conn:
        add_bookmark(conn, item_id, case_id)
        wayback.auto_archive_on_pin(conn, item_id, case_id)
        return _render_card(request, conn, item_id, case_id)


@router.post("/items/{item_id}/unbookmark", response_class=HTMLResponse)
def unbookmark_item(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    """Remove a bookmark. Returns the refreshed card for feed outerHTML swaps;
    drawer ignores the response (hx-swap=none)."""
    with get_connection() as conn:
        remove_bookmark(conn, item_id, None)
        return _render_card(request, conn, item_id, case_id)


@router.post("/items/{item_id}/evidence", response_class=HTMLResponse)
def capture_item_evidence(item_id: int) -> HTMLResponse:
    """Take a timestamped, hashed screenshot of the item's source (Vault)."""
    with get_connection() as conn:
        row = conn.execute("SELECT url FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return HTMLResponse('<span class="text-rose-400">item not found</span>', status_code=404)
    result = capture_evidence(item_id, row["url"])
    if result.get("ok"):
        return HTMLResponse(
            f'<span class="text-sky-400" title="{result["sha256"]}">&#128247; captured</span>'
        )
    # Escape the error: it can embed the item's (untrusted) host, so it must not
    # be able to break out of the title="" attribute.
    from markupsafe import escape

    return HTMLResponse(
        f'<span class="text-rose-400" title="{escape(result.get("error", ""))}">capture failed</span>'
    )


@router.post("/items/{item_id}/read", response_class=HTMLResponse)
def item_mark_read(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    """Mark an item read and return the refreshed (dimmed) card."""
    with get_connection() as conn:
        mark_read(conn, item_id)
        return _render_card(request, conn, item_id, case_id)


@router.post("/items/{item_id}/unread", response_class=HTMLResponse)
def item_mark_unread(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    with get_connection() as conn:
        mark_unread(conn, item_id)
        return _render_card(request, conn, item_id, case_id)


@router.post("/items/{item_id}/dismiss", response_class=HTMLResponse)
def item_dismiss_route(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    with get_connection() as conn:
        dismiss_item(conn, item_id)
        return _render_card(request, conn, item_id, case_id)


@router.post("/items/{item_id}/undismiss", response_class=HTMLResponse)
def item_undismiss_route(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    with get_connection() as conn:
        undismiss_item(conn, item_id)
        return _render_card(request, conn, item_id, case_id)


@router.get("/items/{item_id}/detail", response_class=HTMLResponse)
def item_detail(request: Request, item_id: int) -> HTMLResponse:
    """Return a rich detail fragment for the slide-in drawer."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT i.id, i.source, i.url, i.author, i.title, i.content, i.language,
                   i.published_at, i.fetched_at,
                   a.summary, a.translation, a.threat_level, a.confidence,
                   a.party, a.contradiction, a.entities,
                   COALESCE(i.dismissed, 0) AS dismissed,
                   CASE WHEN r.item_id IS NOT NULL THEN 1 ELSE 0 END AS is_read,
                   CASE WHEN bk.item_id IS NOT NULL THEN 1 ELSE 0 END AS is_bookmarked
            FROM items i
            LEFT JOIN analyses a ON a.item_id = i.id
            LEFT JOIN item_reads r ON r.item_id = i.id
            LEFT JOIN bookmarks bk ON bk.item_id = i.id
            WHERE i.id = ?
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            return HTMLResponse("<p class='text-rose-400 p-4'>Item not found.</p>", status_code=404)
        item = dict(row)
        item["entities_typed"] = decode_entities(item.get("entities"))
        # `reliability` is a heuristic derived from the source/URL (not a stored
        # column), so compute it the same way the feed cards do.
        from nexus.storage import _classify_reliability

        item["reliability"] = _classify_reliability(item.get("source"), item.get("url"))
        wl_hits = conn.execute(
            """SELECT w.label, w.kind FROM watchlist_hits h
               JOIN watchlists w ON w.id = h.watchlist_id WHERE h.item_id = ?""",
            (item_id,),
        ).fetchall()
        cases = conn.execute(
            """SELECT DISTINCT c.id, c.name, COALESCE(c.priority, 'medium') AS priority
               FROM bookmarks b
               JOIN cases c ON c.id = b.case_id
               WHERE b.item_id = ? AND b.case_id IS NOT NULL""",
            (item_id,),
        ).fetchall()
        lists_ = conn.execute(
            """SELECT l.id, l.name, l.color FROM list_memberships m
               JOIN lists l ON l.id = m.list_id WHERE m.item_id = ?""",
            (item_id,),
        ).fetchall()
        pinned_case_ids = {r["id"] for r in cases}
        open_cases = conn.execute(
            "SELECT id, name FROM cases WHERE status = 'open' ORDER BY priority DESC, name",
        ).fetchall()
        archive_ctx = _item_archive_ctx(conn, item_id, item.get("url") or "")
    return TEMPLATES.TemplateResponse(
        request,
        "_item_detail.html",
        {
            "item": item,
            "wl_hits": [dict(r) for r in wl_hits],
            "cases": [dict(r) for r in cases],
            "lists": [dict(r) for r in lists_],
            "open_cases": [dict(r) for r in open_cases],
            "pinned_case_ids": pinned_case_ids,
            "ar": archive_ctx,
        },
    )


@router.post("/items/{item_id}/similar", response_class=HTMLResponse)
def item_similar(request: Request, item_id: int) -> HTMLResponse:
    """Semantic 'find similar' for an item (local Ollama embeddings; graceful)."""
    from nexus.embeddings import find_similar

    with get_connection() as conn:
        result = find_similar(conn, item_id, get_settings())
    return TEMPLATES.TemplateResponse(request, "_similar_result.html", {"r": result})


@router.post("/items/{item_id}/verify", response_class=HTMLResponse)
def item_verify(request: Request, item_id: int) -> HTMLResponse:
    """Cross-check an item's claim against other collected items (AI-assisted).
    Returns a verdict + the corroborating / contradicting sources."""
    from nexus.verify import verify_item

    with get_connection() as conn:
        result = verify_item(conn, item_id, get_settings())
    return TEMPLATES.TemplateResponse(
        request, "_verify_result.html", {"v": result, "item_id": item_id}
    )
