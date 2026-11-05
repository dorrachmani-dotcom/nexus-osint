"""The optional app password: sign-in page, sign-out and the Settings panel."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from nexus.security import apppass
from nexus.web.common import TEMPLATES

router = APIRouter()


def safe_next(target: str) -> str:
    """Only a path on this app: a sign-in link must not lead anywhere else."""
    parts = urlsplit(target)
    if (not target.startswith("/") or target.startswith(("//", "/\\"))
            or parts.scheme or parts.netloc or any(ord(ch) < 32 for ch in target)):
        return "/"
    return target


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(apppass.COOKIE, token, httponly=True, samesite="strict", path="/")


def _panel(request: Request, **extra) -> HTMLResponse:
    ctx = {"password_on": apppass.is_enabled(), "min_length": apppass.MIN_LENGTH,
           "error": None, "notice": None, **extra}
    return TEMPLATES.TemplateResponse(request, "_password_settings.html", ctx)


def _login_page(request: Request, target: str, error: str | None = None,
                status_code: int = 200) -> HTMLResponse:
    ctx = {"error": error, "wait": apppass.THROTTLE.wait_seconds(), "next": target}
    return TEMPLATES.TemplateResponse(request, "login.html", ctx, status_code=status_code)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next_url: str = Query(default="/", alias="next")) -> Response:
    target = safe_next(next_url)
    if (not apppass.is_enabled()
            or apppass.session_valid(request.cookies.get(apppass.COOKIE))):
        return RedirectResponse(target, status_code=303)
    return _login_page(request, target)


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form(default=""),
                 next_url: str = Form(default="/", alias="next")) -> Response:
    target = safe_next(next_url)
    if not apppass.is_enabled():
        return RedirectResponse(target, status_code=303)
    ok, error, wait = apppass.attempt(password)
    if not ok:
        return _login_page(request, target, error, 429 if wait else 401)
    response = RedirectResponse(target, status_code=303)
    _set_cookie(response, apppass.new_session())
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    apppass.end_session(request.cookies.get(apppass.COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(apppass.COOKIE, path="/")
    return response


@router.get("/settings/password", response_class=HTMLResponse)
def password_panel(request: Request) -> HTMLResponse:
    return _panel(request)


def _current_is_wrong(current: str) -> str | None:
    """Changing a password in use takes that password. Returns the error, if any."""
    if not apppass.is_enabled():
        return None
    return apppass.attempt(current)[1]


@router.post("/settings/password", response_class=HTMLResponse)
def password_set(request: Request, password: str = Form(default=""),
                 confirm: str = Form(default=""), current: str = Form(default="")) -> Response:
    error = _current_is_wrong(current)
    if error:
        return _panel(request, error=error)
    if not apppass.MIN_LENGTH <= len(password) <= apppass.MAX_LENGTH:
        return _panel(request, error=f"Use at least {apppass.MIN_LENGTH} characters.")
    if password != confirm:
        return _panel(request, error="The two passwords differ.")
    if not apppass.set_password(password):
        return _panel(request, error="Could not write the .env file.")
    response = _panel(request, notice="App password saved. Other sessions were signed out.")
    _set_cookie(response, apppass.new_session())  # this browser stays signed in
    return response


@router.post("/settings/password/remove", response_class=HTMLResponse)
def password_remove(request: Request, current: str = Form(default="")) -> Response:
    error = _current_is_wrong(current)
    if error:
        return _panel(request, error=error)
    if not apppass.set_password(None):
        return _panel(request, error="Could not write the .env file.")
    response = _panel(request, notice="App password removed.")
    response.delete_cookie(apppass.COOKIE, path="/")
    return response
