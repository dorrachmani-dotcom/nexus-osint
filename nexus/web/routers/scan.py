"""Manual scans, background scan status and the investigation (capsule) scan."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from nexus.collector import Collector
from nexus.db import get_connection
from nexus.storage import (
    count_items,
    enrich_feed_rows,
    get_active_case,
    list_cases,
    list_lists,
    list_query_capsules,
    search_items,
)
from nexus.web.common import TEMPLATES, _filters, _paged_feed

router = APIRouter()


@router.post("/scan", response_class=HTMLResponse)
def scan(request: Request, scope: str | None = Form(default=None)) -> HTMLResponse:
    """Manual scan trigger — runs collection, then returns the refreshed feed
    (scoped to the analyst's tracked topics by default, like the feed itself)."""
    collector: Collector = request.app.state.collector
    stats = collector.scan()
    scope = (scope or "topics").strip()
    with get_connection() as conn:
        page = _paged_feed(conn, scope=scope)
        total = count_items(conn)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_feed.html",
        {
            "items": page["items"], "count": total, "scan_stats": stats,
            "matched": page["matched"], "has_more": page["has_more"],
            "next_offset": page["next_offset"], "remaining": page["remaining"],
            "all_lists": all_lists, "all_cases": all_cases, "active_case": active_case,
            "filters": _filters(None, None, None, None, None, None, False, scope),
            "topic_scoped": page["topic_scoped"],
        },
    )


@router.post("/scan/start", response_class=HTMLResponse)
def scan_start(request: Request) -> HTMLResponse:
    """Kick off a scan in the background and return a self-polling status chip,
    so the page never blocks for the minutes a scan can take."""
    from nexus import scanstate

    scanstate.start(request.app.state.collector)
    return TEMPLATES.TemplateResponse(request, "_scan_status.html", {"state": scanstate.state()})


@router.get("/scan/status", response_class=HTMLResponse)
def scan_status(request: Request) -> HTMLResponse:
    """Current background-scan status (polled by the status chip)."""
    from nexus import scanstate

    return TEMPLATES.TemplateResponse(request, "_scan_status.html", {"state": scanstate.state()})


def _capsule_terms(conn, name: str) -> list[str]:
    capsule = next(
        (c for c in list_query_capsules(conn) if c["name"] == name), None
    )
    return [t["value"] for t in capsule["terms"]] if capsule else []


@router.get("/investigation")
def investigation(name: str = "") -> Response:
    """Folded into the Case hub: redirect to the case of this name (its Live-feed
    tab), or to the cases overview if there's no match. Kept for old bookmarks."""
    from fastapi.responses import RedirectResponse

    target = (name or "").strip().lower()
    with get_connection() as conn:
        match = next(
            (c for c in list_cases(conn, parent_id=None)
             if c["name"].strip().lower() == target),
            None,
        )
    if match:
        return RedirectResponse(url=f"/cases/{match['id']}?tab=feed", status_code=302)
    return RedirectResponse(url="/cases", status_code=302)


@router.post("/investigation/scan", response_class=HTMLResponse)
def investigation_scan(request: Request, name: str = Form(...)) -> HTMLResponse:
    """Run a collection scan, then return this capsule's focused, filtered feed."""
    collector: Collector = request.app.state.collector
    stats = collector.scan()
    with get_connection() as conn:
        terms = _capsule_terms(conn, name)
        items = search_items(conn, terms=terms) if terms else []
        enrich_feed_rows(conn, items)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_feed.html",
        {
            "items": items, "count": len(items), "scan_stats": stats,
            "all_lists": all_lists, "all_cases": all_cases, "active_case": active_case,
        },
    )
