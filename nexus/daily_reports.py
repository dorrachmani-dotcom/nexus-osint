"""Scheduled daily case reports, saved locally and browsable on the Reports page.

Per case the analyst can switch on "export a daily report". At the daily-brief
time the scheduler renders the case's items COLLECTED since the previous report
(re-using the existing feed-report template and PDF backends) and saves it to

    <DATA_DIR>/reports/<case-slug>/<YYYY-MM-DD>.pdf   (or .html when no PDF
                                                        backend is installed)

next to a small ``<YYYY-MM-DD>.json`` sidecar (case, item count, format, time).

Everything stays on this machine. Files are served back only through
``resolve_report_path``, which accepts nothing but a strict slug/filename shape
and re-checks that the resolved path is inside the reports folder, so a crafted
URL can never read (or delete) anything else on disk.

Per-case settings live in DB meta (non-secret):
  * ``daily_report:<case_id>``      JSON {"enabled": bool, "format": "pdf"|"html",
                                          "email": "none"|"link"|"attach"}
  * ``daily_report_last:<case_id>`` ISO UTC time of the last generated report
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("nexus.daily_reports")

REPORT_FORMATS = ("pdf", "html")
EMAIL_MODES = ("none", "link", "attach")
MAX_ITEMS_PER_REPORT = 500

_SLUG_RE = re.compile(r"^case-(\d{1,9})(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?$")
_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(pdf|html)$")
_MIME = {"pdf": "application/pdf", "html": "text/html; charset=utf-8"}

DEFAULT_CONFIG = {"enabled": False, "format": "pdf", "email": "link"}


# ------------------------------------------------------------------ paths


def reports_root(settings) -> Path:
    return Path(settings.data_dir) / "reports"


def slugify(text: str, max_len: int = 40) -> str:
    """ASCII, lowercase, hyphen-separated; empty if nothing usable remains."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].strip("-")


def case_slug(case: dict) -> str:
    """Folder name for a case: ``case-<id>-<name-slug>`` (the id keeps it unique)."""
    name = slugify(str(case.get("name") or ""))
    return f"case-{int(case['id'])}" + (f"-{name}" if name else "")


def valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.fullmatch(slug or ""))


def valid_filename(filename: str) -> bool:
    return bool(_FILE_RE.fullmatch(filename or ""))


def resolve_report_path(settings, slug: str, filename: str) -> Path | None:
    """The on-disk path of one report, or None if the request is not a valid,
    existing report inside the reports folder. Blocks traversal by (1) a strict
    whitelist shape for both parts and (2) a resolved-path containment check."""
    if not (valid_slug(slug) and valid_filename(filename)):
        return None
    try:
        root = reports_root(settings).resolve()
        path = (root / slug / filename).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    if path.parent.parent != root or not path.is_file():
        return None
    return path


def _case_slugs(settings, case_id: int) -> list[str]:
    """Existing report folders that belong to ``case_id`` (validated names)."""
    try:
        return sorted(
            p.name for p in reports_root(settings).iterdir()
            if p.is_dir() and (m := _SLUG_RE.fullmatch(p.name)) and int(m.group(1)) == int(case_id)
        )
    except OSError:
        return []


def case_folder_slug(settings, case: dict) -> str:
    """The folder a case's reports go in: its existing one (so renaming the case
    never splits its history), else a fresh ``case_slug``."""
    existing = _case_slugs(settings, int(case["id"]))
    return existing[0] if existing else case_slug(case)


def find_case_report(settings, case_id: int, filename: str) -> tuple[str, Path] | None:
    """(slug, path) of one of a case's saved reports, or None. Validated."""
    if not valid_filename(filename):
        return None
    for slug in _case_slugs(settings, case_id):
        path = resolve_report_path(settings, slug, filename)
        if path is not None:
            return slug, path
    return None


def media_type_for(filename: str) -> str:
    m = _FILE_RE.fullmatch(filename or "")
    return _MIME[m.group(2)] if m else "application/octet-stream"


# --------------------------------------------------------------- config


def get_config(conn, case_id: int) -> dict:
    from nexus.storage import get_meta

    cfg = dict(DEFAULT_CONFIG)
    raw = get_meta(conn, f"daily_report:{int(case_id)}")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                cfg["enabled"] = bool(data.get("enabled"))
                if data.get("format") in REPORT_FORMATS:
                    cfg["format"] = data["format"]
                if data.get("email") in EMAIL_MODES:
                    cfg["email"] = data["email"]
        except ValueError:
            pass
    return cfg


def set_config(conn, case_id: int, *, enabled: bool, fmt: str, email: str) -> dict:
    from nexus.storage import set_meta

    cfg = {
        "enabled": bool(enabled),
        "format": fmt if fmt in REPORT_FORMATS else "pdf",
        "email": email if email in EMAIL_MODES else "link",
    }
    set_meta(conn, f"daily_report:{int(case_id)}", json.dumps(cfg))
    return cfg


def enabled_case_ids(conn) -> list[int]:
    """Cases (that still exist) with the daily report switched on."""
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'daily_report:%'"
    ).fetchall()
    out: list[int] = []
    for key, value in rows:
        try:
            cid = int(str(key).split(":", 1)[1])
            if json.loads(value or "{}").get("enabled"):
                out.append(cid)
        except (ValueError, IndexError, AttributeError):
            continue
    if not out:
        return []
    placeholders = ",".join("?" * len(out))
    existing = conn.execute(
        f"SELECT id FROM cases WHERE id IN ({placeholders})", out  # noqa: S608 (only a ?-placeholder list is interpolated)
    ).fetchall()
    return sorted(int(r[0]) for r in existing)


# ------------------------------------------------------------ generation


