"""Sources home and user-defined custom API sources."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.envstore import (
    custom_source_env_name,
    custom_source_headers_configured,
    custom_source_headers_env_name,
    custom_source_key_configured,
    reload_settings,
    update_env,
)
from nexus.storage import (
    create_custom_source,
    delete_custom_source,
    get_custom_source,
    list_custom_sources,
    toggle_custom_source,
    update_custom_source,
)
from nexus.web.common import TEMPLATES

router = APIRouter()


# --------------------------------------------------------------- custom sources
# Self-service "add any JSON API as a source". The AI (when connected) fills in
# the config from docs/sample; otherwise the analyst fills the fields by hand.
# The API key (if any) goes to .env (CUSTOM_SOURCE_<id>_KEY) — never the DB.
_CUSTOM_FORM_FIELDS = (
    "name", "base_url", "endpoint", "http_method", "auth_type", "auth_param",
    "query_param", "extra_params", "items_path",
    "map_title", "map_content", "map_url", "map_author", "map_published", "notes",
    "response_format", "pagination", "page_param", "page_size_param", "page_size",
    "page_start", "cursor_path", "max_pages", "body_template", "extra_headers",
    "tls_mode", "oauth_token_url", "oauth_scope",
)
_KEYED_AUTH = ("header", "query", "bearer", "basic", "oauth2")


def _custom_cfg_from_form(form) -> dict:
    return {field: (form.get(field) or "") for field in _CUSTOM_FORM_FIELDS}


def _save_custom_secrets(source_id: int, cfg: dict, form) -> str | None:
    """Write the key and secret headers (if given) to .env. Returns an error
    message for unusable secret headers, else None. Blank fields keep the
    stored values."""
    import json

    updates: dict[str, str] = {}
    api_key = str(form.get("api_key") or "").strip()
    if api_key and cfg.get("auth_type") in _KEYED_AUTH:
        updates[custom_source_env_name(source_id)] = api_key
    error = None
    raw_headers = str(form.get("secret_headers") or "").strip()
    if raw_headers:
        try:
            parsed = json.loads(raw_headers)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and all(isinstance(v, (str, int)) for v in parsed.values()):
            updates[custom_source_headers_env_name(source_id)] = json.dumps(
                {str(k): str(v) for k, v in parsed.items()}
            )
        else:
            error = ('Secret headers must be a JSON object such as {"X-Api-Key": "..."}; '
                     "they were not saved.")
    if updates:
        update_env(updates)
        reload_settings()
    return error


def _custom_sources_context() -> dict:
    settings = get_settings()
    with get_connection() as conn:
        rows = list_custom_sources(conn)
    for row in rows:
        row["key_set"] = custom_source_key_configured(row["id"])
        row["headers_set"] = custom_source_headers_configured(row["id"])
    return {
        "custom_sources": rows,
        "ai_enabled": settings.analysis_enabled,
        "active_provider": settings.active_provider(),
        "status": settings.availability_report(),
    }


@router.get("/sources")
def sources_home() -> Response:
    """Canonical entry for the unified 'where data comes from' area. The Feeds &
    presets tab is the default; Custom APIs is the second tab."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/topics", status_code=302)


@router.get("/sources/custom", response_class=HTMLResponse)
def custom_sources_page(request: Request) -> HTMLResponse:
    """List user-defined API sources and the add-a-source form."""
    return TEMPLATES.TemplateResponse(
        request, "custom_sources.html", _custom_sources_context()
    )


