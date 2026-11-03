"""Knowledge connectors: "Connect Confluence" (Sources > Confluence) and
"Obsidian: import vault" (Sources > Obsidian)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.envstore import confluence_env_name, read_env_value, reload_settings, update_env
from nexus.ingest.upload import cloud_ai_active
from nexus.netguard import internal_policy
from nexus.storage import list_cases
from nexus.storage.confluence_sources import (
    MAX_ITEMS,
    create_confluence_source,
    delete_confluence_source,
    get_confluence_source,
    list_confluence_sources,
    set_confluence_spaces,
    toggle_confluence_source,
)
from nexus.web.common import TEMPLATES

router = APIRouter()

_FORM_FIELDS = ("name", "deployment", "base_url", "email", "spaces", "cql", "max_items",
                "tls_mode")
_CHECKBOXES = ("include_blogposts", "include_comments", "include_attachments")


def _context() -> dict:
    settings = get_settings()
    with get_connection() as conn:
        rows = list_confluence_sources(conn)
        cases = list_cases(conn)
    for row in rows:
        row["token_set"] = bool(read_env_value(confluence_env_name(row["id"])))
    return {
        "confluence_sources": rows,
        "cases": cases,
        "max_items": MAX_ITEMS,
        "internal_enabled": internal_policy().enabled,
        "ca_bundle_set": bool(settings.internal_ca_bundle),
        "cloud_ai": cloud_ai_active(settings),
        "active_provider": settings.active_provider(),
    }


def _source(source_id: int):
    from nexus.sources.confluence import ConfluenceSource

    with get_connection() as conn:
        row = get_confluence_source(conn, source_id)
    return ConfluenceSource(row, get_settings()) if row else None


@router.get("/sources/confluence", response_class=HTMLResponse)
def confluence_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "confluence.html", _context())


@router.post("/sources/confluence", response_class=HTMLResponse)
async def confluence_create(request: Request) -> HTMLResponse:
    form = await request.form()
    cfg = {f: str(form.get(f) or "") for f in _FORM_FIELDS}
    for box in _CHECKBOXES:
        cfg[box] = "1" if form.get(box) else "0"
    destination = str(form.get("destination") or "pool").strip()
    cfg["case_id"] = destination if destination.isdigit() else ""
    cfg["in_pool"] = "1" if (not destination.isdigit() or form.get("also_pool")) else "0"
    cfg["no_ai"] = "0" if form.get("allow_ai") else "1"
    error = None
    token = str(form.get("token") or "").strip()
    if not cfg["base_url"].strip().lower().startswith(("http://", "https://")):
        error = "Enter the full http:// or https:// address of your Confluence."
    elif not token:
        error = "Paste the API token (Cloud) or Personal Access Token (Data Center)."
    elif cfg["deployment"] != "dc" and not cfg["email"].strip():
        error = "Confluence Cloud needs the e-mail address of the account the token belongs to."
    else:
        with get_connection() as conn:
            new_id = create_confluence_source(conn, cfg)
        update_env({confluence_env_name(new_id): token})
        reload_settings()
    ctx = _context()
    ctx["error"] = error
    return TEMPLATES.TemplateResponse(request, "_confluence_list.html", ctx)


@router.post("/sources/confluence/{source_id}/toggle", response_class=HTMLResponse)
def confluence_toggle(request: Request, source_id: int) -> HTMLResponse:
    with get_connection() as conn:
        toggle_confluence_source(conn, source_id)
    return TEMPLATES.TemplateResponse(request, "_confluence_list.html", _context())


@router.post("/sources/confluence/{source_id}/delete", response_class=HTMLResponse)
def confluence_delete(request: Request, source_id: int) -> HTMLResponse:
    from nexus.ingest.connector import forget_source

    with get_connection() as conn:
        row = get_confluence_source(conn, source_id)
        if row:
            # Pages already stored stay in their case / the pool (delete them
            # under Documents if wanted); only the connection and its state go.
            forget_source(conn, "confluence", source_id, row["name"])
        delete_confluence_source(conn, source_id)
    update_env({confluence_env_name(source_id): ""})  # no orphan secret
    return TEMPLATES.TemplateResponse(request, "_confluence_list.html", _context())


@router.post("/sources/confluence/{source_id}/test", response_class=HTMLResponse)
async def confluence_test(request: Request, source_id: int) -> HTMLResponse:
    """Connect, list the spaces this account can read, and offer them to pick."""
    src = _source(source_id)
    if src is None:
        return HTMLResponse('<span class="text-rose-400">Connection not found.</span>')
    result = await run_in_threadpool(src.test_connection)
    return TEMPLATES.TemplateResponse(
        request, "_confluence_test.html",
        {"result": result, "source": src.cfg, "chosen": set(src.spaces())},
    )


@router.post("/sources/confluence/{source_id}/spaces", response_class=HTMLResponse)
async def confluence_spaces(request: Request, source_id: int) -> HTMLResponse:
    form = await request.form()
    with get_connection() as conn:
        set_confluence_spaces(conn, source_id, [str(v) for v in form.getlist("space")])
    return TEMPLATES.TemplateResponse(request, "_confluence_list.html", _context())


@router.post("/sources/confluence/{source_id}/scan", response_class=HTMLResponse)
async def confluence_scan(request: Request, source_id: int) -> HTMLResponse:
    """Read this connection now (new and changed pages only)."""
    src = _source(source_id)
    if src is None:
        return HTMLResponse('<span class="text-rose-400">Connection not found.</span>')
    if not src.is_available():
        return HTMLResponse('<span class="text-amber-300">Not ready: check the token and '
                            "choose at least one space or enter a CQL query.</span>")
    result = await run_in_threadpool(src.ingest)
    return TEMPLATES.TemplateResponse(
        request, "_confluence_test.html", {"result": {"ok": True, "scanned": True, **result}},
    )


# ------------------------------------------------------ Obsidian: import vault
def _obsidian_context() -> dict:
    from nexus.storage.obsidian_vaults import list_obsidian_vaults

    settings = get_settings()
    with get_connection() as conn:
        rows = list_obsidian_vaults(conn)
        cases = list_cases(conn)
    return {"vaults": rows, "cases": cases, "cloud_ai": cloud_ai_active(settings),
            "active_provider": settings.active_provider()}


def _vault(vault_id: int):
    from nexus.sources.obsidian import ObsidianSource
    from nexus.storage.obsidian_vaults import get_obsidian_vault

    with get_connection() as conn:
        row = get_obsidian_vault(conn, vault_id)
    return ObsidianSource(row, get_settings()) if row else None


@router.get("/sources/obsidian", response_class=HTMLResponse)
def obsidian_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "obsidian.html", _obsidian_context())


@router.post("/sources/obsidian", response_class=HTMLResponse)
async def obsidian_create(request: Request) -> HTMLResponse:
    from nexus.sources.obsidian import check_vault
    from nexus.storage.obsidian_vaults import (
        create_obsidian_vault,
        delete_obsidian_vault,
        get_obsidian_vault,
    )

    form = await request.form()
    cfg = {f: str(form.get(f) or "") for f in ("name", "path", "include_folders",
                                               "exclude_folders", "required_tag")}
    destination = str(form.get("destination") or "pool").strip()
    cfg["case_id"] = destination if destination.isdigit() else ""
    cfg["in_pool"] = "1" if (not destination.isdigit() or form.get("also_pool")) else "0"
    cfg["no_ai"] = "0" if form.get("allow_ai") else "1"
    error = None
    with get_connection() as conn:
        new_id = create_obsidian_vault(conn, cfg)
        row = get_obsidian_vault(conn, new_id) or {}
        # Validate the stored folder (read-only check; nothing is read yet).
        _root, reason = check_vault(row.get("path"))
        if reason:
            delete_obsidian_vault(conn, new_id)
            error = f"Vault not added: {reason}."
    ctx = _obsidian_context()
    ctx["error"] = error
    return TEMPLATES.TemplateResponse(request, "_obsidian_list.html", ctx)


@router.post("/sources/obsidian/{vault_id}/toggle", response_class=HTMLResponse)
def obsidian_toggle(request: Request, vault_id: int) -> HTMLResponse:
    from nexus.storage.obsidian_vaults import toggle_obsidian_vault

    with get_connection() as conn:
        toggle_obsidian_vault(conn, vault_id)
    return TEMPLATES.TemplateResponse(request, "_obsidian_list.html", _obsidian_context())


@router.post("/sources/obsidian/{vault_id}/delete", response_class=HTMLResponse)
def obsidian_delete(request: Request, vault_id: int) -> HTMLResponse:
    from nexus.ingest.connector import forget_source
    from nexus.storage.obsidian_vaults import delete_obsidian_vault, get_obsidian_vault

    with get_connection() as conn:
        row = get_obsidian_vault(conn, vault_id)
        if row:
            # Notes already imported stay under Documents; the vault on disk is
            # never touched.
            forget_source(conn, "obsidian", vault_id, row["name"])
        delete_obsidian_vault(conn, vault_id)
    return TEMPLATES.TemplateResponse(request, "_obsidian_list.html", _obsidian_context())


@router.post("/sources/obsidian/{vault_id}/test", response_class=HTMLResponse)
async def obsidian_test(request: Request, vault_id: int) -> HTMLResponse:
    src = _vault(vault_id)
    if src is None:
        return HTMLResponse('<span class="text-rose-400">Vault not found.</span>')
    result = await run_in_threadpool(src.describe)
    return TEMPLATES.TemplateResponse(request, "_obsidian_test.html", {"result": result})


@router.post("/sources/obsidian/{vault_id}/scan", response_class=HTMLResponse)
async def obsidian_scan(request: Request, vault_id: int) -> HTMLResponse:
    src = _vault(vault_id)
    if src is None:
        return HTMLResponse('<span class="text-rose-400">Vault not found.</span>')
    result = await run_in_threadpool(src.ingest)
    return TEMPLATES.TemplateResponse(
        request, "_obsidian_test.html", {"result": {"ok": True, "scanned": True, **result}},
    )
