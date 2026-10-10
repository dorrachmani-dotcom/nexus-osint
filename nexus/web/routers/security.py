"""Security center: egress overview, file scan and audit report."""

from __future__ import annotations

import logging
from datetime import UTC

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from nexus.config import get_settings
from nexus.web.common import TEMPLATES

logger = logging.getLogger("nexus")
router = APIRouter()


# --- Security center: outbound-traffic monitor + file safety check -----------
@router.get("/security", response_class=HTMLResponse)
def security_center(request: Request) -> HTMLResponse:
    """Show what the app talks to, and offer a local file-cleanliness check."""
    from nexus.security import get_egress_report

    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request,
        "security.html",
        {
            "egress": get_egress_report(),
            "scan": None,
            "status": settings.availability_report(),
        },
    )


@router.post("/security/scan-file", response_class=HTMLResponse)
def security_scan_file(request: Request, upload: UploadFile = File(...)) -> HTMLResponse:
    """Scan one uploaded file locally and report a verdict. Nothing is stored."""
    from nexus.security import get_egress_report, scan_bytes

    settings = get_settings()
    scan = None
    try:
        blob = upload.file.read()
        scan = scan_bytes(blob, filename=getattr(upload, "filename", "") or "")
    except Exception:
        logger.exception("File scan failed")
        scan = {
            "ok": False, "verdict": "suspicious",
            "findings": [{"level": "warn", "message": "The scan could not be completed."}],
            "filename": getattr(upload, "filename", "") or "", "scanners": [],
        }
    return TEMPLATES.TemplateResponse(
        request,
        "security.html",
        {
            "egress": get_egress_report(),
            "scan": scan,
            "status": settings.availability_report(),
        },
    )


@router.get("/security/report")
def security_audit_report() -> Response:
    """Download a plain-text data-handling audit report (egress snapshot)."""
    from datetime import datetime

    from nexus.security import format_audit_report

    text = format_audit_report()
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    filename = f"nexus_data_handling_report_{stamp}.txt"
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
