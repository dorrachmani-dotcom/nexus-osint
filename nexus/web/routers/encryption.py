"""Encryption at rest: the Settings panel, enabling it, and the unlock page."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from nexus.security import atrest
from nexus.web.common import TEMPLATES

logger = logging.getLogger("nexus")
router = APIRouter()
# One unlock attempt at a time: parallel guesses can neither skip the back-off
# nor run many 128 MiB scrypt derivations at once.
_UNLOCK_GATE = threading.Lock()


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


@router.get("/unlock", response_class=HTMLResponse)
def unlock_page(request: Request) -> Response:
    if atrest.status() != "locked":
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(request, "unlock.html", {"error": None, "wait": 0})


@router.post("/unlock", response_class=HTMLResponse)
async def unlock_submit(request: Request) -> Response:
    form = await request.form()
    if atrest.status() != "locked":
        return RedirectResponse("/", status_code=303)
    ok, error, wait = await run_in_threadpool(_attempt, str(form.get("passphrase") or ""))
    if ok:
        atrest.start_background_conversion()
        return RedirectResponse("/", status_code=303)
    throttle = atrest.THROTTLE
    return TEMPLATES.TemplateResponse(
        request, "unlock.html", {"error": error, "wait": throttle.wait_seconds()},
        status_code=429 if wait else 401,
    )


def _attempt(secret: str) -> tuple[bool, str | None, int]:
    """One unlock attempt, serialised; returns (ok, error, wait-before)."""
    throttle = atrest.THROTTLE
    with _UNLOCK_GATE:
        wait = throttle.wait_seconds()
        if wait:
            return False, f"Too many wrong attempts. Try again in {wait} s.", wait
        try:
            atrest.unlock(secret)
        except atrest.WrongPassphraseError:
            throttle.failed()
            logger.warning("Unlock attempt with a wrong passphrase")
            return False, "That passphrase (or recovery code) is not correct.", 0
        throttle.succeeded()
        return True, None, 0
