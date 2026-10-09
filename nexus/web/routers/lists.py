"""Analyst lists (collections of items)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.storage import (
    add_to_list,
    create_list,
    delete_list,
    enrich_feed_rows,
    get_active_case,
    get_list,
    list_cases,
    list_lists,
    list_member_items,
    remove_from_list,
    update_list,
)
from nexus.web.common import TEMPLATES, _render_card

router = APIRouter()


# --- Custom triage lists / lanes --------------------------------------------
@router.get("/lists", response_class=HTMLResponse)
def lists_page(request: Request) -> HTMLResponse:
    """Overview of all triage lists."""
    settings = get_settings()
    with get_connection() as conn:
        rows = list_lists(conn)
    return TEMPLATES.TemplateResponse(
        request, "lists.html", {"lists": rows, "status": settings.availability_report()}
    )


@router.post("/lists", response_class=HTMLResponse)
def lists_create(
    request: Request, name: str = Form(...), color: str = Form(default="slate")
) -> HTMLResponse:
    with get_connection() as conn:
        create_list(conn, name, color)
        rows = list_lists(conn)
    return TEMPLATES.TemplateResponse(request, "_list_overview.html", {"lists": rows})


@router.post("/lists/{list_id}/update", response_class=HTMLResponse)
def lists_update(request: Request, list_id: int, name: str = Form(...)) -> HTMLResponse:
    with get_connection() as conn:
        update_list(conn, list_id, name)
        rows = list_lists(conn)
    return TEMPLATES.TemplateResponse(request, "_list_overview.html", {"lists": rows})


@router.post("/lists/{list_id}/delete", response_class=HTMLResponse)
def lists_delete(request: Request, list_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_list(conn, list_id)
        rows = list_lists(conn)
    return TEMPLATES.TemplateResponse(request, "_list_overview.html", {"lists": rows})


@router.get("/lists/{list_id}", response_class=HTMLResponse)
def list_detail(request: Request, list_id: int) -> HTMLResponse:
    """View the items filed into one list (reuses the feed card)."""
    settings = get_settings()
    with get_connection() as conn:
        meta = get_list(conn, list_id)
        if meta is None:
            return HTMLResponse("List not found", status_code=404)
        items = list_member_items(conn, list_id)
        enrich_feed_rows(conn, items)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "list_detail.html",
        {
            "list": meta,
            "items": items,
            "count": len(items),
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
            "status": settings.availability_report(),
        },
    )


@router.post("/lists/{list_id}/items/{item_id}/add", response_class=HTMLResponse)
def list_add_item(
    request: Request, list_id: int, item_id: int,
    case_id: int | None = Form(default=None),
) -> HTMLResponse:
    with get_connection() as conn:
        add_to_list(conn, list_id, item_id)
        return _render_card(request, conn, item_id, case_id=case_id)


@router.post("/lists/{list_id}/items/{item_id}/remove", response_class=HTMLResponse)
def list_remove_item(
    request: Request, list_id: int, item_id: int,
    case_id: int | None = Form(default=None),
) -> HTMLResponse:
    with get_connection() as conn:
        remove_from_list(conn, list_id, item_id)
        return _render_card(request, conn, item_id, case_id=case_id)


@router.post("/lists/quick-add/{item_id}", response_class=HTMLResponse)
def list_quick_add(
    request: Request, item_id: int, name: str = Form(...),
    case_id: int | None = Form(default=None),
) -> HTMLResponse:
    """Create a new list from the card's inline form, scoped to a case if given."""
    with get_connection() as conn:
        new_id = create_list(conn, name, case_id=case_id)
        add_to_list(conn, new_id, item_id)
        return _render_card(request, conn, item_id, case_id=case_id)
