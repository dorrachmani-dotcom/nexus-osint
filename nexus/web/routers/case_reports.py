"""Case exports and reports: printable report, Obsidian vault, evidence
manifest and scheduled case reports.
"""

from __future__ import annotations

import logging
from datetime import UTC

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.reporting import (
    feed_rows_to_csv,
    feed_rows_to_json,
    render_feed_report_html,
    render_report_html,
    render_report_pdf,
)
from nexus.storage import (
    case_evidence,
    case_items,
    get_case,
    get_meta,
    item_archives_map,
)
from nexus.web.common import TEMPLATES

logger = logging.getLogger("nexus")
router = APIRouter()


# --- Reporting --------------------------------------------------------------
@router.get("/cases/{case_id}/report")
def case_report(case_id: int, format: str = "pdf", scope: str = "pinned") -> Response:
    """Export a case as PDF (default), HTML, or machine-readable CSV/JSON.

    ``scope='pinned'`` (default) exports the curated dossier; ``scope='live'``
    exports the case's tracked items (everything matching its words — e.g. "only
    Neymar"). Falls back to HTML if no PDF backend is installed.
    """
    fmt = (format or "pdf").strip().lower()
    use_live = (scope or "pinned").strip().lower() == "live"

    # Machine-readable case exports: the case's items as data, for a spreadsheet
    # or another tool. Built from the same rows the HTML/PDF report uses.
    if fmt in ("csv", "json"):
        from nexus.storage import (
            case_items,
            case_live_items,
            enrich_feed_rows,
            get_case,
        )

        with get_connection() as conn:
            if get_case(conn, case_id) is None:
                return HTMLResponse("Case not found", status_code=404)
            base = case_live_items(conn, case_id, unread_only=False) if use_live else case_items(conn, case_id)
            rows = enrich_feed_rows(conn, base)
        from nexus.reporting import CASE_EXPORT_FIELDS

        suffix = "tracked" if use_live else "items"
        if fmt == "csv":
            return Response(
                content=feed_rows_to_csv(rows, CASE_EXPORT_FIELDS),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="case_{case_id}_{suffix}.csv"'},
            )
        return Response(
            content=feed_rows_to_json(rows, CASE_EXPORT_FIELDS),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="case_{case_id}_{suffix}.json"'},
        )

    # PDF / HTML report of the case's TRACKED (live) items — rendered like a feed
    # report so "Export tracked" offers the same formats as the pinned dossier.
    if use_live:
        from nexus.storage import case_live_items, enrich_feed_rows, get_case

        with get_connection() as conn:
            case = get_case(conn, case_id)
            if case is None:
                return HTMLResponse("Case not found", status_code=404)
            rows = enrich_feed_rows(conn, case_live_items(conn, case_id, unread_only=False))
        live_html = render_feed_report_html(
            TEMPLATES, items=rows, total_matched=len(rows),
            filters_label=f"Case “{case['name']}” — tracked items",
        )
        if fmt == "html":
            return HTMLResponse(live_html)
        live_pdf = render_report_pdf(live_html)
        if live_pdf is None:
            return HTMLResponse(live_html)
        return Response(
            content=live_pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="case_{case_id}_tracked.pdf"'},
        )

    html = render_report_html(TEMPLATES, case_id)
    if html is None:
        return HTMLResponse("Case not found", status_code=404)

    if fmt == "html":
        return HTMLResponse(html)

    pdf = render_report_pdf(html)
    if pdf is None:
        # Graceful fallback: serve the HTML report instead of erroring.
        return HTMLResponse(html)
    filename = f"case_{case_id}_report.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/cases/{case_id}/obsidian")
def case_obsidian(case_id: int) -> Response:
    """Download the case as an Obsidian Markdown vault (.zip) — one note per item
    with [[wikilinks]] to entities, so Obsidian's graph view shows the web."""
    from nexus.obsidian import build_case_vault

    with get_connection() as conn:
        result = build_case_vault(conn, case_id)
    if result is None:
        return HTMLResponse("Case not found", status_code=404)
    blob, summary = result
    safe = "".join(ch if ch.isalnum() else "_" for ch in summary["case"])[:40] or "case"
    logger.info("Obsidian export: case %s, %s item(s), %s entities",
                case_id, summary["items"], summary["entities"])
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}_obsidian.zip"'},
    )


