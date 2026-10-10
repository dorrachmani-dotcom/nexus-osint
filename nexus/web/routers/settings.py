"""Settings page, guide, auto-scan, AI provider, API keys, translation and
archive settings.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from nexus import wayback
from nexus.collector import Collector
from nexus.config import get_settings
from nexus.db import get_connection
from nexus.envstore import (
    EDITABLE_KEYS,
    reload_settings,
    update_env,
)
from nexus.mailer import (
    EMAIL_ENV_KEYS as _EMAIL_KEYS,
    clear_verified as clear_email_verified,
)
from nexus.storage import (
    get_meta,
    set_meta,
)
from nexus.web.common import TEMPLATES
from nexus.web.settings_context import (
    _archive_settings_ctx,
    _archive_settings_page_ctx,
    _email_context,
    _settings_context,
)

router = APIRouter()


@router.post("/settings/archive", response_class=HTMLResponse)
def settings_set_archive(request: Request, enabled: str | None = Form(default=None)) -> HTMLResponse:
    """Global on/off for Internet Archive features (a DB meta flag, not a secret)."""
    settings = get_settings()
    with get_connection() as conn:
        set_meta(conn, wayback.META_ENABLED, "1" if enabled == "1" else "0")
        ctx = _archive_settings_ctx(conn, settings, archive_saved=True)
    return TEMPLATES.TemplateResponse(request, "_archive_settings.html", ctx)


@router.get("/guide", response_class=HTMLResponse)
def guide(request: Request) -> HTMLResponse:
    """Beginner-friendly walkthrough: what each page does and how to use them."""
    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request,
        "guide.html",
        {"status": settings.availability_report()},
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Configuration overview, onboarding guide, and the secure key editor.

    API keys entered here are written to the local, git-ignored .env file only —
    never the database — and are never rendered back (the UI shows Set / Not set).
    The provider *choice* (not a secret) is stored in the DB meta table.
    """
    settings = get_settings()
    # One-time result of the "Connect Gmail" round trip, kept server-side (DB
    # meta) rather than in the URL so a crafted link cannot inject a message.
    with get_connection() as conn:
        flash = get_meta(conn, "email_flash") or ""
        if flash:
            set_meta(conn, "email_flash", None)
    ok, _, msg = flash.partition("|")
    return TEMPLATES.TemplateResponse(
        request, "settings.html",
        {**_settings_context(settings),
         **_email_context(settings, **({"email_msg": msg, "email_ok": ok == "1"} if msg else {})),
         **_archive_settings_page_ctx(settings)},
    )


@router.post("/settings/auto-scan", response_class=JSONResponse)
def settings_set_auto_scan(interval_hours: int = Form(default=0)) -> JSONResponse:
    """Set the automatic scan interval (0 = off, otherwise hours between scans)."""
    interval_hours = max(0, min(interval_hours, 168))  # cap at 1 week
    with get_connection() as conn:
        set_meta(conn, "auto_scan_interval", str(interval_hours))
        if interval_hours == 0:
            set_meta(conn, "last_auto_scan_at", None)
    return JSONResponse({"ok": True, "interval_hours": interval_hours})


@router.get("/settings/auto-scan-status", response_class=JSONResponse)
def settings_auto_scan_status() -> JSONResponse:
    """Return current auto-scan config and next scheduled time."""
    with get_connection() as conn:
        interval_h = int(get_meta(conn, "auto_scan_interval") or 0)
        last_raw = get_meta(conn, "last_auto_scan_at") or ""
    next_at = ""
    if interval_h > 0 and last_raw:
        try:
            last_dt = datetime.fromisoformat(last_raw)
            next_dt = last_dt + timedelta(hours=interval_h)
            next_at = next_dt.strftime("%H:%M")
        except ValueError:
            pass
    return JSONResponse({"interval_hours": interval_h, "next_at": next_at,
                         "last_at": last_raw[:16] if last_raw else ""})


@router.post("/settings/provider", response_class=HTMLResponse)
def settings_set_provider(request: Request, provider: str = Form(...)) -> HTMLResponse:
    """Switch the active AI provider (auto / a named backend / off). Not a secret."""
    settings = get_settings()
    if provider in settings.PROVIDER_CHOICES:
        with get_connection() as conn:
            set_meta(conn, "ai_provider", provider)
    return TEMPLATES.TemplateResponse(
        request, "_provider_status.html", _settings_context(settings)
    )


@router.post("/settings/secrets", response_class=HTMLResponse)
async def settings_set_secrets(request: Request) -> HTMLResponse:
    """Persist API keys to .env (never the DB) and reload settings live.

    A blank field leaves the existing value untouched, so one key can be updated
    without wiping the rest; ticking 'clear' removes a key. After writing, the
    cached settings are dropped and the collector is rebuilt so new keys take
    effect immediately — no restart required.
    """
    form = await request.form()
    updates: dict[str, str] = {}
    for key in EDITABLE_KEYS:
        if form.get(f"clear_{key}"):
            updates[key] = ""  # explicit removal
            continue
        value = str(form.get(key) or "").strip()
        if value:  # blank = leave as-is (don't overwrite an existing secret)
            updates[key] = value

    if updates:
        update_env(updates)
        reload_settings()
        if _EMAIL_KEYS.intersection(updates):
            # A changed email connection must pass a fresh test before sending.
            with get_connection() as conn:
                clear_email_verified(conn)
        # Rebuild the collector so its sources pick up the new keys without a restart.
        request.app.state.collector = Collector(get_settings())

    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request, "_settings_left.html", {**_settings_context(settings), "saved": bool(updates)}
    )


@router.post("/settings/translation", response_class=HTMLResponse)
async def settings_set_translation(request: Request) -> HTMLResponse:
    """Configure the keyless English-translation endpoint from the dashboard.

    Writes LIBRETRANSLATE_URL to .env (it is a plain URL, not a secret, so it is
    shown back), reloads settings live, and rebuilds the collector so the next
    scan uses it. A blank submission clears it (disabling the HTTP backend). The
    URL is validated against the shared SSRF guard so a private/loopback address
    is rejected with a clear message rather than silently failing at scan time.
    """
    from nexus.netguard import safe_http_url

    form = await request.form()
    url = str(form.get("libretranslate_url") or "").strip()

    if url:
        # Validate the /translate endpoint the same way nexus.translate will call it.
        ok, reason = safe_http_url(url.rstrip("/") + "/translate")
        if not ok:
            # Reject without writing — surface the reason so the operator can fix it.
            settings = get_settings()
            ctx = {
                **_settings_context(settings),
                "translation_saved": False,
                "translation_msg": f"Not saved — that address was refused: {reason}",
            }
            return TEMPLATES.TemplateResponse(request, "_settings_left.html", ctx)

    update_env({"LIBRETRANSLATE_URL": url})
    reload_settings()
    request.app.state.collector = Collector(get_settings())

    settings = get_settings()
    ctx = {
        **_settings_context(settings),
        "translation_saved": True,
        "translation_msg": (
            "Saved — non-English items will now be rendered to English on the next scan."
            if url else "Cleared — the keyless HTTP translation backend is now off."
        ),
    }
    return TEMPLATES.TemplateResponse(request, "_settings_left.html", ctx)
