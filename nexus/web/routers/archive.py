"""Wayback Machine archiving for single items and whole cases."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from nexus import wayback
from nexus.db import get_connection
from nexus.storage import (
    case_archive_targets,
    get_case,
    get_item_archive,
    set_case_auto_archive,
    set_item_archive,
)
from nexus.web.common import TEMPLATES, _case_archive_ctx, _fmt_archive_time, _item_archive_ctx

router = APIRouter()


def _render_item_archive(request: Request, ctx: dict, status_code: int = 200) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "_item_archive.html", {"ar": ctx}, status_code=status_code
    )


def _item_url(conn, item_id: int) -> str | None:
    row = conn.execute("SELECT url FROM items WHERE id = ?", (item_id,)).fetchone()
    return None if row is None else (row["url"] or "")


@router.get("/items/{item_id}/archive", response_class=HTMLResponse)
def item_archive_status(request: Request, item_id: int) -> HTMLResponse:
    """The drawer's archive box (also its polling target while pending)."""
    with get_connection() as conn:
        url = _item_url(conn, item_id)
        if url is None:
            return HTMLResponse('<span class="text-rose-400">item not found</span>', status_code=404)
        ctx = _item_archive_ctx(conn, item_id, url)
    return _render_item_archive(request, ctx)


def _archive_precheck(conn, url: str, acknowledge: str | None) -> tuple[str, bool]:
    """Shared gate for archive actions: returns ``(message, needs_warning)``.

    An empty message and False means the action may proceed.
    """
    if not url:
        return "This item has no source link to archive.", False
    if not wayback.is_enabled(conn):
        return "Archiving is turned off in Settings.", False
    if acknowledge == "1":
        wayback.acknowledge_opsec(conn)
    if not wayback.opsec_acknowledged(conn):
        return "", True
    return "", False


@router.post("/items/{item_id}/archive", response_class=HTMLResponse)
def item_archive_save(
    request: Request, item_id: int, acknowledge: str | None = Form(default=None)
) -> HTMLResponse:
    """Queue a fresh Wayback Machine capture of the item's source."""
    with get_connection() as conn:
        url = _item_url(conn, item_id)
        if url is None:
            return HTMLResponse('<span class="text-rose-400">item not found</span>', status_code=404)
        msg, warn = _archive_precheck(conn, url, acknowledge)
        if warn:
            return _render_item_archive(request, _item_archive_ctx(conn, item_id, url, warn_action="save"))
        if not msg:
            blocked = wayback.check_url(url)
            if blocked:
                msg = blocked
            elif not wayback.get_queue().enqueue(item_id, url, conn=conn):
                msg = "A capture of this item is already in progress."
        ctx = _item_archive_ctx(conn, item_id, url, message=msg)
    return _render_item_archive(request, ctx)


@router.post("/items/{item_id}/archive/lookup", response_class=HTMLResponse)
def item_archive_lookup(
    request: Request, item_id: int, acknowledge: str | None = Form(default=None)
) -> HTMLResponse:
    """Find the newest existing Wayback snapshot (no new capture is made)."""
    with get_connection() as conn:
        url = _item_url(conn, item_id)
        if url is None:
            return HTMLResponse('<span class="text-rose-400">item not found</span>', status_code=404)
        msg, warn = _archive_precheck(conn, url, acknowledge)
        if warn:
            return _render_item_archive(request, _item_archive_ctx(conn, item_id, url, warn_action="lookup"))
        if msg:
            return _render_item_archive(request, _item_archive_ctx(conn, item_id, url, message=msg))
    # Network call outside the DB connection.
    res = wayback.lookup(url)
    ok = False
    with get_connection() as conn:
        if res.get("ok"):
            ok = True
            current = get_item_archive(conn, item_id) or {}
            newer_own = (
                current.get("status") == "done"
                and (current.get("archived_at") or "") >= (res.get("archived_at") or "")
            )
            if current.get("status") != "pending" and not newer_own:
                set_item_archive(
                    conn, item_id, url, "existing",
                    archive_url=res["archive_url"], archived_at=res.get("archived_at") or None,
                )
            msg = f"Found a snapshot from {_fmt_archive_time(res.get('archived_at')) or 'an unknown date'}."
        else:
            msg = res.get("error") or "No snapshot found."
        ctx = _item_archive_ctx(conn, item_id, url, message=msg, message_ok=ok)
    return _render_item_archive(request, ctx)


def _render_case_archive(request: Request, ctx: dict) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_case_archive.html", {"ca": ctx})


@router.get("/cases/{case_id}/archive", response_class=HTMLResponse)
def case_archive_status(request: Request, case_id: int) -> HTMLResponse:
    """Archive progress panel for a case (polled while captures are queued)."""
    with get_connection() as conn:
        if get_case(conn, case_id) is None:
            return HTMLResponse("Case not found", status_code=404)
        ctx = _case_archive_ctx(conn, case_id)
    return _render_case_archive(request, ctx)


@router.post("/cases/{case_id}/archive-all", response_class=HTMLResponse)
def case_archive_all(
    request: Request, case_id: int, acknowledge: str | None = Form(default=None)
) -> HTMLResponse:
    """Queue a capture for every pinned item that is not archived yet."""
    with get_connection() as conn:
        if get_case(conn, case_id) is None:
            return HTMLResponse("Case not found", status_code=404)
        msg, warn = _archive_precheck(conn, "-", acknowledge)
        if warn:
            return _render_case_archive(request, _case_archive_ctx(conn, case_id, warn_action="all"))
        if not msg:
            q = wayback.get_queue()
            queued = sum(
                1 for t in case_archive_targets(conn, case_id)
                if q.enqueue(int(t["id"]), t["url"], conn=conn)
            )
            msg = (f"Queued {queued} item(s) for archiving." if queued
                   else "Nothing to archive: every pinned link is archived or already queued.")
        ctx = _case_archive_ctx(conn, case_id, message=msg)
    return _render_case_archive(request, ctx)


@router.post("/cases/{case_id}/auto-archive", response_class=HTMLResponse)
def case_set_auto_archive(
    request: Request, case_id: int,
    enabled: str | None = Form(default=None),
    acknowledge: str | None = Form(default=None),
) -> HTMLResponse:
    """Opt this case in/out of archiving items automatically when pinned."""
    want_on = enabled == "1"
    with get_connection() as conn:
        if get_case(conn, case_id) is None:
            return HTMLResponse("Case not found", status_code=404)
        msg = ""
        if want_on:
            msg, warn = _archive_precheck(conn, "-", acknowledge)
            if warn:
                return _render_case_archive(request, _case_archive_ctx(conn, case_id, warn_action="auto"))
        if not msg:
            set_case_auto_archive(conn, case_id, want_on)
            msg = ("New pins in this case will be archived automatically." if want_on
                   else "Auto-archive is off for this case.")
        ctx = _case_archive_ctx(conn, case_id, message=msg)
    return _render_case_archive(request, ctx)
