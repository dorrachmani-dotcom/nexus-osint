"""Web hardening middleware: CSRF/DNS-rebinding guard + security headers.

Nexus binds to 127.0.0.1 only, so it is never directly reachable from the
network. The residual browser-side threats for a localhost app are:

  * **CSRF** — a malicious website the operator visits can make the browser
    silently POST to ``http://127.0.0.1:8000/...`` (form posts are "simple"
    requests, so there is no CORS preflight to stop them). That could trigger a
    scan, create/delete a case, run a tool, or rewrite settings.
  * **DNS rebinding** — a hostile page rebinds its own domain to 127.0.0.1 and
    then talks to us as same-origin, bypassing the bind-address protection.

Both are closed without any per-form token (which would be awkward with htmx) by
verifying, on every state-changing request, that the ``Host`` is a loopback name
and that any ``Origin``/``Referer`` is same-origin. A genuine browser always
sends at least one of those on a cross-site POST, so a forged request is
rejected with 403 while same-origin htmx posts pass untouched.

The header middleware then adds defense-in-depth response headers (clickjacking,
MIME-sniffing, referrer leakage, a CSP) to every response.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

logger = logging.getLogger("nexus.security")

# Loopback host names we answer to. A request whose Host header is anything else
# is treated as a DNS-rebinding attempt and refused.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

# Methods that change state and therefore need CSRF/origin verification. Safe,
# read-only methods are exempt (they can't mutate anything).
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Content-Security-Policy. Every script and stylesheet ships with the app
# (no CDN), so only our own origin may serve them. 'unsafe-inline' remains for
# a few inline <script>/onclick handlers and the self-contained pyvis graph
# (rendered into a sandboxed srcdoc iframe); 'unsafe-eval' remains because
# htmx compiles hx-on:: handlers with Function(). The high-value directives stay
# locked down: no plugins (object-src), no other site may frame us
# (frame-ancestors), forms only post back to us (form-action), and <base>
# can't be hijacked.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": _CSP,
}


def _host_only(value: str) -> str:
    """Strip any port from a Host/Origin host component, keeping IPv6 brackets."""
    value = value.strip().lower()
    if value.startswith("["):  # IPv6 literal like [::1]:8000
        return value.split("]")[0] + "]"
    return value.split(":")[0]


def _is_loopback_host(host: str) -> bool:
    return _host_only(host) in _ALLOWED_HOSTS


class SameOriginGuardMiddleware(BaseHTTPMiddleware):
    """Reject cross-origin state-changing requests and DNS-rebinding hosts."""

    async def dispatch(self, request: Request, call_next):
        # 1) DNS-rebinding guard: only answer to loopback Host names.
        host = request.headers.get("host", "")
        if host and not _is_loopback_host(host):
            logger.warning("Rejected request with non-loopback Host header: %r", host)
            return PlainTextResponse("Bad Host header", status_code=400)

        # 2) CSRF/origin guard on state-changing methods.
        if request.method in _UNSAFE_METHODS:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            source = origin or referer
            if source and not _is_loopback_host(urlsplit(source).hostname or ""):
                logger.warning(
                    "Rejected cross-origin %s %s (origin/referer=%r)",
                    request.method,
                    request.url.path,
                    source,
                )
                return PlainTextResponse("Cross-origin request refused", status_code=403)
            # If neither Origin nor Referer is present the request isn't a
            # browser cross-site post (browsers always send one on those); we
            # allow it so non-browser local clients still work.

        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach defense-in-depth security headers to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


def install_security(app) -> None:
    """Wire both middlewares onto the app (call once at startup).

    Order matters: middleware added last runs first. We add the origin guard
    last so it runs before the header middleware and can short-circuit a forged
    request without doing any work.
    """
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(SameOriginGuardMiddleware)