def generate_case_report(
    settings, templates, conn, case_id: int, *, now: datetime | None = None,
    fmt: str | None = None,
) -> dict | None:
    """Render and save today's report for one case. Returns its info dict, or
    None if the case does not exist. Never raises on a renderer problem: a
    missing/broken PDF backend falls back to HTML."""
    from nexus.reporting import render_feed_report_html, render_report_pdf
    from nexus.storage import (
        case_items_collected_since,
        enrich_feed_rows,
        get_case,
        get_meta,
        set_meta,
    )

    case = get_case(conn, case_id)
    if case is None:
        return None
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    cfg = get_config(conn, case_id)
    want = fmt if fmt in REPORT_FORMATS else cfg["format"]

    since = get_meta(conn, f"daily_report_last:{int(case_id)}") or (
        now_utc - timedelta(hours=24)
    ).isoformat()
    total, items = case_items_collected_since(conn, case_id, since, limit=MAX_ITEMS_PER_REPORT)
    enrich_feed_rows(conn, items)

    since_label = _short_time(since)
    html = render_feed_report_html(
        templates, items=items, total_matched=total,
        filters_label=f"Case “{case['name']}” — daily report: items collected since {since_label}",
    )
    content: bytes
    actual = want
    if want == "pdf":
        pdf = None
        try:
            pdf = render_report_pdf(html)
        except Exception:
            logger.warning("Daily report: PDF rendering failed; saving HTML instead.")
        if pdf:
            content = pdf
        else:
            actual = "html"
            content = html.encode("utf-8")
    else:
        content = html.encode("utf-8")

    slug = case_folder_slug(settings, case)
    folder = reports_root(settings) / slug
    folder.mkdir(parents=True, exist_ok=True)
    day = now_utc.astimezone().strftime("%Y-%m-%d")  # local calendar day
    filename = f"{day}.{actual}"
    # One report per case per day: replace a same-day file of either format.
    for other in REPORT_FORMATS:
        stale = folder / f"{day}.{other}"
        if other != actual and stale.is_file():
            stale.unlink()
    (folder / filename).write_bytes(content)
    info = {
        "case_id": int(case_id),
        "case_name": case["name"],
        "slug": slug,
        "filename": filename,
        "date": day,
        "format": actual,
        "items": int(total),
        "since": since,
        "generated_at": now_utc.isoformat(),
        "size": len(content),
    }
    (folder / f"{day}.json").write_text(json.dumps(info), encoding="utf-8")
    set_meta(conn, f"daily_report_last:{int(case_id)}", now_utc.isoformat())
    logger.info("Daily report saved for case %s (%d item(s), %s).", case_id, total, actual)
    return info


def generate_due_reports(settings, templates, conn, now: datetime | None = None) -> list[dict]:
    """Generate today's report for every case that has it switched on."""
    out: list[dict] = []
    for cid in enabled_case_ids(conn):
        try:
            info = generate_case_report(settings, templates, conn, cid, now=now)
            if info:
                out.append(info)
        except Exception:
            logger.exception("Daily report for case %s failed", cid)
    return out


# --------------------------------------------------------------- listing


def _short_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso or "")[:16]


def list_reports(settings, case_id: int | None = None) -> list[dict]:
    """Every saved report, newest first (optionally for one case). Never raises."""
    root = reports_root(settings)
    out: list[dict] = []
    try:
        folders = [p for p in root.iterdir() if p.is_dir() and valid_slug(p.name)]
    except OSError:
        return []
    for folder in folders:
        m = _SLUG_RE.fullmatch(folder.name)
        cid = int(m.group(1)) if m else 0
        if case_id is not None and cid != int(case_id):
            continue
        try:
            files = [f for f in folder.iterdir() if f.is_file() and valid_filename(f.name)]
        except OSError:
            continue
        for f in files:
            match = _FILE_RE.fullmatch(f.name)
            if match is None:  # unreachable: files were filtered by valid_filename
                continue
            day, fmt = match.groups()
            meta: dict = {}
            try:
                meta = json.loads((folder / f"{day}.json").read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    meta = {}
            except (OSError, ValueError):
                meta = {}
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            out.append({
                "case_id": cid,
                "case_name": str(meta.get("case_name") or folder.name),
                "slug": folder.name,
                "filename": f.name,
                "date": day,
                "format": fmt,
                "items": meta.get("items"),
                "generated_at": _short_time(meta.get("generated_at", "")) if meta else "",
                "size": size,
                "size_label": human_size(size),
            })
    out.sort(key=lambda r: (r["date"], r["generated_at"]), reverse=True)
    return out


def group_by_case(reports: list[dict]) -> list[dict]:
    """[{case_id, case_name, slug, reports:[...]}], groups ordered by newest report."""
    groups: dict[str, dict] = {}
    for r in reports:
        g = groups.setdefault(r["slug"], {
            "case_id": r["case_id"], "case_name": r["case_name"],
            "slug": r["slug"], "reports": [],
        })
        g["reports"].append(r)
    return list(groups.values())


def delete_case_report(settings, case_id: int, filename: str) -> bool:
    """Delete one of a case's saved reports (and its sidecar when no same-day
    report remains). Only validated paths inside the reports folder."""
    found = find_case_report(settings, case_id, filename)
    if found is None:
        return False
    path = found[1]
    try:
        path.unlink()
        match = _FILE_RE.fullmatch(filename)  # validated by find_case_report
        day = match.group(1) if match else None
        if day and not any((path.parent / f"{day}.{f}").is_file() for f in REPORT_FORMATS):
            sidecar = path.parent / f"{day}.json"
            if sidecar.is_file():
                sidecar.unlink()
        return True
    except OSError:
        logger.warning("Could not delete report %s/%s", path.parent.name, filename)
        return False


def human_size(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