@router.post("/sources/custom/plan", response_class=HTMLResponse)
async def custom_source_plan(request: Request) -> HTMLResponse:
    """Ask the connected AI to draft a source config from docs/sample/hint.

    Returns the add-source form pre-filled with the AI's draft for review. If no
    AI is connected (or the reply is unusable), returns the form with an error
    banner so the analyst can fill it in manually.
    """
    from nexus.analysis.source_planner import plan_source

    form = await request.form()
    result = plan_source(
        docs_url=str(form.get("docs_url") or "").strip() or None,
        sample=str(form.get("sample") or "").strip() or None,
        hint=str(form.get("hint") or "").strip() or None,
    )
    ctx: dict = {"ai_enabled": get_settings().analysis_enabled}
    if result.get("ok"):
        ctx["cfg"] = result["config"]
        ctx["plan_ok"] = True
    else:
        ctx["error"] = result.get("error")
        ctx["cfg"] = {}
    return TEMPLATES.TemplateResponse(request, "_custom_source_form.html", ctx)


@router.post("/sources/custom", response_class=HTMLResponse)
async def custom_source_create(request: Request) -> HTMLResponse:
    """Persist a new custom source; write its API key (if given) to .env."""
    form = await request.form()
    cfg = _custom_cfg_from_form(form)
    with get_connection() as conn:
        new_id = create_custom_source(conn, cfg)
    error = _save_custom_secrets(new_id, cfg, form)
    ctx = {**_custom_sources_context(), "list_error": error}
    return TEMPLATES.TemplateResponse(request, "_custom_source_list.html", ctx)


@router.post("/sources/custom/{source_id}/update", response_class=HTMLResponse)
async def custom_source_update(request: Request, source_id: int) -> HTMLResponse:
    """Update a custom source's config (and rotate its key if a new one is given)."""
    form = await request.form()
    cfg = _custom_cfg_from_form(form)
    with get_connection() as conn:
        update_custom_source(conn, source_id, cfg)
    error = _save_custom_secrets(source_id, cfg, form)
    ctx = {**_custom_sources_context(), "list_error": error}
    return TEMPLATES.TemplateResponse(request, "_custom_source_list.html", ctx)


@router.post("/sources/custom/{source_id}/toggle", response_class=HTMLResponse)
def custom_source_toggle(request: Request, source_id: int) -> HTMLResponse:
    with get_connection() as conn:
        toggle_custom_source(conn, source_id)
    return TEMPLATES.TemplateResponse(
        request, "_custom_source_list.html", _custom_sources_context()
    )


@router.post("/sources/custom/{source_id}/delete", response_class=HTMLResponse)
def custom_source_delete(request: Request, source_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_custom_source(conn, source_id)
    # Clear its secret from .env so no orphan key lingers.
    update_env({custom_source_env_name(source_id): "",
                custom_source_headers_env_name(source_id): ""})
    return TEMPLATES.TemplateResponse(
        request, "_custom_source_list.html", _custom_sources_context()
    )


@router.post("/sources/custom/{source_id}/test", response_class=HTMLResponse)
def custom_source_test(request: Request, source_id: int) -> HTMLResponse:
    """Do a live fetch against one source and report the result in plain words.

    Runs in a worker thread (the source uses blocking httpx). Never raises — a
    failure is reported as a red status, matching graceful degradation.
    """
    from nexus.sources.custom import CustomApiSource

    with get_connection() as conn:
        row = get_custom_source(conn, source_id)
    if not row:
        return HTMLResponse('<span class="text-rose-400">Source not found.</span>')

    result: dict = {"name": row.get("name")}
    try:
        src = CustomApiSource(row, get_settings())
        if not src.is_available():
            result["error"] = (
                "Not ready: check the base URL and (if the API needs a key) that "
                "the key is set."
            )
        else:
            items = src.fetch()
            result["count"] = len(items)
            result["format"] = src.detected_format
            if not items and src.last_error:
                result["error"] = src.last_error
            elif items and items[0].raw.get("flattened"):
                result["flattened"] = True
            if items:
                first = items[0]
                result["sample_title"] = first.title or (first.content or "")[:120]
                result["sample_url"] = first.url
    except Exception as exc:  # pragma: no cover - defensive
        result["error"] = str(exc)
    return TEMPLATES.TemplateResponse(
        request, "_custom_source_test.html", {"result": result}
    )
