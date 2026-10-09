"""The unified feed: dashboard, htmx feed partials, attention queue, bulk
actions and feed export.
"""

from __future__ import annotations

import logging
from datetime import UTC

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from nexus import wayback
from nexus.config import get_settings
from nexus.db import get_connection
from nexus.reporting import (
    feed_rows_to_csv,
    feed_rows_to_json,
    render_feed_report_html,
    render_report_pdf,
)
from nexus.storage import (
    add_bookmark,
    add_to_list,
    attention_count,
    attention_items,
    case_new_count,
    case_terms_map,
    count_items,
    count_matching_items,
    dismiss_item,
    distinct_sources,
    enrich_feed_rows,
    get_active_case,
    get_case,
    get_list,
    list_cases,
    list_lists,
    list_query_capsules,
    list_subscriptions,
    mark_read,
    search_items,
)
from nexus.web.common import (
    _MAX_EXPORT_ITEMS,
    TEMPLATES,
    THREAT_LEVELS,
    TOPIC_PRESETS,
    _feed_partial_response,
    _filters,
    _paged_feed,
    _setup_state,
    _topic_terms,
    _window_since_ts,
)

logger = logging.getLogger("nexus")
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def feed(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    window: str | None = None,
    unread: str | None = None,
    scope: str | None = None,
    show_dismissed: str | None = None,
    welcome: str | None = None,
) -> HTMLResponse:
    """The unified feed (The River) with full-text + faceted filtering.

    By default the feed is scoped to the analyst's tracked topics so it shows
    only what they're following — not the entire raw collection. ``scope=all``
    opens the firehose; an explicit search (``q``) always overrides scoping.

    ``welcome=1`` re-opens the getting-started steps even after setup is
    complete, so a user who forgot a step can always return to the explanations.
    """
    settings = get_settings()
    scope = (scope or "topics").strip()
    since_ts = _window_since_ts(window)
    unread_only = bool(unread)
    dismissed = bool(show_dismissed)
    with get_connection() as conn:
        capsules = list_query_capsules(conn)
        has_targets = bool(list_subscriptions(conn))
        page = _paged_feed(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, show_dismissed=dismissed, scope=scope,
        )
        total = count_items(conn)
        sources = distinct_sources(conn)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open", parent_id=None)
        _terms = case_terms_map(conn, [c["id"] for c in all_cases], kind="required")
        for _c in all_cases:
            _w = _terms.get(_c["id"], [])
            _c["new_count"] = case_new_count(conn, _c["id"], terms=_w)
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "feed.html",
        {
            "items": page["items"],
            "count": total,
            "matched": page["matched"],
            "has_more": page["has_more"],
            "next_offset": page["next_offset"],
            "remaining": page["remaining"],
            "status": settings.availability_report(),
            "sources": sources,
            "threat_levels": THREAT_LEVELS,
            "filters": _filters(q, source, threat, since, until, window,
                                unread_only, scope, dismissed),
            "has_topics": bool(capsules),
            "topic_scoped": page["topic_scoped"],
            "setup": _setup_state(settings, total, has_targets=has_targets),
            "force_getting_started": bool(welcome),
            "bundles": list(TOPIC_PRESETS.keys()),
            "capsules": capsules,
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
        },
    )


@router.get("/feed", response_class=HTMLResponse)
def feed_partial(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    window: str | None = None,
    unread: str | None = None,
    scope: str | None = None,
    show_dismissed: str | None = None,
) -> HTMLResponse:
    """htmx partial: the filtered feed list only (for live search/filtering)."""
    scope = (scope or "topics").strip()
    since_ts = _window_since_ts(window)
    unread_only = bool(unread)
    dismissed = bool(show_dismissed)
    with get_connection() as conn:
        page = _paged_feed(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, show_dismissed=dismissed, scope=scope,
        )
        total = count_items(conn)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_feed.html",
        {
            "items": page["items"], "count": total,
            "matched": page["matched"], "has_more": page["has_more"],
            "next_offset": page["next_offset"], "remaining": page["remaining"],
            "all_lists": all_lists,
            "all_cases": all_cases, "active_case": active_case,
            "filters": _filters(q, source, threat, since, until, window,
                                unread_only, scope, dismissed),
            "topic_scoped": page["topic_scoped"],
        },
    )


