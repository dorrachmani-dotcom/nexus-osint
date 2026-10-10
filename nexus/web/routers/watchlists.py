"""Watchlists (keyword / regex / wallet / phone alerts)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.storage import (
    count_new_watchlist_hits,
    create_watchlist,
    delete_watchlist,
    get_active_case,
    list_watchlist_hits,
    list_watchlists,
    mark_watchlist_hits_seen,
    toggle_watchlist,
)
from nexus.web.common import TEMPLATES

router = APIRouter()


# --- Watchlists + alerts ----------------------------------------------------
WATCHLIST_KINDS = ["keyword", "regex", "wallet", "phone"]


@router.get("/watchlists/count")
def watchlists_count() -> dict:
    """Return unseen watchlist hit count for the nav badge."""
    with get_connection() as conn:
        return {"total": count_new_watchlist_hits(conn)}


@router.get("/watchlists", response_class=HTMLResponse)
def watchlists(request: Request) -> HTMLResponse:
    settings = get_settings()
    with get_connection() as conn:
        wls = list_watchlists(conn)
        hits = list_watchlist_hits(conn)
        active_case = get_active_case(conn)
        mark_watchlist_hits_seen(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "watchlists.html",
        {
            "watchlists": wls,
            "hits": hits,
            "active_case": active_case,
            "kinds": WATCHLIST_KINDS,
            "status": settings.availability_report(),
        },
    )


@router.post("/watchlists", response_class=HTMLResponse)
def watchlists_create(
    request: Request,
    label: str = Form(...),
    pattern: str = Form(...),
    kind: str = Form(default="keyword"),
) -> HTMLResponse:
    if kind not in WATCHLIST_KINDS:
        kind = "keyword"
    with get_connection() as conn:
        create_watchlist(conn, label.strip(), pattern.strip(), kind)
        wls = list_watchlists(conn)
    return TEMPLATES.TemplateResponse(
        request, "_watchlist_list.html", {"watchlists": wls}
    )


@router.post("/watchlists/{watchlist_id}/delete", response_class=HTMLResponse)
def watchlists_delete(request: Request, watchlist_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_watchlist(conn, watchlist_id)
        wls = list_watchlists(conn)
    return TEMPLATES.TemplateResponse(
        request, "_watchlist_list.html", {"watchlists": wls}
    )


@router.post("/watchlists/{watchlist_id}/toggle", response_class=HTMLResponse)
def watchlists_toggle(request: Request, watchlist_id: int) -> HTMLResponse:
    with get_connection() as conn:
        toggle_watchlist(conn, watchlist_id)
        wls = list_watchlists(conn)
    return TEMPLATES.TemplateResponse(
        request, "_watchlist_list.html", {"watchlists": wls}
    )
