"""Encrypted export/import of the local workspace."""

from __future__ import annotations

import logging
from datetime import UTC

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.storage import (
    count_items,
    get_meta,
    list_cases,
)
from nexus.web.common import TEMPLATES

logger = logging.getLogger("nexus")
router = APIRouter()


# --- Air-gap transfer (export / import a portable intelligence bundle) -------
@router.get("/transfer", response_class=HTMLResponse)
def transfer_page(request: Request) -> HTMLResponse:
    """The Transfer station: export a USB bundle here, import one over there."""
    settings = get_settings()
    with get_connection() as conn:
        cases = list_cases(conn)
        last_export = get_meta(conn, "transfer:last_export_at")
        total = count_items(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "transfer.html",
        {
            "cases": cases,
            "last_export": last_export,
            "total_items": total,
            "status": settings.availability_report(),
        },
    )


@router.post("/transfer/export")
def transfer_export(
    scope: str = Form(default="all"),
    case_id: str | None = Form(default=None),
    only_new: str | None = Form(default=None),
) -> Response:
    """Build a ``.nexusbundle`` and stream it as a download for the USB stick."""
    from datetime import datetime

    from nexus.transfer import export_bundle

    cid = int(case_id) if (scope == "case" and case_id and case_id.isdigit()) else None
    with get_connection() as conn:
        blob, summary = export_bundle(
            conn, scope=scope, case_id=cid, only_new=bool(only_new)
        )
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    filename = f"nexus_bundle_{stamp}.nexusbundle"
    logger.info(
        "Transfer export: %s item(s), %s evidence file(s), scope=%s",
        summary["item_count"], summary["evidence_count"], summary["scope"],
    )
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/transfer/import", response_class=HTMLResponse)
def transfer_import(
    request: Request, bundle: UploadFile = File(...)
) -> HTMLResponse:
    """Receive an uploaded bundle and merge it into the local database."""
    from nexus.transfer import import_bundle

    settings = get_settings()
    result = None
    error = None
    scan = None
    try:
        blob = bundle.file.read()
        # Safety gate: vet the uploaded file locally before we read it as a
        # bundle. A "dangerous" verdict (executable, zip-slip, zip-bomb, or an
        # AV/VirusTotal hit) blocks the import outright.
        from nexus.security import scan_bytes

        scan = scan_bytes(blob, filename=getattr(bundle, "filename", "") or "")
        if scan.get("verdict") == "dangerous":
            reasons = "; ".join(
                f["message"] for f in scan.get("findings", []) if f["level"] == "danger"
            )
            error = (
                "This file was blocked by the safety check and was NOT imported. "
                + (reasons or "It looks unsafe.")
            )
        else:
            with get_connection() as conn:
                result = import_bundle(conn, blob)
            logger.info("Transfer import: %s", result)
    except ValueError as exc:
        error = str(exc)
    except Exception:
        logger.exception("Transfer import failed")
        error = "Could not import that file. It may be damaged or not a Nexus bundle."

    with get_connection() as conn:
        cases = list_cases(conn)
        last_export = get_meta(conn, "transfer:last_export_at")
        total = count_items(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "transfer.html",
        {
            "cases": cases,
            "last_export": last_export,
            "total_items": total,
            "import_result": result,
            "import_error": error,
            "import_scan": scan,
            "status": settings.availability_report(),
        },
    )
