"""Single-entity profile page and its item list."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from nexus.adapters.registry import get_adapter
from nexus.config import get_settings
from nexus.db import get_connection
from nexus.storage import (
    entity_profile,
    get_active_case,
    list_cases,
    list_lists,
)
from nexus.web.common import TEMPLATES

router = APIRouter()


# --- Entity dossier: everything about one entity in one place ---------------
@router.get("/entity", response_class=HTMLResponse)
def entity_page(request: Request, name: str = "") -> Response:
    """One entity's dossier: stats, the items mentioning it, who it co-occurs
    with, and the cases it appears in. Reachable by clicking any entity name."""
    settings = get_settings()
    name = (name or "").strip()
    if not name:
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="/graph", status_code=302)
    with get_connection() as conn:
        profile = entity_profile(conn, name)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    # Contextual pivots: if this entity looks like an identifier (email / phone /
    # domain / url / handle), offer one-click runs of the matching PASSIVE OSINT
    # tools. Unavailable tools are shown with their install hint, never hidden.
    pivot_type, pivot_tools = None, []
    if profile and profile.get("item_count"):
        from nexus.toolguide import TOOL_CATALOG, pivot_tools_for

        pivot_type, keys = pivot_tools_for(profile["name"], profile.get("kind"))
        if keys:
            cat = {t["key"]: t for t in TOOL_CATALOG}
            for k in keys:
                adapter = get_adapter(k, settings)
                meta = cat.get(k, {})
                pivot_tools.append({
                    "key": k,
                    "title": meta.get("title", k),
                    "available": bool(adapter and adapter.is_available()),
                    "install": meta.get("install", ""),
                })
    return TEMPLATES.TemplateResponse(
        request,
        "entity.html",
        {
            "profile": profile,
            "name": name,
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
            "pivot_type": pivot_type,
            "pivot_tools": pivot_tools,
            "status": settings.availability_report(),
        },
    )


@router.get("/entity/items", response_class=HTMLResponse)
def entity_items(request: Request, name: str = "", offset: int = 0) -> HTMLResponse:
    """htmx 'load more' for the entity dossier's item list."""
    offset = max(int(offset or 0), 0)
    with get_connection() as conn:
        profile = entity_profile(conn, name, offset=offset)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_entity_items.html",
        {
            "profile": profile,
            "name": name,
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
        },
    )