@router.get("/feed/page", response_class=HTMLResponse)
def feed_page(
    request: Request,
    offset: int = 0,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    window: str | None = None,
    unread: str | None = None,
    scope: str | None = None,
    show_dismissed: str | None = None,
) -> HTMLResponse:
    """htmx partial: the *next* page of feed cards (for the Load more button).

    Returns only the item cards plus a fresh load-more control, which htmx
    appends in place — so the existing rows (and any bulk selection) are kept.
    """
    scope = (scope or "topics").strip()
    since_ts = _window_since_ts(window)
    unread_only = bool(unread)
    dismissed = bool(show_dismissed)
    offset = max(int(offset or 0), 0)
    with get_connection() as conn:
        page = _paged_feed(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, show_dismissed=dismissed,
            scope=scope, offset=offset,
        )
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_feed_page.html",
        {
            "items": page["items"],
            "has_more": page["has_more"], "next_offset": page["next_offset"],
            "remaining": page["remaining"],
            "all_lists": all_lists, "all_cases": all_cases,
            "active_case": active_case,
            "filters": _filters(q, source, threat, since, until, window,
                                unread_only, scope, dismissed),
        },
    )


# --- "Needs your eyes": the few items that most warrant attention ------------
@router.get("/attention", response_class=HTMLResponse)
def attention(request: Request) -> HTMLResponse:
    """A short, ranked list of the unread items that most warrant attention,
    each with a plain reason it surfaced. Cuts triage from hundreds to a few."""
    settings = get_settings()
    with get_connection() as conn:
        items = attention_items(conn)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "attention.html",
        {
            "items": items,
            "count": len(items),
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
            "status": settings.availability_report(),
        },
    )


@router.get("/attention/count")
def attention_count_route() -> JSONResponse:
    """Unseen-attention count for the nav badge. Never raises."""
    total = 0
    try:
        with get_connection() as conn:
            total = attention_count(conn)
    except Exception:
        logger.debug("attention_count failed", exc_info=True)
    return JSONResponse({"total": total})


