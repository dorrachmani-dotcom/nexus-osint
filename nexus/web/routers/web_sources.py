"""Watched web pages / docs sites, and the opt-in internal-network allow-list."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.envstore import read_env_value, reload_settings, update_env, web_source_env_name
from nexus.netguard import internal_policy, parse_policy, tls_verify_for
from nexus.storage.web_sources import (
    MAX_DEPTH,
    MAX_PAGES,
    create_web_source,
    delete_web_source,
    get_web_source,
    list_web_sources,
    toggle_web_source,
)
from nexus.web.common import TEMPLATES

router = APIRouter()

_FORM_FIELDS = ("name", "url", "crawl", "max_depth", "max_pages", "stay_under_path",
                "delay_seconds", "tls_mode", "auth_type", "auth_param")


def _context() -> dict:
    with get_connection() as conn:
        rows = list_web_sources(conn)
    for row in rows:
        row["secret_set"] = bool(read_env_value(web_source_env_name(row["id"])))
    policy = internal_policy()
    return {
        "web_sources": rows,
        "max_depth": MAX_DEPTH,
        "max_pages": MAX_PAGES,
        "internal_enabled": policy.enabled,
        "ca_bundle_set": bool(get_settings().internal_ca_bundle),
    }


@router.get("/sources/web", response_class=HTMLResponse)
def web_sources_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "web_sources.html", _context())


@router.post("/sources/web", response_class=HTMLResponse)
async def web_source_create(request: Request) -> HTMLResponse:
    form = await request.form()
    cfg = {f: str(form.get(f) or "") for f in _FORM_FIELDS}
    # Unticked checkboxes are absent from the form.
    cfg["crawl"] = "1" if form.get("crawl") else "0"
    cfg["stay_under_path"] = "1" if form.get("stay_under_path") else "0"
    error = None
    if not cfg["url"].strip().lower().startswith(("http://", "https://")):
        error = "Enter a full http:// or https:// address."
    else:
        with get_connection() as conn:
            new_id = create_web_source(conn, cfg)
        secret = str(form.get("secret") or "").strip()
        if secret and cfg.get("auth_type") not in ("", "none"):
            update_env({web_source_env_name(new_id): secret})
            reload_settings()
    ctx = _context()
    ctx["error"] = error
    return TEMPLATES.TemplateResponse(request, "_web_source_list.html", ctx)


@router.post("/sources/web/{source_id}/toggle", response_class=HTMLResponse)
def web_source_toggle(request: Request, source_id: int) -> HTMLResponse:
    with get_connection() as conn:
        toggle_web_source(conn, source_id)
    return TEMPLATES.TemplateResponse(request, "_web_source_list.html", _context())


@router.post("/sources/web/{source_id}/delete", response_class=HTMLResponse)
def web_source_delete(request: Request, source_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_web_source(conn, source_id)
    update_env({web_source_env_name(source_id): ""})  # no orphan secret
    return TEMPLATES.TemplateResponse(request, "_web_source_list.html", _context())


@router.post("/sources/web/{source_id}/test", response_class=HTMLResponse)
async def web_source_test(request: Request, source_id: int) -> HTMLResponse:
    """Fetch only the start page and report what was found (stores nothing)."""
    from nexus.sources.webpage import WebPageSource

    with get_connection() as conn:
        row = get_web_source(conn, source_id)
    if not row:
        return HTMLResponse('<span class="text-rose-400">Source not found.</span>')
    src = WebPageSource(row, get_settings())
    if not src.is_available():
        result = {"ok": False, "error": "Not ready: check the address and, if the site "
                                        "needs a login, that its secret is set."}
    else:
        result = await run_in_threadpool(src.test_fetch)
    return TEMPLATES.TemplateResponse(request, "_web_source_test.html", {"result": result})


@router.post("/sources/web/{source_id}/scan", response_class=HTMLResponse)
async def web_source_scan(request: Request, source_id: int) -> HTMLResponse:
    """Scan just this source now: fetch, store new/changed pages, commit state."""
    from nexus.collector import Collector
    from nexus.sources.webpage import WebPageSource

    with get_connection() as conn:
        row = get_web_source(conn, source_id)
    if not row:
        return HTMLResponse('<span class="text-rose-400">Source not found.</span>')
    src = WebPageSource(row, get_settings())
    if not src.is_available():
        return HTMLResponse('<span class="text-amber-300">Not ready: check the address '
                            "and the login secret.</span>")
    collector = getattr(request.app.state, "collector", None) or Collector(get_settings())

    def run() -> tuple[int, int]:
        items = src.fetch()
        new, _alerts = collector._persist(src.name, items)
        src.commit_state()
        return len(items), new

    found, new = await run_in_threadpool(run)
    return TEMPLATES.TemplateResponse(
        request, "_web_source_test.html",
        {"result": {"ok": True, "scanned": True, "status": src.status,
                    "found": found, "new": new}},
    )


# ------------------------------------------------- internal network policy
@router.get("/settings/internal-network", response_class=HTMLResponse)
def internal_network_get(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "_internal_network_settings.html", _internal_ctx()
    )


def _internal_ctx(saved: bool = False, error: str | None = None) -> dict:
    s = get_settings()
    policy = internal_policy()
    return {
        "hosts": s.internal_allowed_hosts,
        "cidrs": s.internal_allowed_cidrs,
        "ca_bundle": s.internal_ca_bundle,
        "policy_hosts": policy.hosts,
        "policy_nets": [str(n) for n in policy.networks],
        "saved": saved,
        "error": error,
    }


@router.post("/settings/internal-network", response_class=HTMLResponse)
async def internal_network_save(request: Request) -> HTMLResponse:
    form = await request.form()
    hosts = " ".join(str(form.get("hosts") or "").split())[:2000]
    cidrs = " ".join(str(form.get("cidrs") or "").split())[:2000]
    ca = str(form.get("ca_bundle") or "").strip()[:500]
    # Store the normalised, comma-separated form of what was accepted.
    parsed = parse_policy(hosts, cidrs)
    update_env({
        "INTERNAL_ALLOWED_HOSTS": ",".join(parsed.hosts),
        "INTERNAL_ALLOWED_CIDRS": ",".join(str(n) for n in parsed.networks),
        "INTERNAL_CA_BUNDLE": ca,
    })
    reload_settings()
    error = None
    # Check the saved setting (not the raw form value) with the same resolver
    # the fetchers use; it falls back to normal verification if the file is missing.
    if ca and tls_verify_for("ca_bundle") is True:
        error = "The CA bundle path does not exist on this computer."
    return TEMPLATES.TemplateResponse(
        request, "_internal_network_settings.html", _internal_ctx(saved=True, error=error)
    )
