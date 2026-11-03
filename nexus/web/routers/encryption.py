"""Encryption at rest: the Settings panel, enabling it, and the unlock page."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from nexus.security import atrest
from nexus.web.common import TEMPLATES

logger = logging.getLogger("nexus")
router = APIRouter()


def _panel(request: Request, **extra) -> HTMLResponse:
    ctx = {"enc": atrest.describe(), "error": None, "recovery_code": None, **extra}
    return TEMPLATES.TemplateResponse(request, "_encryption_settings.html", ctx)


@router.get("/settings/encryption", response_class=HTMLResponse)
def encryption_panel(request: Request) -> HTMLResponse:
    return _panel(request)


@router.post("/settings/encryption", response_class=HTMLResponse)
async def encryption_enable(request: Request) -> HTMLResponse:
    form = await request.form()
    passphrase = str(form.get("passphrase") or "")
    confirm = str(form.get("confirm") or "")
    if atrest.is_enabled():
        return _panel(request, error="Encryption is already on.")
    if not form.get("understand"):
        return _panel(request, error="Tick the box to confirm you understand the risk of losing the passphrase.")
    if passphrase != confirm:
        return _panel(request, error="The two passphrases do not match.")
    try:
        result = await run_in_threadpool(
            atrest.enable, passphrase, use_dpapi=bool(form.get("auto_unlock"))
        )
    except atrest.EncryptionError as exc:
        return _panel(request, error=str(exc))
    atrest.start_background_conversion()
    return _panel(request, recovery_code=result.recovery_code)


def _safe_next(raw: str) -> str:
    """Only a local path may be the post-unlock destination (no open redirect)."""
    parts = urlsplit(raw or "/")
    if parts.scheme or parts.netloc or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@router.get("/unlock", response_class=HTMLResponse)
def unlock_page(request: Request, next: str = "/") -> Response:
    if atrest.status() != "locked":
        return RedirectResponse(_safe_next(next), status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "unlock.html", {"next": _safe_next(next), "error": None, "wait": 0}
    )


@router.post("/unlock", response_class=HTMLResponse)
async def unlock_submit(request: Request) -> Response:
    form = await request.form()
    nxt = _safe_next(str(form.get("next") or "/"))
    if atrest.status() != "locked":
        return RedirectResponse(nxt, status_code=303)
    throttle = atrest.THROTTLE
    wait = throttle.wait_seconds()
    error: str | None
    if wait:
        error = f"Too many wrong attempts. Try again in {wait} s."
    else:
        try:
            await run_in_threadpool(atrest.unlock, str(form.get("passphrase") or ""))
        except atrest.WrongPassphraseError:
            throttle.failed()
            logger.warning("Unlock attempt with a wrong passphrase")
            error = "That passphrase (or recovery code) is not correct."
        else:
            throttle.succeeded()
            atrest.start_background_conversion()
            return RedirectResponse(nxt, status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "unlock.html", {"next": nxt, "error": error, "wait": throttle.wait_seconds()},
        status_code=429 if wait else 401,
    )