@router.get("/cases/{case_id}/evidence-manifest")
def case_evidence_manifest(case_id: int) -> Response:
    """Download a plain-text chain-of-custody manifest for a case: every captured
    evidence screenshot with its SHA-256 hash and capture timestamp. Court-ready
    provenance the analyst can keep alongside the exported screenshots."""
    from datetime import datetime

    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        rows = case_evidence(conn, case_id)
        pinned = case_items(conn, case_id)
        archives = item_archives_map(conn, [int(p["id"]) for p in pinned])
    archived = [
        (p, archives[int(p["id"])]) for p in pinned
        if int(p["id"]) in archives
        and archives[int(p["id"])].get("status") in ("done", "existing")
        and archives[int(p["id"])].get("archive_url")
    ]
    archive_by_item = {int(p["id"]): a for p, a in archived}

    now = datetime.now(UTC)
    lines = [
        "NEXUS-OSINT — EVIDENCE MANIFEST",
        "=" * 72,
        f"Case        : {case['name']} (#{case_id})",
        f"Generated   : {now.isoformat(timespec='seconds')}",
        f"Evidence    : {len(rows)} captured screenshot(s)",
        "",
        "Each screenshot below was captured locally and hashed with SHA-256 at",
        "capture time, providing tamper-evident provenance: re-hashing the stored",
        "file and comparing it to the hash here proves the image is unaltered.",
        "=" * 72,
        "",
    ]
    for n, r in enumerate(rows, 1):
        lines += [
            f"[{n}] item #{r['item_id']} — {(r.get('title') or '(untitled)')}",
            f"    source      : {r.get('source') or '?'}",
            f"    url         : {r.get('url') or '(none)'}",
            f"    captured_at : {r.get('captured_at') or '?'}",
            f"    sha256      : {r.get('sha256') or '?'}",
            f"    file        : {r.get('screenshot') or '?'}",
        ]
        arch = archive_by_item.get(int(r["item_id"]))
        if arch:
            lines += [
                f"    archive_url : {arch['archive_url']}",
                f"    archived_at : {arch.get('archived_at') or '?'}",
            ]
        lines.append("")
    if not rows:
        lines.append("(No evidence captured for this case yet — capture some from a feed card.)")
    lines += [
        "",
        "=" * 72,
        "INTERNET ARCHIVE (WAYBACK MACHINE) CAPTURES",
        "=" * 72,
        "Independent, third-party public snapshots of pinned sources on",
        "web.archive.org. 'done' = captured from Nexus; 'existing' = a snapshot",
        "that already existed and was looked up.",
        "",
    ]
    for n, (p, a) in enumerate(archived, 1):
        lines += [
            f"[A{n}] item #{p['id']} — {(p.get('title') or '(untitled)')}",
            f"    url         : {p.get('url') or '(none)'}",
            f"    archive_url : {a['archive_url']}",
            f"    archived_at : {a.get('archived_at') or '?'}",
            f"    status      : {a.get('status')}",
            "",
        ]
    if not archived:
        lines.append("(No pinned item has an Internet Archive capture yet.)")
    text = "\n".join(lines)

    safe = "".join(ch if ch.isalnum() else "_" for ch in case["name"])[:40] or "case"
    stamp = now.strftime("%Y%m%d_%H%M")
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="evidence_manifest_{safe}_{stamp}.txt"'
        },
    )


# --- Daily case reports (case "Reports" tab) ---------------------------------
def _report_status(cfg: dict, digest_time: str, last_report: str | None,
                   last_run: str | None, brief_on: bool) -> dict:
    """Plain-English 'next / last / by email' lines for one case's daily report."""
    from datetime import UTC, datetime

    from nexus.setup_status import human_when, next_daily_run, parse_stored_time

    now = datetime.now(UTC)
    last_dt = parse_stored_time(last_report)
    nxt = next_daily_run(now, digest_time, last_run) if cfg.get("enabled") else None
    if not cfg.get("enabled"):
        email = "Switch the daily report on to get it by email."
    elif cfg.get("email") == "none":
        email = "Not mentioned in the daily email (saved here only)."
    elif brief_on:
        email = ("Attached to" if cfg.get("email") == "attach" else "Linked from") + " your daily email."
    else:
        email = "Saved here only. Turn on the daily brief (Setup) to get it by email too."
    return {
        "next": (human_when(nxt, now) if nxt and nxt > now.astimezone().replace(tzinfo=None)
                 else ("within a minute" if nxt else "not scheduled")),
        "last": human_when(last_dt, now) if last_dt else "never",
        "email": email,
        "brief_on": brief_on,
    }


