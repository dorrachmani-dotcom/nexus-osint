"""Read-only cloud connectors (Sources > Cloud connectors)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.envstore import cloud_env_name, read_env_value, reload_settings, update_env
from nexus.ingest.upload import cloud_ai_active
from nexus.sources.cloud import CloudSource, connector_class, connector_classes, make_source
from nexus.storage import list_cases
from nexus.storage.cloud_sources import (
    MAX_ITEMS,
    create_cloud_source,
    delete_cloud_source,
    get_cloud_source,
    list_cloud_sources,
    set_cloud_scope,
    toggle_cloud_source,
)
from nexus.web.common import TEMPLATES

router = APIRouter()


def _context(error: str | None = None) -> dict:
    settings = get_settings()
    with get_connection() as conn:
        rows = list_cloud_sources(conn)
        cases = list_cases(conn)
    labels = {c.KIND: c for c in connector_classes()}
    for row in rows:
        row["token_set"] = bool(read_env_value(cloud_env_name(row["id"])))
        row["connector"] = labels.get(row["kind"])
    return {
        "cloud_sources": rows,
        "connectors": list(labels.values()),
        "cases": cases,
        "max_items": MAX_ITEMS,
        "cloud_ai": cloud_ai_active(settings),
        "active_provider": settings.active_provider(),
        "error": error,
    }


def _source(source_id: int) -> CloudSource | None:
    with get_connection() as conn:
        row = get_cloud_source(conn, source_id)
    return make_source(row, get_settings()) if row else None


def _list(request: Request, error: str | None = None) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_cloud_list.html", _context(error))


@router.get("/sources/cloud", response_class=HTMLResponse)
def cloud_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "cloud.html", _context())


@router.post("/sources/cloud", response_class=HTMLResponse)
async def cloud_create(request: Request) -> HTMLResponse:
    form = await request.form()
    kind = str(form.get("kind") or "")
    cls = connector_class(kind)
    token = str(form.get("token") or "").strip()
    account = str(form.get("account") or "").strip()
    if cls is None:
        return _list(request, "Unknown connector.")
    if not token:
        return _list(request, f"Paste the {cls.KEY_LABEL.lower()}.")
    if cls.ACCOUNT_LABEL and not account:
        return _list(request, f"Enter the {cls.ACCOUNT_LABEL.lower()}.")
    destination = str(form.get("destination") or "pool").strip()
    cfg = {
        "kind": kind, "name": str(form.get("name") or "") or cls.LABEL, "account": account,
        "max_items": str(form.get("max_items") or ""),
        "case_id": destination if destination.isdigit() else "",
        "in_pool": "1" if (not destination.isdigit() or form.get("also_pool")) else "0",
        "no_ai": "0" if form.get("allow_ai") else "1",
    }
    with get_connection() as conn:
        new_id = create_cloud_source(conn, cfg)
    update_env({cloud_env_name(new_id): token})
    reload_settings()
    return _list(request)


@router.post("/sources/cloud/{source_id}/toggle", response_class=HTMLResponse)
def cloud_toggle(request: Request, source_id: int) -> HTMLResponse:
    with get_connection() as conn:
        toggle_cloud_source(conn, source_id)
    return _list(request)


@router.post("/sources/cloud/{source_id}/delete", response_class=HTMLResponse)
def cloud_delete(request: Request, source_id: int) -> HTMLResponse:
    from nexus.ingest.connector import forget_source

    with get_connection() as conn:
        row = get_cloud_source(conn, source_id)
        if row:
            # What was read stays in its case / the pool (delete it under
            # Documents if wanted); only the connection and its state go.
            forget_source(conn, row["kind"], source_id, row["name"])
        delete_cloud_source(conn, source_id)
    update_env({cloud_env_name(source_id): ""})  # no orphan secret
    return _list(request)


@router.post("/sources/cloud/{source_id}/test", response_class=HTMLResponse)
async def cloud_test(request: Request, source_id: int) -> HTMLResponse:
    src = _source(source_id)
    if src is None:
        return HTMLResponse('<span class="text-rose-400">Connection not found.</span>')
    result = await run_in_threadpool(src.test_connection)
    return TEMPLATES.TemplateResponse(
        request, "_cloud_test.html",
        {"result": result, "source": src.cfg, "connector": src, "chosen": set(src.scope())},
    )


@router.post("/sources/cloud/{source_id}/scope", response_class=HTMLResponse)
async def cloud_scope(request: Request, source_id: int) -> HTMLResponse:
    form = await request.form()
    with get_connection() as conn:
        set_cloud_scope(conn, source_id, [str(v) for v in form.getlist("item")])
    return _list(request)


@router.post("/sources/cloud/{source_id}/scan", response_class=HTMLResponse)
async def cloud_scan(request: Request, source_id: int) -> HTMLResponse:
    """Read this connection now (new and changed entries only)."""
    from nexus.security.outbound import record

    src = _source(source_id)
    if src is None:
        return HTMLResponse('<span class="text-rose-400">Connection not found.</span>')
    if not src.is_available():
        noun = src.SCOPE_NOUN or "scope"
        return HTMLResponse('<span class="text-amber-300">Not ready: check the token'
                            + (f" and choose at least one of the {noun}." if src.NEEDS_SCOPE else ".")
                            + "</span>")
    result = await run_in_threadpool(src.ingest)
    record("source", src.name, "fetch", items=int(result.get("fetched") or 0),
           ok=not str(result.get("status") or "").startswith("error"))
    return TEMPLATES.TemplateResponse(
        request, "_cloud_test.html", {"result": {"ok": True, "scanned": True, **result}},
    )
