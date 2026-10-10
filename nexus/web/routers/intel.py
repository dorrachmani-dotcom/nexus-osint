"""Intelligence requirements and the requirement-driven intel views."""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from nexus.collector import Collector
from nexus.config import get_settings
from nexus.db import get_connection
from nexus.reporting import (
    feed_rows_to_csv,
    feed_rows_to_json,
)
from nexus.storage import (
    add_requirement,
    delete_requirement,
    enrich_feed_rows,
    get_active_case,
    intel_items,
    list_requirements,
    set_requirement_enabled,
)
from nexus.web.common import _MAX_EXPORT_ITEMS, TEMPLATES

router = APIRouter()


# --- Intelligence requirements (PIRs) ---------------------------------------
# Standing questions the analyst wants answered. The AI scores collected items
# for relevance to each on every scan; the Intel view ranks by that score.
REQUIREMENT_PRIORITIES = {1, 2, 3}


def _to_int(value, default: int | None = None) -> int | None:
    """Parse an optional form/query value to int, tolerating '' and None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@router.get("/requirements", response_class=HTMLResponse)
def requirements_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    with get_connection() as conn:
        reqs = list_requirements(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "requirements.html",
        {
            "requirements": reqs,
            "status": settings.availability_report(),
            "analysis_enabled": settings.analysis_enabled,
        },
    )


@router.post("/requirements", response_class=HTMLResponse)
def requirements_create(
    request: Request, question: str = Form(...), priority: int = Form(default=2)
) -> HTMLResponse:
    if priority not in REQUIREMENT_PRIORITIES:
        priority = 2
    question = (question or "").strip()
    with get_connection() as conn:
        if question:
            add_requirement(conn, question, priority)
        reqs = list_requirements(conn)
    return TEMPLATES.TemplateResponse(
        request, "_requirement_list.html", {"requirements": reqs}
    )


@router.post("/requirements/{req_id}/toggle", response_class=HTMLResponse)
def requirements_toggle(request: Request, req_id: int) -> HTMLResponse:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT enabled FROM requirements WHERE id = ?", (req_id,)
        ).fetchone()
        if row is not None:
            set_requirement_enabled(conn, req_id, not bool(row["enabled"]))
        reqs = list_requirements(conn)
    return TEMPLATES.TemplateResponse(
        request, "_requirement_list.html", {"requirements": reqs}
    )


@router.post("/requirements/{req_id}/delete", response_class=HTMLResponse)
def requirements_delete(request: Request, req_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_requirement(conn, req_id)
        reqs = list_requirements(conn)
    return TEMPLATES.TemplateResponse(
        request, "_requirement_list.html", {"requirements": reqs}
    )


# --- Intel: items ranked by requirement relevance ---------------------------
def _intel_filters(requirement_id, min_score, since, until, q=None) -> dict:
    return {
        "requirement_id": str(requirement_id) if requirement_id else "",
        "min_score": str(min_score or 1),
        "since": since or "",
        "until": until or "",
        "q": q or "",
    }


@router.get("/intel", response_class=HTMLResponse)
def intel(
    request: Request,
    requirement_id: str | None = None,
    min_score: str | None = None,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
) -> HTMLResponse:
    """The relevance-ranked intelligence view."""
    settings = get_settings()
    rid = _to_int(requirement_id)
    score = _to_int(min_score, 1) or 1
    with get_connection() as conn:
        reqs = list_requirements(conn)
        items = intel_items(
            conn, requirement_id=rid, min_score=score, since=since, until=until, q=q
        )
        active_case = get_active_case(conn)
        enrich_feed_rows(conn, items)
    return TEMPLATES.TemplateResponse(
        request,
        "intel.html",
        {
            "items": items,
            "requirements": reqs,
            "status": settings.availability_report(),
            "filters": _intel_filters(rid, score, since, until, q),
            "analysis_enabled": settings.analysis_enabled,
            "has_requirements": bool(reqs),
            "active_case": active_case,
        },
    )


@router.get("/intel/export")
def intel_export(
    requirement_id: str | None = None,
    min_score: str | None = None,
    since: str | None = None,
    until: str | None = None,
    format: str = "csv",
) -> Response:
    """Download the relevance-ranked intelligence view as CSV or JSON data."""
    from datetime import datetime

    rid = _to_int(requirement_id)
    score = _to_int(min_score, 1) or 1
    with get_connection() as conn:
        rows = intel_items(
            conn, requirement_id=rid, min_score=score, since=since, until=until,
            limit=_MAX_EXPORT_ITEMS,
        )
    # Append the relevance score as an extra column — the whole point of this view.
    from nexus.reporting import DATA_EXPORT_FIELDS

    fields = [*DATA_EXPORT_FIELDS, "rel_score"]
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    if (format or "csv").strip().lower() == "json":
        return Response(
            content=feed_rows_to_json(rows, fields=fields),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="intel_export_{stamp}.json"'},
        )
    return Response(
        content=feed_rows_to_csv(rows, fields=fields),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="intel_export_{stamp}.csv"'},
    )


@router.get("/intel/feed", response_class=HTMLResponse)
def intel_feed(
    request: Request,
    requirement_id: str | None = None,
    min_score: str | None = None,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
) -> HTMLResponse:
    """htmx partial: the ranked list only (live filtering)."""
    rid = _to_int(requirement_id)
    score = _to_int(min_score, 1) or 1
    settings = get_settings()
    with get_connection() as conn:
        items = intel_items(
            conn, requirement_id=rid, min_score=score, since=since, until=until, q=q
        )
        has_requirements = bool(list_requirements(conn))
        active_case = get_active_case(conn)
        enrich_feed_rows(conn, items)
    return TEMPLATES.TemplateResponse(
        request,
        "_intel_feed.html",
        {
            "items": items,
            "analysis_enabled": settings.analysis_enabled,
            "has_requirements": has_requirements,
            "active_case": active_case,
        },
    )


@router.post("/intel/scan", response_class=HTMLResponse)
def intel_scan(
    request: Request,
    requirement_id: str | None = Form(default=None),
    min_score: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    q: str | None = Form(default=None),
) -> HTMLResponse:
    """Manual scan from the Intel view — runs collection + scoring, then ranks."""
    collector: Collector = request.app.state.collector
    stats = collector.scan()
    rid = _to_int(requirement_id)
    score = _to_int(min_score, 1) or 1
    settings = get_settings()
    with get_connection() as conn:
        items = intel_items(
            conn,
            requirement_id=rid,
            min_score=score,
            since=since or None,
            until=until or None,
            q=q or None,
        )
        has_requirements = bool(list_requirements(conn))
        active_case = get_active_case(conn)
        enrich_feed_rows(conn, items)
    return TEMPLATES.TemplateResponse(
        request,
        "_intel_feed.html",
        {
            "items": items,
            "scan_stats": stats,
            "analysis_enabled": settings.analysis_enabled,
            "has_requirements": has_requirements,
            "active_case": active_case,
        },
    )
