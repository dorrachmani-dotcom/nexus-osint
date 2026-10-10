"""Daily brief page and its unread counter."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.storage import (
    all_case_terms,
    case_new_count,
    case_new_items,
    enrich_feed_rows,
    get_active_case,
    list_cases,
    list_subscriptions,
    mark_read,
)
from nexus.web.common import TEMPLATES

logger = logging.getLogger("nexus")
router = APIRouter()


@router.get("/brief/count")
def brief_count() -> JSONResponse:
    """Total new tracked items across open cases since each was last opened.
    Polled by the nav to show a live 'unread' badge. Never raises."""
    total = 0
    try:
        with get_connection() as conn:
            for c in list_cases(conn, status="open", parent_id=None):
                total += case_new_count(conn, c["id"])
    except Exception:
        logger.debug("brief_count failed", exc_info=True)
    return JSONResponse({"total": total})


@router.post("/brief/mark-read", response_class=HTMLResponse)
def brief_mark_read(item_ids: list[int] = Form(default=[])) -> HTMLResponse:
    """Mark a set of items read; used by the Brief 'Mark read' per-block button."""
    with get_connection() as conn:
        for iid in item_ids:
            mark_read(conn, iid)
    return HTMLResponse(
        '<span class="text-emerald-400 text-xs mono">&#10003; Marked read</span>'
    )


@router.get("/brief", response_class=HTMLResponse)
def daily_brief(request: Request) -> HTMLResponse:
    """The analyst's morning read: what's new across your open cases since you
    last opened each one, top items first — the whole day's news in one screen."""
    settings = get_settings()
    with get_connection() as conn:
        cases = list_cases(conn, status="open", parent_id=None)
        blocks: list[dict] = []
        quiet_cases: list[dict] = []
        total_new = 0
        for c in cases:
            n = case_new_count(conn, c["id"])
            if n <= 0:
                quiet_cases.append(c)
                continue
            items = case_new_items(conn, c["id"], limit=6)
            enrich_feed_rows(conn, items)
            blocks.append({"case": c, "new_count": n, "new_items": items})
            total_new += n
        active = get_active_case(conn)
        # Readiness: surface the few setup gaps that matter, with a fix link.
        has_sources = bool(
            list_subscriptions(conn) or all_case_terms(conn) or settings.rss_feed_list
        )
        readiness: list[dict] = []
        if not settings.analysis_enabled:
            readiness.append({
                "label": "AI analysis is off",
                "hint": "Add an API key (Gemini / OpenAI / Anthropic / Grok) or run a local "
                        "model (Ollama or a local server such as LM Studio) so items get "
                        "summaries, scores and translations.",
                "link": "/settings", "cta": "Open Settings",
            })
        if not has_sources:
            readiness.append({
                "label": "No sources to collect from yet",
                "hint": "Add an RSS preset or feed, or give a case some tracking words.",
                "link": "/topics", "cta": "Add sources",
            })
        if not cases:
            readiness.append({
                "label": "No cases yet",
                "hint": "Open a case for a subject — the AI can set up its tracking words "
                        "and questions from a one-line brief.",
                "link": "/cases", "cta": "Create a case",
            })
    # Newest saved daily report per case (from the local reports folder).
    try:
        from nexus import daily_reports as dr

        latest_reports = [g["reports"][0] for g in dr.group_by_case(dr.list_reports(settings))]
    except Exception:
        latest_reports = []
    return TEMPLATES.TemplateResponse(
        request,
        "brief.html",
        {
            "blocks": blocks,
            "quiet_cases": quiet_cases,
            "total_new": total_new,
            "open_cases": len(cases),
            "readiness": readiness,
            "latest_reports": latest_reports[:12],
            "active_case": active,
            "analysis_enabled": settings.analysis_enabled,
            "status": settings.availability_report(),
        },
    )
