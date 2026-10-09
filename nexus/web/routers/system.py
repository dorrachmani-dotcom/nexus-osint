"""Health, favicon and JSON status endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from nexus import __version__
from nexus.config import get_settings
from nexus.web.common import TEMPLATES_DIR

router = APIRouter()


# When this server process started (UTC ISO). The desktop launcher compares it
# to the newest source-file time to detect a stale server and restart it.
_STARTED_AT = datetime.now(UTC).isoformat()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "started_at": _STARTED_AT}


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Serve the Nexus icon so the app window/taskbar shows it instead of a
    generic browser glyph. The .ico ships inside the (bundled) templates dir, so
    this resolves in both a dev run and the frozen installer."""
    from fastapi.responses import FileResponse

    ico = TEMPLATES_DIR / "favicon.ico"
    if ico.is_file():
        return FileResponse(str(ico), media_type="image/x-icon")
    return Response(status_code=404)


@router.get("/api/status")
def api_status() -> JSONResponse:
    settings = get_settings()
    return JSONResponse(
        {
            "app": "Nexus-OSINT",
            "version": __version__,
            "claude_enabled": settings.claude_enabled,
            "sources": settings.availability_report(),
        }
    )
