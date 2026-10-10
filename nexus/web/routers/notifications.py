"""Email connection wizard and the daily digest settings."""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.envstore import (
    reload_settings,
    update_env,
)
from nexus.mailer import (
    PROVIDERS as EMAIL_PROVIDERS,
    clear_verified as clear_email_verified,
)
from nexus.storage import (
    set_meta,
)
from nexus.web.common import TEMPLATES
from nexus.web.settings_context import (
    _EMAIL_PLAIN_FIELDS,
    _EMAIL_SECRET_FIELDS,
    _MAX_RECIPIENTS,
    _email_context,
    _settings_context,
)

router = APIRouter()


def _render_email(request: Request, **extra) -> HTMLResponse:
    settings = get_settings()
    ctx = {**_settings_context(settings), **_email_context(settings, **extra)}
    return TEMPLATES.TemplateResponse(request, "_email_settings.html", ctx)


@router.post("/settings/email/provider", response_class=HTMLResponse)
def settings_email_provider(request: Request, provider: str = Form("")) -> HTMLResponse:
    """Pick how email is sent (a preference, stored in DB meta, not a secret)."""
    provider = (provider or "").strip().lower()
    if provider not in EMAIL_PROVIDERS:
        return _render_email(request, email_msg="Unknown email provider.", email_ok=False)
    with get_connection() as conn:
        set_meta(conn, "email_provider", provider)
        clear_email_verified(conn)
    return _render_email(request)


@router.post("/settings/email", response_class=HTMLResponse)
async def settings_email_save(request: Request) -> HTMLResponse:
    """Save the email connection: plain fields are written (and shown back);
    secrets are written only when typed (blank = unchanged), always to .env."""
    from nexus.mailer import valid_address

    form = await request.form()
    updates: dict[str, str] = {}
    for key in _EMAIL_PLAIN_FIELDS:
        if key in form:
            updates[key] = str(form.get(key) or "").strip()
    for key in _EMAIL_SECRET_FIELDS:
        if form.get(f"clear_{key}"):
            updates[key] = ""
            continue
        value = str(form.get(key) or "").strip()
        if value:
            updates[key] = value

    problems: list[str] = []
    port = updates.get("SMTP_PORT")
    if port and not (port.isdigit() and 0 < int(port) < 65536):
        problems.append("the port must be a number such as 587 or 465")
    for key, label in (("SMTP_FROM", "From address"), ("SMTP_USERNAME", "email address")):
        addr = updates.get(key)
        if addr and "@" in addr and not valid_address(addr):
            problems.append(f"the {label} does not look like an email address")
    if "DIGEST_TO" in updates:
        recips = [r.strip() for r in updates["DIGEST_TO"].split(",") if r.strip()]
        if any(not valid_address(r) for r in recips):
            problems.append("every recipient must be a plain email address, separated by commas")
        elif len(recips) > _MAX_RECIPIENTS:
            problems.append(f"at most {_MAX_RECIPIENTS} recipients")
        updates["DIGEST_TO"] = ", ".join(recips)
    host = updates.get("SMTP_HOST")
    if host and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        problems.append("the SMTP server must be a host name such as smtp.example.com")
    if problems:
        return _render_email(request, email_msg="Not saved — " + "; ".join(problems) + ".", email_ok=False)

    settings = get_settings()
    changed = {
        k: v for k, v in updates.items()
        if (getattr(settings, k.lower(), None) or "") != v
    }
    if changed:
        update_env(changed)
        reload_settings()
        with get_connection() as conn:
            clear_email_verified(conn)
    msg = (
        "Saved. Now press “Send test email” — the daily brief unlocks after a successful test."
        if changed else "Nothing changed."
    )
    return _render_email(request, email_msg=msg, email_ok=True)


@router.post("/settings/email/test", response_class=HTMLResponse)
def settings_email_test(request: Request) -> HTMLResponse:
    """Send a short test email; on success the connection is marked verified."""
    from nexus.mailer import mark_verified, resolve_email_config, send_email

    settings = get_settings()
    cfg = resolve_email_config(settings)
    if not cfg.ready:
        return _render_email(
            request, email_ok=False,
            email_msg="Not sent — still missing: " + ", ".join(cfg.missing) + ".",
        )
    text = (
        "This is a test message from your local Nexus-OSINT.\n\n"
        "If you can read this, the email connection works and the daily brief "
        "written by Sherlock can now be switched on in Settings."
    )
    html = (
        "<p>This is a test message from your local <b>Nexus-OSINT</b>.</p>"
        "<p>If you can read this, the email connection works and the daily brief "
        "written by Sherlock can now be switched on in Settings.</p>"
    )
    result = send_email(cfg, "Nexus-OSINT: test email", text, html)
    if result.ok:
        with get_connection() as conn:
            mark_verified(conn, cfg)
        return _render_email(
            request, email_ok=True,
            email_msg=result.message + " Check the inbox (and spam folder). The daily brief can now be switched on.",
        )
    return _render_email(request, email_ok=False, email_msg=result.message)


