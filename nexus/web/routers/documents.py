"""Uploaded documents: the "Add documents" page, the upload endpoint, and
per-case / knowledge-pool document lists.

Uploads are multipart POSTs. They pass the same same-origin CSRF guard as every
other state-changing request (``nexus/web/security.py`` checks Origin/Referer,
which browsers send on multipart posts too). Size limits are enforced before
and while reading, and the actual parsing runs in a worker thread.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.datastructures import UploadFile

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.ingest.extract import format_support
from nexus.ingest.safety import DEFAULT_LIMITS, MB, safe_display_name
from nexus.ingest.upload import cloud_ai_active, ingest_upload, remove_original, uploads_dir
from nexus.storage import get_case, list_cases
from nexus.storage.documents import (
    delete_document,
    document_counts,
    document_items,
    get_document,
    list_documents,
)
from nexus.web.common import TEMPLATES

router = APIRouter()

_MAX_FILES = 200


def _upload_context(case_id: int | None = None) -> dict:
    settings = get_settings()
    with get_connection() as conn:
        cases = list_cases(conn)
        docs = list_documents(conn, case_id=case_id) if case_id else list_documents(conn)
        counts = document_counts(conn)
    return {
        "cases": cases,
        "documents": docs,
        "counts": counts,
        "formats": format_support(),
        "cloud_ai": cloud_ai_active(settings),
        "active_provider": settings.active_provider(),
        "max_file_mb": DEFAULT_LIMITS.max_file_bytes // MB,
        "max_request_mb": DEFAULT_LIMITS.max_request_bytes // MB,
        "fixed_case_id": case_id,
    }


@router.get("/sources/documents", response_class=HTMLResponse)
def documents_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "documents.html", _upload_context())


@router.get("/documents/list", response_class=HTMLResponse)
def documents_list(request: Request, case_id: int | None = None) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "_document_list.html", _upload_context(case_id)
    )


def _parse_destination(raw: str) -> tuple[int | None, bool]:
    """Form value 'pool' or a case id -> (case_id, in_pool)."""
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw), False
    return None, True


@router.post("/documents/upload", response_class=HTMLResponse)
async def documents_upload(request: Request) -> HTMLResponse:
    """Ingest one or many uploaded files into a case or the knowledge pool."""
    limits = DEFAULT_LIMITS
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limits.max_request_bytes + MB:
        return HTMLResponse(
            f'<p class="text-sm text-rose-400">Upload refused: more than '
            f"{limits.max_request_bytes // MB} MB in one go. Split it into smaller batches.</p>",
            status_code=413,
        )
    form = await request.form(max_files=_MAX_FILES, max_fields=50)
    case_id, in_pool = _parse_destination(str(form.get("destination") or ""))
    if form.get("also_pool"):
        in_pool = True
    no_ai = not form.get("allow_ai")

    results: list[dict] = []
    if case_id is not None:
        with get_connection() as conn:
            if get_case(conn, case_id) is None:
                results.append({"ok": False, "filename": "-", "error": "that case no longer exists"})
                case_id = None
    total = 0
    uploads = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
    if not uploads and not results:
        results.append({"ok": False, "filename": "-", "error": "choose at least one file"})
    for upload in uploads if not results else []:
        name = safe_display_name(upload.filename)
        data = await upload.read(limits.max_file_bytes + 1)
        await upload.close()
        total += len(data)
        if total > limits.max_request_bytes:
            results.append({"ok": False, "filename": name,
                            "error": "skipped: this upload is over the total size limit"})
            continue
        if len(data) > limits.max_file_bytes:
            results.append({"ok": False, "filename": name,
                            "error": f"larger than the {limits.max_file_bytes // MB} MB limit"})
            continue
        res = await run_in_threadpool(
            ingest_upload, data, name, case_id=case_id, in_pool=in_pool, no_ai=no_ai,
        )
        results.append(res)

    response = TEMPLATES.TemplateResponse(
        request, "_document_results.html",
        {"results": results, "case_id": case_id, "in_pool": in_pool, "no_ai": no_ai},
    )
    response.headers["HX-Trigger"] = "documents-changed"
    return response


@router.get("/documents/{doc_id}/chunks", response_class=HTMLResponse)
def document_chunks(request: Request, doc_id: int) -> HTMLResponse:
    with get_connection() as conn:
        doc = get_document(conn, doc_id)
        rows = document_items(conn, doc_id) if doc else []
    if doc is None:
        return HTMLResponse('<p class="text-xs text-rose-400">Document not found.</p>',
                            status_code=404)
    return TEMPLATES.TemplateResponse(
        request, "_document_chunks.html", {"doc": doc, "chunks": rows}
    )


@router.get("/documents/{doc_id}/file")
def document_file(doc_id: int) -> Response:
    """Download the stored original. Always as an attachment with a generic
    type, so an uploaded HTML/SVG file can never run in the app's origin."""
    with get_connection() as conn:
        doc = get_document(conn, doc_id)
    if doc is None:
        return HTMLResponse("Not found", status_code=404)
    base = uploads_dir().resolve()
    path = (base.parent / doc["stored_path"]).resolve()
    if path.parent != base or not path.is_file():
        return HTMLResponse("The stored file is missing.", status_code=404)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=safe_display_name(doc["filename"]),
        content_disposition_type="attachment",
    )


@router.post("/documents/{doc_id}/delete", response_class=HTMLResponse)
def document_delete(
    request: Request, doc_id: int, case_id: int | None = Form(None)
) -> HTMLResponse:
    with get_connection() as conn:
        orphan = delete_document(conn, doc_id)
    remove_original(orphan)
    return TEMPLATES.TemplateResponse(
        request, "_document_list.html", _upload_context(case_id)
    )


@router.get("/cases/{case_id}/documents", response_class=HTMLResponse)
def case_documents(request: Request, case_id: int) -> HTMLResponse:
    """The case page's Documents tab: upload straight into this case."""
    with get_connection() as conn:
        if get_case(conn, case_id) is None:
            return HTMLResponse("Case not found", status_code=404)
    return TEMPLATES.TemplateResponse(
        request, "_case_documents.html", _upload_context(case_id)
    )
