"""Case reporting — export an investigation to a self-contained HTML or PDF.

The HTML is always available (no dependencies). PDF rendering tries a chain of
backends, each imported lazily and guarded, so the platform degrades gracefully:

  1. WeasyPrint    — best CSS fidelity; needs native GTK libs (ships in Docker).
  2. xhtml2pdf     — pure Python, no system dependencies; works on Windows/macOS
                     local installs out of the box.

If every backend is unavailable the caller falls back to serving the HTML report
(which the browser can still print to PDF), so export never hard-fails.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from nexus.config import Settings, get_settings
from nexus.db import get_connection
from nexus.storage import case_items, case_notes, enrich_feed_rows, get_case

logger = logging.getLogger("nexus.reporting")

# A stable, human-meaningful column order for the machine-readable exports
# (CSV/JSON). Analysts open these in Excel, a notebook, or another tool, so the
# fields are the ones that survive across views: provenance, timing, the AI
# verdict, and the text. Anything missing on a given row is emitted as blank.
DATA_EXPORT_FIELDS = [
    "id", "source", "title", "url", "author", "language",
    "published_at", "fetched_at", "threat_level", "party", "confidence",
    "shared_count", "summary", "translation", "entities", "content",
]

# Case exports also carry the item's Internet Archive (Wayback Machine) capture,
# when one exists — independent proof the source page existed.
CASE_EXPORT_FIELDS = DATA_EXPORT_FIELDS + ["archive_url", "archived_at"]


def _export_value(row: dict, field: str):
    """One field of one row, flattened to a scalar safe for CSV/JSON."""
    val = row.get(field)
    if val is None:
        return ""
    if isinstance(val, (list, dict)):
        # e.g. an already-parsed entities structure -> compact JSON string.
        return json.dumps(val, ensure_ascii=False)
    return val


def feed_rows_to_csv(items: list[dict], fields: list[str] | None = None) -> str:
    """Render feed rows as CSV text (UTF-8) for spreadsheets / data tools.

    ``fields`` overrides the default column set (e.g. the Intel view appends a
    relevance-score column). Unknown fields simply render blank.
    """
    cols = fields or DATA_EXPORT_FIELDS
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for it in items:
        writer.writerow({f: _export_value(it, f) for f in cols})
    return buf.getvalue()


def feed_rows_to_json(items: list[dict], fields: list[str] | None = None) -> str:
    """Render feed rows as a JSON array of objects (the curated fields)."""
    cols = fields or DATA_EXPORT_FIELDS
    out = [{f: _export_value(it, f) for f in cols} for it in items]
    return json.dumps(out, ensure_ascii=False, indent=2)


def _gather(case_id: int) -> dict | None:
    """Collect everything a report needs in one read transaction."""
    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return None
        items = case_items(conn, case_id)
        # Enrich with the same fields the live feed shows (entities_typed,
        # top_requirement = how well it answers a standing question, display_url,
        # relative_time) so the report table can present them.
        items = enrich_feed_rows(conn, items)
        notes = case_notes(conn, case_id)
        # Attach evidence (with absolute paths so the PDF renderer can embed it).
        settings = get_settings()
        data_root = settings.data_path.resolve()
        for it in items:
            ev_rows = conn.execute(
                "SELECT screenshot, sha256, captured_at, ocr_text FROM evidence "
                "WHERE item_id = ? ORDER BY id DESC",
                (it["id"],),
            ).fetchall()
            evidence = []
            for ev in ev_rows:
                evidence.append(
                    {
                        "abs_path": (data_root / ev["screenshot"]).as_uri(),
                        "sha256": ev["sha256"],
                        "captured_at": ev["captured_at"],
                        "ocr_text": ev["ocr_text"],
                    }
                )
            it["evidence"] = evidence
    return {"case": case, "items": items, "notes": notes}


def render_report_html(templates, case_id: int) -> str | None:
    """Render the case report to an HTML string, or None if the case is absent.

    `templates` is the app's Jinja2Templates instance so the report reuses the
    same environment as the rest of the UI.
    """
    bundle = _gather(case_id)
    if bundle is None:
        return None
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    template = templates.get_template("report.html")
    return template.render(generated_at=generated_at, **bundle)


def render_feed_report_html(
    templates,
    *,
    items: list[dict],
    total_matched: int,
    filters_label: str,
) -> str:
    """Render a full-feed export (every collected item matching the current
    filters) to a self-contained HTML string.

    Unlike the case report this is not scoped to one investigation — it is the
    analyst's whole River (or a filtered slice of it), with each item's AI
    analysis (summary, threat, translation, entities) included so the export is
    a complete, offline-readable intelligence digest.
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    template = templates.get_template("feed_report.html")
    return template.render(
        items=items,
        total_matched=total_matched,
        shown=len(items),
        filters_label=filters_label,
        generated_at=generated_at,
    )


def _pdf_via_weasyprint(html: str) -> bytes | None:
    """Render with WeasyPrint (best CSS fidelity; needs native GTK libs)."""
    try:
        from weasyprint import HTML
    except Exception as exc:  # ImportError or missing native libs
        logger.info("WeasyPrint unavailable (%s); trying next PDF backend.", exc)
        return None
    try:
        return HTML(string=html).write_pdf()
    except Exception:
        logger.exception("WeasyPrint PDF rendering failed")
        return None


def _file_uri_to_path(uri: str) -> str:
    """Map a file:// URI (used for embedded evidence images) to a local path.

    xhtml2pdf cannot fetch file:// URIs itself, so its link_callback resolves
    them here. Non-file URIs are returned unchanged.
    """
    if uri.startswith("file:"):
        parsed = urlparse(uri)
        local = unquote(parsed.path)
        # On Windows urlparse leaves a leading slash before the drive letter.
        if local.startswith("/") and len(local) > 2 and local[2] == ":":
            local = local[1:]
        return str(Path(local))
    return uri


def _pdf_via_xhtml2pdf(html: str) -> bytes | None:
    """Render with xhtml2pdf (pure Python, no system deps; Windows/macOS local)."""
    try:
        from xhtml2pdf import pisa
    except Exception as exc:
        logger.info("xhtml2pdf unavailable (%s); no PDF backend left.", exc)
        return None
    try:
        import io

        buffer = io.BytesIO()
        result = pisa.CreatePDF(
            src=html, dest=buffer, link_callback=lambda uri, _rel: _file_uri_to_path(uri)
        )
        if result.err:
            logger.warning("xhtml2pdf reported %s error(s) rendering report.", result.err)
            return None
        return buffer.getvalue()
    except Exception:
        logger.exception("xhtml2pdf PDF rendering failed")
        return None


def render_report_pdf(html: str, settings: Settings | None = None) -> bytes | None:
    """Convert report HTML to PDF bytes, trying each backend in turn.

    Returns the first backend's output, or None if none are available (callers
    then fall back to serving the HTML report).
    """
    for backend in (_pdf_via_weasyprint, _pdf_via_xhtml2pdf):
        pdf = backend(html)
        if pdf:
            return pdf
    logger.warning("No PDF backend available; falling back to HTML report.")
    return None