def _render_case_reports(request: Request, case_id: int, **extra) -> HTMLResponse:
    from nexus import daily_reports as dr

    settings = get_settings()
    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        cfg = dr.get_config(conn, case_id)
        digest_time = get_meta(conn, "digest_time") or "08:00"
        last_report = get_meta(conn, f"daily_report_last:{int(case_id)}")
        last_run = get_meta(conn, "last_daily_reports_at")
        brief_on = get_meta(conn, "digest_enabled") == "1"
    ctx = {
        "case": case,
        "report_cfg": cfg,
        "report_status": _report_status(cfg, digest_time, last_report, last_run, brief_on),
        "reports": dr.list_reports(settings, case_id=case_id),
        "digest_time": digest_time,
        "pdf_note": "If no PDF engine is installed, the report is saved as HTML instead.",
        "report_msg": "",
        "report_ok": True,
    }
    ctx.update(extra)
    return TEMPLATES.TemplateResponse(request, "_case_reports.html", ctx)


@router.get("/cases/{case_id}/reports", response_class=HTMLResponse)
def case_reports_tab(request: Request, case_id: int) -> HTMLResponse:
    return _render_case_reports(request, case_id)


@router.post("/cases/{case_id}/reports/settings", response_class=HTMLResponse)
def case_reports_settings(
    request: Request, case_id: int,
    enabled: str = Form(""), format: str = Form("pdf"), email: str = Form("link"),
    view: str = Form(""),
) -> HTMLResponse:
    """Save one case's daily-report switch. ``view=row`` re-renders just its
    row on the Setup page; otherwise the case's Reports tab."""
    from nexus import daily_reports as dr
    from nexus.storage import case_terms

    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        cfg = dr.set_config(conn, case_id, enabled=enabled == "1", fmt=format, email=email)
        word_count = sum(1 for t in case_terms(conn, case_id) if t.get("kind") != "alert")
    msg = ("Saved — a report will be generated every day." if cfg["enabled"]
           else "Saved — the daily report is off.")
    if view == "row":
        row = {**case, "report": cfg, "word_count": word_count}
        return TEMPLATES.TemplateResponse(
            request, "_setup_report_row.html",
            {"c": row, "report_msg": "Saved" + (" — on" if cfg["enabled"] else " — off")},
        )
    return _render_case_reports(request, case_id, report_msg=msg, report_ok=True)


@router.post("/cases/{case_id}/reports/generate", response_class=HTMLResponse)
def case_reports_generate(request: Request, case_id: int) -> HTMLResponse:
    from nexus import daily_reports as dr

    settings = get_settings()
    try:
        with get_connection() as conn:
            wanted = dr.get_config(conn, case_id)["format"]
            info = dr.generate_case_report(settings, TEMPLATES, conn, case_id)
    except Exception:
        logger.exception("Generate report for case %s failed", case_id)
        return _render_case_reports(
            request, case_id, report_ok=False,
            report_msg="The report could not be generated. Nothing was changed.",
        )
    if info is None:
        return HTMLResponse("Case not found", status_code=404)
    note = " (saved as HTML: no PDF engine available)" if info["format"] != wanted else ""
    return _render_case_reports(
        request, case_id, report_ok=True,
        report_msg=f"Report for {info['date']} saved — {info['items']} new item(s){note}.",
    )


@router.get("/cases/{case_id}/reports/{filename}")
def case_report_file(case_id: int, filename: str, download: int = 0) -> Response:
    """Serve one saved daily report from the local reports folder only.

    Strictly validated (filename shape + resolved-path containment), so this
    can never read anything outside DATA_DIR/reports/<this case's folder>/.
    """
    from fastapi.responses import FileResponse

    from nexus import daily_reports as dr

    found = dr.find_case_report(get_settings(), case_id, filename)
    if found is None:
        return HTMLResponse("Report not found", status_code=404)
    slug, path = found
    disposition = "attachment" if download else "inline"
    return FileResponse(
        str(path),
        media_type=dr.media_type_for(filename),
        headers={"Content-Disposition": f'{disposition}; filename="{slug}-{filename}"'},
    )


@router.post("/cases/{case_id}/reports/{filename}/delete", response_class=HTMLResponse)
def case_report_delete(request: Request, case_id: int, filename: str) -> HTMLResponse:
    from nexus import daily_reports as dr

    ok = dr.delete_case_report(get_settings(), case_id, filename)
    return _render_case_reports(
        request, case_id, report_ok=ok,
        report_msg="Report deleted." if ok else "That report was not found.",
    )