@router.post("/settings/digest", response_class=HTMLResponse)
def settings_digest(
    request: Request,
    enabled: str = Form(""),
    digest_time: str = Form("08:00"),
    scan_first: str = Form(""),
) -> HTMLResponse:
    """Daily brief schedule: on/off (only once the email is verified), time, scan-first."""
    from nexus.digest import parse_digest_time
    from nexus.mailer import is_verified, resolve_email_config

    settings = get_settings()
    want_on = enabled == "1"
    h, m = parse_digest_time(digest_time)
    with get_connection() as conn:
        verified = is_verified(conn, resolve_email_config(settings))
        if want_on and not verified:
            return _render_email(
                request, digest_ok=False,
                digest_msg="Connect your email and pass “Send test email” first.",
            )
        set_meta(conn, "digest_enabled", "1" if want_on else "0")
        set_meta(conn, "digest_time", f"{h:02d}:{m:02d}")
        set_meta(conn, "digest_scan_first", "1" if scan_first == "1" else "0")
    msg = (f"Saved — the brief will be emailed daily at {h:02d}:{m:02d}." if want_on
           else "Saved — the daily email brief is off.")
    return _render_email(request, digest_ok=True, digest_msg=msg)


@router.post("/settings/digest/test", response_class=HTMLResponse)
def settings_digest_test(request: Request) -> HTMLResponse:
    """Compose and send a real brief now (marked [Test]); schedule untouched."""
    from nexus.digest import build_and_send_digest

    settings = get_settings()
    if not settings.email_enabled:
        return _render_email(
            request, digest_ok=False,
            digest_msg="Set up the email connection above before sending a brief.",
        )
    ok, message = build_and_send_digest(settings, test=True)
    return _render_email(request, digest_ok=ok, digest_msg=("Test brief sent. " if ok else "") + message)


# ------------------------------------------------- Connect Gmail (OAuth, send-only)


def _google_redirect_uri(request: Request) -> str:
    # Google Desktop clients accept any loopback port, so the callback is simply
    # this local server's own address.
    return str(request.base_url).rstrip("/") + "/settings/email/google/callback"


@router.get("/settings/email/google/connect")
def settings_email_google_connect(request: Request):
    """Send the operator to Google's consent screen (send-only permission)."""
    from fastapi.responses import RedirectResponse

    from nexus import google_oauth

    settings = get_settings()
    redirect_uri = _google_redirect_uri(request)
    if not (settings.google_oauth_client_id and settings.google_oauth_client_secret):
        return _render_email(request, email_ok=False, email_msg=(
            "Save your Google OAuth client ID and secret first (Step 2), then click “Connect Gmail”."))
    if not google_oauth.is_loopback_redirect(redirect_uri):
        return _render_email(request, email_ok=False, email_msg=(
            "Open Nexus-OSINT at http://127.0.0.1:8000 to connect Gmail."))
    with get_connection() as conn:
        set_meta(conn, "email_provider", "gmail_api")
    return RedirectResponse(
        google_oauth.start(settings.google_oauth_client_id, redirect_uri), status_code=303
    )


@router.get("/settings/email/google/callback")
def settings_email_google_callback(request: Request, code: str = "", state: str = "",
                                   error: str = ""):
    """Google sends the operator back here after the consent screen."""
    from fastapi.responses import RedirectResponse

    from nexus import google_oauth

    def back(msg: str, ok: bool) -> RedirectResponse:
        with get_connection() as conn:
            set_meta(conn, "email_flash", ("1|" if ok else "0|") + msg[:300])
        return RedirectResponse("/settings#email-settings", status_code=303)

    if error:
        return back("Gmail was not connected (Google said: " + error[:60] + ").", False)
    settings = get_settings()
    result = google_oauth.finish(
        settings.google_oauth_client_id, settings.google_oauth_client_secret or "", state, code
    )
    if not result.ok:
        return back(result.message, False)
    update_env({
        "GOOGLE_OAUTH_REFRESH_TOKEN": result.refresh_token,
        "GOOGLE_OAUTH_EMAIL": result.email,
    })
    reload_settings()
    with get_connection() as conn:
        set_meta(conn, "email_provider", "gmail_api")
        clear_email_verified(conn)
    who = f" as {result.email}" if result.email else ""
    return back(f"Gmail connected{who}. Now press “Send test email”.", True)


@router.post("/settings/email/google/disconnect", response_class=HTMLResponse)
def settings_email_google_disconnect(request: Request) -> HTMLResponse:
    """Revoke the permission at Google (best effort) and forget it locally."""
    from nexus import google_oauth

    settings = get_settings()
    if settings.google_oauth_refresh_token:
        google_oauth.revoke(settings.google_oauth_refresh_token)
    update_env({"GOOGLE_OAUTH_REFRESH_TOKEN": "", "GOOGLE_OAUTH_EMAIL": ""})
    reload_settings()
    with get_connection() as conn:
        clear_email_verified(conn)
    return _render_email(request, email_ok=True, email_msg=(
        "Gmail disconnected. The permission was revoked and removed from this computer."))
