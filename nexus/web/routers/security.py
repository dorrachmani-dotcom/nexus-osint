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


# --- Outbound activity log: everything that left the machine ---------------
_DAYS = (1, 7, 30, 90, 0)


def _outbound_filters(category: str, days: int, status: str) -> tuple[str, int, str]:
    from nexus.security.outbound import CATEGORIES

    return (category if category in CATEGORIES else "",
            days if days in _DAYS else 30,
            status if status in ("ok", "failed") else "")


@router.get("/security/outbound", response_class=HTMLResponse)
def outbound_activity(request: Request, category: str = "", days: int = 30,
                      status: str = "") -> HTMLResponse:
    """What left this machine: AI calls, publishes, e-mail, archive, look-ups."""
    from nexus.db import get_connection
    from nexus.security.outbound import CATEGORIES, query, summary

    category, days, status = _outbound_filters(category, days, status)
    with get_connection() as conn:
        rows = query(conn, category=category, days=days, status=status, limit=500)
        totals = summary(conn, days=days)
    return TEMPLATES.TemplateResponse(request, "outbound.html", {
        "rows": rows, "totals": totals, "categories": CATEGORIES,
        "category": category, "days": days, "status": status, "day_options": _DAYS,
    })


@router.get("/security/outbound.csv")
def outbound_activity_csv(category: str = "", days: int = 30, status: str = "") -> Response:
    from datetime import datetime

    from nexus.db import get_connection
    from nexus.security.outbound import query, to_csv

    category, days, status = _outbound_filters(category, days, status)
    with get_connection() as conn:
        rows = query(conn, category=category, days=days, status=status, limit=200_000)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    return Response(
        content=to_csv(rows), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="nexus_outbound_{stamp}.csv"'},
    )