@router.post("/feed/read-all", response_class=HTMLResponse)
def feed_read_all(
    request: Request,
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    window: str | None = Form(default=None),
    unread: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> HTMLResponse:
    """Mark every item matching the current filter as read, then redraw the feed."""
    since_ts = _window_since_ts(window)
    _scope = (scope or "topics").strip()
    with get_connection() as conn:
        terms = (_topic_terms(conn) or None) if (not q and _scope == "topics") else None
        rows = search_items(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=bool(unread),
            terms=terms, limit=50000,
        )
        for r in rows:
            mark_read(conn, r["id"])
    return _feed_partial_response(
        request, q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread_only=bool(unread), scope=_scope,
    )


@router.post("/bulk/read", response_class=HTMLResponse)
def bulk_mark_read(
    request: Request,
    item_ids: list[int] = Form(default=[]),
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    window: str | None = Form(default=None),
    unread: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> HTMLResponse:
    """Mark every selected item read in one action, then redraw the feed."""
    with get_connection() as conn:
        for iid in item_ids:
            mark_read(conn, iid)
    return _feed_partial_response(
        request, q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread_only=bool(unread), scope=(scope or "topics"),
    )


@router.post("/bulk/dismiss", response_class=HTMLResponse)
def bulk_dismiss(
    request: Request,
    item_ids: list[int] = Form(default=[]),
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    window: str | None = Form(default=None),
    unread: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> HTMLResponse:
    """Dismiss every selected item, then redraw the feed."""
    with get_connection() as conn:
        for iid in item_ids:
            dismiss_item(conn, iid)
    return _feed_partial_response(
        request, q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread_only=bool(unread), scope=(scope or "topics"),
    )


@router.post("/bulk/lists/{list_id}/add", response_class=HTMLResponse)
def bulk_add_to_list(
    request: Request,
    list_id: int,
    item_ids: list[int] = Form(default=[]),
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    window: str | None = Form(default=None),
    unread: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> HTMLResponse:
    """Add every selected item to a triage list, then redraw the feed."""
    with get_connection() as conn:
        if get_list(conn, list_id) is not None:
            for iid in item_ids:
                add_to_list(conn, list_id, iid)
    return _feed_partial_response(
        request, q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread_only=bool(unread), scope=(scope or "topics"),
    )


@router.post("/bulk/cases/{case_id}/add", response_class=HTMLResponse)
def bulk_add_to_case(
    request: Request,
    case_id: int,
    item_ids: list[int] = Form(default=[]),
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    window: str | None = Form(default=None),
    unread: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> HTMLResponse:
    """Pin every selected item into an investigation case, then redraw the feed."""
    with get_connection() as conn:
        if get_case(conn, case_id) is not None:
            for iid in item_ids:
                add_bookmark(conn, iid, case_id)
                wayback.auto_archive_on_pin(conn, iid, case_id)
    return _feed_partial_response(
        request, q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread_only=bool(unread), scope=(scope or "topics"),
    )


def _filters_label(*, q, source, threat, since, until, window, unread, scope) -> str:
    """A short human description of the active filters for the export header."""
    parts: list[str] = []
    if q:
        parts.append(f'search "{q}"')
    parts.append("everything" if scope == "all" and not q else "tracked topics")
    if source:
        parts.append(f"source={source}")
    if threat:
        parts.append(f"threat={threat}")
    if window:
        parts.append(f"last {window}")
    if since:
        parts.append(f"from {since}")
    if until:
        parts.append(f"to {until}")
    if unread:
        parts.append("unread only")
    return ", ".join(parts)


@router.get("/export")
def feed_export(
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    window: str | None = None,
    unread: str | None = None,
    scope: str | None = None,
    format: str = "pdf",
) -> Response:
    """Export the feed (every item matching the current filters) to PDF or HTML.

    Mirrors the home-feed filters exactly so 'what you see is what you export'.
    Falls back to HTML when no PDF backend is installed, so export never fails.
    """
    scope = (scope or "topics").strip()
    since_ts = _window_since_ts(window)
    unread_only = bool(unread)
    with get_connection() as conn:
        terms = None
        if not q and scope == "topics":
            terms = _topic_terms(conn) or None
        items = search_items(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, terms=terms,
            limit=_MAX_EXPORT_ITEMS, offset=0,
        )
        enrich_feed_rows(conn, items)
        total_matched = count_matching_items(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, terms=terms,
        )

    from datetime import datetime

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    fmt = (format or "pdf").strip().lower()

    # Machine-readable exports for analysts who want the raw data in a spreadsheet
    # or another tool — same filtered rows as the report, no PDF backend needed.
    if fmt == "csv":
        return Response(
            content=feed_rows_to_csv(items),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="feed_export_{stamp}.csv"'},
        )
    if fmt == "json":
        return Response(
            content=feed_rows_to_json(items),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="feed_export_{stamp}.json"'},
        )

    label = _filters_label(
        q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread=unread_only, scope=scope,
    )
    html = render_feed_report_html(
        TEMPLATES, items=items, total_matched=total_matched, filters_label=label,
    )

    if fmt == "html":
        return HTMLResponse(html)

    pdf = render_report_pdf(html)
    if pdf is None:
        # Graceful fallback: serve HTML (the browser can still print to PDF).
        return HTMLResponse(html)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="feed_export_{stamp}.pdf"'
        },
    )
