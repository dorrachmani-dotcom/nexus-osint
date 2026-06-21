"""FastAPI application entry point.

Serves the unified feed dashboard, a manual-scan trigger, and a JSON status
endpoint. The database is initialised on boot and a silent Boot Sync is kicked
off in the background (non-blocking) when any source is available.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nexus import __version__
from nexus.adapters.registry import all_adapters, get_adapter
from nexus.collector import Collector
from nexus.config import get_settings
from nexus.db import get_connection, init_db
from nexus.envstore import (
    EDITABLE_KEYS,
    custom_source_env_name,
    custom_source_key_configured,
    is_configured,
    reload_settings,
    update_env,
)
from nexus.evidence import capture_evidence, list_evidence
from nexus.graph import render_entity_graph_html, render_graph_html
from nexus.reporting import (
    feed_rows_to_csv,
    feed_rows_to_json,
    render_feed_report_html,
    render_report_html,
    render_report_pdf,
)
from nexus.storage import (
    add_bookmark,
    add_case_term,
    add_entity_alias,
    add_note,
    add_requirement,
    add_subscription,
    add_to_list,
    all_case_terms,
    attention_count,
    attention_items,
    case_alert_terms,
    case_timeline,
    delete_entity_alias,
    dismiss_item,
    get_entity_aliases,
    case_items,
    case_evidence,
    case_live_items,
    case_new_count,
    case_new_items,
    case_notes,
    case_question_items,
    case_reviewed_items,
    case_terms,
    case_terms_map,
    case_unread_count,
    count_items,
    count_matching_items,
    create_case,
    create_custom_source,
    create_list,
    create_watchlist,
    delete_capsule,
    delete_case,
    delete_custom_source,
    delete_list,
    update_list,
    delete_note,
    delete_requirement,
    delete_subscription,
    delete_watchlist,
    toggle_watchlist,
    distinct_sources,
    enrich_feed_rows,
    entity_profile,
    feed_item,
    get_active_case,
    get_case,
    get_custom_source,
    get_meta,
    get_list,
    intel_items,
    list_cases,
    list_custom_sources,
    list_lists,
    list_member_items,
    list_query_capsules,
    list_requirements,
    list_subscriptions,
    list_watchlist_hits,
    list_watchlists,
    count_new_watchlist_hits,
    mark_watchlist_hits_seen,
    mark_case_read,
    mark_read,
    mark_unread,
    related_cases,
    remove_bookmark,
    remove_case_term,
    remove_from_list,
    undismiss_item,
    update_case_term,
    search_items,
    set_active_case,
    set_meta,
    set_requirement_enabled,
    touch_case_visit,
    toggle_custom_source,
    topic_entity_graph,
    update_case,
    update_custom_source,
    update_note,
    decode_entities,
)

logger = logging.getLogger("nexus")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# OpSec: httpx/httpcore log every request URL at INFO — and some source URLs
# carry the API key as a query param (e.g. SERPAPI ?api_key=...). Raising their
# level to WARNING keeps secrets out of the log file. Our own "nexus.*" loggers
# stay at INFO.
for _noisy in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
# Defense-in-depth: scrub any configured secret value from every log record, so
# an accidental leak (a key in a URL, a payload dump) becomes a redacted ***.
from nexus.logging_safe import install_log_redaction  # noqa: E402

install_log_redaction()

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _safe_url(value) -> str:
    """Sanitise a link target rendered from untrusted (collected) content.

    Item URLs come from OSINT sources, so a crafted ``url`` could be
    ``javascript:...`` or ``data:...`` and execute when an analyst clicks it.
    Jinja autoescaping quotes the attribute but does NOT neutralise the scheme.
    This filter passes through only safe link schemes and returns ``#`` for
    anything else, so ``href="{{ it.url | safe_url }}"`` can never become an
    active-script link.
    """
    if not value:
        return "#"
    text = str(value).strip()
    lowered = text.lower()
    if lowered.startswith(("http://", "https://", "mailto:")):
        return text
    return "#"


TEMPLATES.env.filters["safe_url"] = _safe_url


def _linkify(value: str) -> "markupsafe.Markup":
    """Escape text, then turn bare http/https URLs into clickable links."""
    import re as _re
    from markupsafe import Markup, escape
    safe = str(escape(value))
    safe = _re.sub(
        r"(https?://[^\s&quot;&lt;&gt;\"']+)",
        r'<a href="\1" target="_blank" rel="noopener" '
        r'class="text-sky-400 hover:text-sky-300 break-all">\1</a>',
        safe,
    )
    return Markup(safe)


TEMPLATES.env.filters["linkify"] = _linkify


@asynccontextmanager
def _auto_scan_loop(collector: "Collector") -> None:
    """Daemon thread: fires collector.scan() on the DB-configured interval.

    Checks every 60 s whether an auto-scan is due, reads the interval live from
    the DB so the user can change it at runtime without a restart.
    """
    while True:
        time.sleep(60)
        try:
            with get_connection() as conn:
                interval_h = int(get_meta(conn, "auto_scan_interval") or 0)
                if interval_h <= 0:
                    continue
                last_raw = get_meta(conn, "last_auto_scan_at") or ""
                if last_raw:
                    try:
                        last_dt = datetime.fromisoformat(last_raw)
                        if datetime.now(timezone.utc) - last_dt < timedelta(hours=interval_h):
                            continue
                    except ValueError:
                        pass
                set_meta(conn, "last_auto_scan_at", datetime.now(timezone.utc).isoformat())
            logger.info("Auto-scan: starting scheduled scan (interval=%dh)", interval_h)
            collector.scan()
            logger.info("Auto-scan: completed")
        except Exception:
            logger.exception("Auto-scan: failed")


async def lifespan(app: FastAPI):
    settings = get_settings()
    # Re-run after uvicorn has installed its own log handlers, so the secret
    # redaction filter covers those too.
    install_log_redaction()
    init_db()
    app.state.collector = Collector(settings)
    report = settings.availability_report()
    logger.info("Nexus-OSINT v%s ready. Sources: %s", __version__, report)

    # Auto-scan background daemon — checks every 60 s if a scan is due.
    t = threading.Thread(
        target=_auto_scan_loop, args=(app.state.collector,), daemon=True, name="auto-scan"
    )
    t.start()

    # Boot Sync: silent gap-fill, off the event loop so startup stays fast.
    if app.state.collector.available_sources():
        asyncio.create_task(asyncio.to_thread(app.state.collector.boot_sync))
    yield


app = FastAPI(title="Nexus-OSINT", version=__version__, lifespan=lifespan)

# Web hardening: reject cross-origin/DNS-rebinding requests (CSRF defense) and
# add security headers (CSP, anti-clickjacking, no-sniff) to every response.
from nexus.web.security import install_security  # noqa: E402

install_security(app)

# Outbound-traffic monitor: observe every connection this process opens so the
# operator can confirm the tool only talks to its providers/sources and the
# local machine. Installed at import time for the widest coverage; fail-open.
from nexus.security import install_egress_monitor  # noqa: E402

install_egress_monitor()


@app.exception_handler(Exception)
async def _friendly_error_handler(request: Request, exc: Exception) -> HTMLResponse:
    """Last-resort guard: turn any unhandled error into a calm, branded page
    instead of a bare 500 / stack trace.

    The graceful-degradation principle says a single backend hiccup (a locked
    SQLite WAL, a full disk, an unexpected None) should never confront a
    non-technical analyst with a traceback. Routes still handle their own
    expected failures; this only catches the truly unexpected. HTTPExceptions
    (404s etc.) are handled by FastAPI's own handlers and never reach here.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    body = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Something went wrong</title>'
        '<style>body{background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;'
        'display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}'
        '.box{max-width:32rem;padding:2rem;text-align:center}a{color:#fbbf24}</style></head>'
        '<body><div class="box"><h1 style="font-size:1.1rem">Something went wrong</h1>'
        '<p style="color:#94a3b8;font-size:.9rem">An unexpected error interrupted this action. '
        'Your data is safe and the rest of the app keeps working. '
        'Try again, or <a href="/">return to the feed</a>.</p></div></body></html>'
    )
    return HTMLResponse(body, status_code=500)


# Serve stored evidence screenshots (and any other data assets) read-only.
_DATA_DIR = Path(get_settings().data_dir)
_DATA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/data", StaticFiles(directory=str(_DATA_DIR)), name="data")


# When this server process started (UTC ISO). The desktop launcher compares it
# to the newest source-file time to detect a stale server and restart it.
from datetime import datetime as _dt, timezone as _tz

_STARTED_AT = _dt.now(_tz.utc).isoformat()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "started_at": _STARTED_AT}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Serve the Nexus icon so the app window/taskbar shows it instead of a
    generic browser glyph. The .ico ships inside the (bundled) templates dir, so
    this resolves in both a dev run and the frozen installer."""
    from fastapi.responses import FileResponse

    ico = Path(__file__).parent / "templates" / "favicon.ico"
    if ico.is_file():
        return FileResponse(str(ico), media_type="image/x-icon")
    return Response(status_code=404)


@app.get("/api/status")
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


THREAT_LEVELS = ["none", "low", "medium", "high", "critical"]

# How many feed cards to render per page. "Load more" fetches the next slice
# by offset so an accumulating archive never renders thousands of rows at once.
FEED_PAGE_SIZE = 50

# Collection sources (used to tell a first-time user whether anything is wired up).
COLLECTION_SOURCES = ("rss", "serpapi", "telegram", "twitter", "reddit")


def _setup_state(settings, total_items: int, has_targets: bool = False) -> dict:
    """A friendly 'are you set up yet?' snapshot for the getting-started guide.

    Step 1 ("choose what to follow") is keyed to whether the analyst has defined
    at least one collection target — a topic bundle, an RSS feed, a subreddit or
    a search-term capsule. (It is *not* keyed to API-key availability: RSS is
    always on, which would otherwise make step 1 look done before the user has
    picked anything. This matters because the feed is topic-scoped by default,
    so a brand-new user with no targets sees almost nothing until they choose.)
    """
    report = settings.availability_report()
    sources_on = any(report.get(name) == "on" for name in COLLECTION_SOURCES)
    return {
        "has_targets": has_targets,
        "sources_on": sources_on,
        "ai_on": settings.analysis_enabled,
        "scanned": total_items > 0,
        "complete": has_targets and total_items > 0,
    }


def _filters(q, source, threat, since, until, window=None, unread=False,
             scope="topics", show_dismissed=False) -> dict:
    return {
        "q": q or "",
        "source": source or "",
        "threat": threat or "",
        "since": since or "",
        "until": until or "",
        "window": window or "",
        "unread": bool(unread),
        "scope": scope or "topics",
        "show_dismissed": bool(show_dismissed),
    }


def _topic_terms(conn) -> list[str]:
    """Flatten everything the analyst follows into one list of search terms.

    Sources the union of (a) every Case's tracking words (the unified model) and
    (b) any legacy capsule terms not yet migrated, so the home feed's "tracked
    topics" scope reflects what the analyst is actually following. Returns [] when
    nothing is tracked, so the caller falls back to showing everything.
    """
    terms: list[str] = list(all_case_terms(conn))
    for capsule in list_query_capsules(conn):
        terms.extend(t["value"] for t in capsule["terms"] if t.get("value"))
    # De-duplicate case-insensitively while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        key = (t or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return out


# Quick relative-time windows -> number of hours back from "now".
_WINDOW_HOURS = {"1h": 1, "6h": 6, "24h": 24, "7d": 24 * 7, "30d": 24 * 30}


def _window_since_ts(window: str | None) -> str | None:
    """Translate a quick-window token (e.g. '24h') into an ISO timestamp cutoff."""
    hours = _WINDOW_HOURS.get((window or "").strip())
    if not hours:
        return None
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return cutoff.isoformat()


def _paged_feed(
    conn,
    *,
    q=None, source=None, threat=None, since=None, until=None,
    since_ts=None, unread_only=False, show_dismissed=False, scope="topics", offset=0,
) -> dict:
    """Run the topic-scoped, paginated feed query and return render context.

    Centralises the topic-scoping rule (default feed shows only tracked topics;
    an explicit ``q`` or ``scope=all`` opens the firehose) and the pagination
    math so the home page, the htmx partial, scan, bulk actions and the
    load-more endpoint all behave identically.
    """
    terms = None
    if not q and scope == "topics":
        terms = _topic_terms(conn) or None
    items = search_items(
        conn, q=q, source=source, threat=threat, since=since, until=until,
        since_ts=since_ts, unread_only=unread_only, show_dismissed=show_dismissed,
        terms=terms, limit=FEED_PAGE_SIZE, offset=offset,
    )
    enrich_feed_rows(conn, items)
    matched = count_matching_items(
        conn, q=q, source=source, threat=threat, since=since, until=until,
        since_ts=since_ts, unread_only=unread_only, show_dismissed=show_dismissed, terms=terms,
    )
    next_offset = offset + len(items)
    return {
        "items": items,
        "matched": matched,
        "has_more": next_offset < matched,
        "next_offset": next_offset,
        "remaining": max(matched - next_offset, 0),
        "page_size": FEED_PAGE_SIZE,
        "topic_scoped": bool(terms),
        "_terms": terms,
    }


@app.get("/", response_class=HTMLResponse)
def feed(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    window: str | None = None,
    unread: str | None = None,
    scope: str | None = None,
    show_dismissed: str | None = None,
    welcome: str | None = None,
) -> HTMLResponse:
    """The unified feed (The River) with full-text + faceted filtering.

    By default the feed is scoped to the analyst's tracked topics so it shows
    only what they're following — not the entire raw collection. ``scope=all``
    opens the firehose; an explicit search (``q``) always overrides scoping.

    ``welcome=1`` re-opens the getting-started steps even after setup is
    complete, so a user who forgot a step can always return to the explanations.
    """
    settings = get_settings()
    scope = (scope or "topics").strip()
    since_ts = _window_since_ts(window)
    unread_only = bool(unread)
    dismissed = bool(show_dismissed)
    with get_connection() as conn:
        capsules = list_query_capsules(conn)
        has_targets = bool(list_subscriptions(conn))
        page = _paged_feed(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, show_dismissed=dismissed, scope=scope,
        )
        total = count_items(conn)
        sources = distinct_sources(conn)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open", parent_id=None)
        _terms = case_terms_map(conn, [c["id"] for c in all_cases], kind="required")
        for _c in all_cases:
            _w = _terms.get(_c["id"], [])
            _c["new_count"] = case_new_count(conn, _c["id"], terms=_w)
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "feed.html",
        {
            "items": page["items"],
            "count": total,
            "matched": page["matched"],
            "has_more": page["has_more"],
            "next_offset": page["next_offset"],
            "remaining": page["remaining"],
            "status": settings.availability_report(),
            "sources": sources,
            "threat_levels": THREAT_LEVELS,
            "filters": _filters(q, source, threat, since, until, window,
                                unread_only, scope, dismissed),
            "has_topics": bool(capsules),
            "topic_scoped": page["topic_scoped"],
            "setup": _setup_state(settings, total, has_targets=has_targets),
            "force_getting_started": bool(welcome),
            "bundles": list(TOPIC_PRESETS.keys()),
            "capsules": capsules,
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
        },
    )


@app.get("/feed", response_class=HTMLResponse)
def feed_partial(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    window: str | None = None,
    unread: str | None = None,
    scope: str | None = None,
    show_dismissed: str | None = None,
) -> HTMLResponse:
    """htmx partial: the filtered feed list only (for live search/filtering)."""
    scope = (scope or "topics").strip()
    since_ts = _window_since_ts(window)
    unread_only = bool(unread)
    dismissed = bool(show_dismissed)
    with get_connection() as conn:
        page = _paged_feed(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, show_dismissed=dismissed, scope=scope,
        )
        total = count_items(conn)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_feed.html",
        {
            "items": page["items"], "count": total,
            "matched": page["matched"], "has_more": page["has_more"],
            "next_offset": page["next_offset"], "remaining": page["remaining"],
            "all_lists": all_lists,
            "all_cases": all_cases, "active_case": active_case,
            "filters": _filters(q, source, threat, since, until, window,
                                unread_only, scope, dismissed),
            "topic_scoped": page["topic_scoped"],
        },
    )


@app.get("/feed/page", response_class=HTMLResponse)
def feed_page(
    request: Request,
    offset: int = 0,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    window: str | None = None,
    unread: str | None = None,
    scope: str | None = None,
    show_dismissed: str | None = None,
) -> HTMLResponse:
    """htmx partial: the *next* page of feed cards (for the Load more button).

    Returns only the item cards plus a fresh load-more control, which htmx
    appends in place — so the existing rows (and any bulk selection) are kept.
    """
    scope = (scope or "topics").strip()
    since_ts = _window_since_ts(window)
    unread_only = bool(unread)
    dismissed = bool(show_dismissed)
    offset = max(int(offset or 0), 0)
    with get_connection() as conn:
        page = _paged_feed(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, show_dismissed=dismissed,
            scope=scope, offset=offset,
        )
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_feed_page.html",
        {
            "items": page["items"],
            "has_more": page["has_more"], "next_offset": page["next_offset"],
            "remaining": page["remaining"],
            "all_lists": all_lists, "all_cases": all_cases,
            "active_case": active_case,
            "filters": _filters(q, source, threat, since, until, window,
                                unread_only, scope, dismissed),
        },
    )


@app.post("/scan", response_class=HTMLResponse)
def scan(request: Request, scope: str | None = Form(default=None)) -> HTMLResponse:
    """Manual scan trigger — runs collection, then returns the refreshed feed
    (scoped to the analyst's tracked topics by default, like the feed itself)."""
    collector: Collector = request.app.state.collector
    stats = collector.scan()
    scope = (scope or "topics").strip()
    with get_connection() as conn:
        page = _paged_feed(conn, scope=scope)
        total = count_items(conn)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_feed.html",
        {
            "items": page["items"], "count": total, "scan_stats": stats,
            "matched": page["matched"], "has_more": page["has_more"],
            "next_offset": page["next_offset"], "remaining": page["remaining"],
            "all_lists": all_lists, "all_cases": all_cases, "active_case": active_case,
            "filters": _filters(None, None, None, None, None, None, False, scope),
            "topic_scoped": page["topic_scoped"],
        },
    )


@app.post("/scan/start", response_class=HTMLResponse)
def scan_start(request: Request) -> HTMLResponse:
    """Kick off a scan in the background and return a self-polling status chip,
    so the page never blocks for the minutes a scan can take."""
    from nexus import scanstate

    scanstate.start(request.app.state.collector)
    return TEMPLATES.TemplateResponse(request, "_scan_status.html", {"state": scanstate.state()})


@app.get("/scan/status", response_class=HTMLResponse)
def scan_status(request: Request) -> HTMLResponse:
    """Current background-scan status (polled by the status chip)."""
    from nexus import scanstate

    return TEMPLATES.TemplateResponse(request, "_scan_status.html", {"state": scanstate.state()})


@app.post("/demo/load", response_class=HTMLResponse)
def demo_load(request: Request) -> HTMLResponse:
    """Load the one-click demo dataset (fictional sample), then show the populated
    feed so a fresh install demonstrates its features before sources are set up."""
    from nexus.demodata import load_demo_data

    with get_connection() as conn:
        load_demo_data(conn)
    return _feed_partial_response(request, scope="all")


def _capsule_terms(conn, name: str) -> list[str]:
    capsule = next(
        (c for c in list_query_capsules(conn) if c["name"] == name), None
    )
    return [t["value"] for t in capsule["terms"]] if capsule else []


@app.get("/investigation")
def investigation(name: str = "") -> Response:
    """Folded into the Case hub: redirect to the case of this name (its Live-feed
    tab), or to the cases overview if there's no match. Kept for old bookmarks."""
    from fastapi.responses import RedirectResponse

    target = (name or "").strip().lower()
    with get_connection() as conn:
        match = next(
            (c for c in list_cases(conn, parent_id=None)
             if c["name"].strip().lower() == target),
            None,
        )
    if match:
        return RedirectResponse(url=f"/cases/{match['id']}?tab=feed", status_code=302)
    return RedirectResponse(url="/cases", status_code=302)


@app.post("/investigation/scan", response_class=HTMLResponse)
def investigation_scan(request: Request, name: str = Form(...)) -> HTMLResponse:
    """Run a collection scan, then return this capsule's focused, filtered feed."""
    collector: Collector = request.app.state.collector
    stats = collector.scan()
    with get_connection() as conn:
        terms = _capsule_terms(conn, name)
        items = search_items(conn, terms=terms) if terms else []
        enrich_feed_rows(conn, items)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_feed.html",
        {
            "items": items, "count": len(items), "scan_stats": stats,
            "all_lists": all_lists, "all_cases": all_cases, "active_case": active_case,
        },
    )


# --- Plug & Play OSINT tools + entity graph ---------------------------------


def _adapter_cards(settings) -> list[dict]:
    return [
        {"name": a.name, "binary": a.binary, "available": a.is_available()}
        for a in all_adapters(settings)
    ]


@app.get("/tools", response_class=HTMLResponse)
def tools(request: Request) -> HTMLResponse:
    """Plug & Play console: run an installed OSINT CLI tool against a target."""
    from nexus.toolguide import TOOL_CATALOG

    settings = get_settings()
    cards = _adapter_cards(settings)
    installed = {c["name"]: c["available"] for c in cards}
    return TEMPLATES.TemplateResponse(
        request,
        "tools.html",
        {
            "adapters": cards,
            "tool_guide": TOOL_CATALOG,
            "installed": installed,
            "status": settings.availability_report(),
        },
    )


@app.post("/tools/run", response_class=HTMLResponse)
def tools_run(request: Request, tool: str = Form(...), target: str = Form(...)) -> HTMLResponse:
    """Run one adapter, then render its findings plus an entity graph."""
    settings = get_settings()
    adapter = get_adapter(tool, settings)
    if adapter is None:
        result = None
        graph_html = None
    else:
        result = adapter.run(target)
        graph_html = (
            render_graph_html(target, result.findings) if result.findings else None
        )
    return TEMPLATES.TemplateResponse(
        request,
        "_tool_results.html",
        {"result": result, "tool": tool, "target": target, "graph_html": graph_html},
    )


_EMPTY_GRAPH = {
    "nodes": [], "edges": [], "most_mentioned": [], "most_connected": [],
    "item_count": 0, "entity_count": 0,
}


def _entity_graph_context(
    q: str | None, source: str | None, threat: str | None, window: str | None,
    case_ids: list[int] | None = None,
) -> dict:
    """Build the topic entity-graph payload (graph HTML + ranked tables).

    Aggregates the AI-extracted entities of every stored item matching the same
    filters the feed uses, so the analyst sees *who is talked about the most*
    and *who is most connected* across exactly the slice they are looking at.
    When ``case_ids`` is given, the graph is scoped to the UNION of those cases'
    tracking words — pick one case, several (e.g. Messi + Neymar), or all — so the
    map shows only entities from items those cases track, not the whole river.
    """
    since_ts = _window_since_ts(window)
    cases: list[dict] = []
    with get_connection() as conn:
        all_cases = list_cases(conn, status="open", parent_id=None)
        if case_ids:
            terms: list[str] = []
            seen: set[str] = set()
            pinned: list[int] = []
            for cid in case_ids:
                c = get_case(conn, cid)
                if not c:
                    continue
                cases.append(c)
                for t in case_terms(conn, cid):
                    if t.get("kind") == "alert":
                        continue
                    key = (t["term"] or "").strip().lower()
                    if key and key not in seen:
                        seen.add(key)
                        terms.append(t["term"])
                # The case's pinned dossier items, so a case the analyst curated by
                # pinning (not by tracking words) still produces a graph.
                pinned.extend(
                    int(r["item_id"]) for r in conn.execute(
                        "SELECT item_id FROM bookmarks WHERE case_id = ?", (cid,)
                    ).fetchall()
                )
            # Graph from the union of tracked-word items + pinned items. Only truly
            # empty (no words, nothing pinned) -> an honest empty graph.
            data = (
                topic_entity_graph(
                    conn, terms=terms or None, since_ts=since_ts,
                    extra_item_ids=pinned or None,
                )
                if (terms or pinned) else dict(_EMPTY_GRAPH)
            )
        else:
            data = topic_entity_graph(
                conn, q=q, source=source, threat=threat, since_ts=since_ts
            )
        sources = distinct_sources(conn)
        aliases = get_entity_aliases(conn)
    graph_html = render_entity_graph_html(data)
    return {
        "data": data,
        "graph_html": graph_html,
        "sources": sources,
        "threat_levels": THREAT_LEVELS,
        "filters": _filters(q, source, threat, None, None, window, False),
        # ``case`` (single) kept for the per-case tab banner; ``cases`` is the full
        # selection; ``all_cases`` + ``selected_ids`` drive the multi-select.
        "case": cases[0] if len(cases) == 1 else None,
        "cases": cases,
        "all_cases": all_cases,
        "selected_ids": [c["id"] for c in cases],
        "aliases": aliases,
    }


def _case_id_list(case) -> list[int]:
    """Parse repeated ?case= values (str/list) into a list of int case ids."""
    if isinstance(case, str):
        case = [case]
    return [int(c) for c in (case or []) if str(c).strip().isdigit()]


@app.get("/graph", response_class=HTMLResponse)
def graph(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    window: str | None = None,
    case: list[str] = Query(default=[]),
) -> HTMLResponse:
    """Topic relationship graph — who is talked about the most, and who is most
    connected. Global by default; scope to one or more cases with repeated
    ``?case=ID`` (e.g. ``?case=3&case=7`` for two cases)."""
    ctx = _entity_graph_context(q, source, threat, window, case_ids=_case_id_list(case))
    return TEMPLATES.TemplateResponse(request, "graph.html", ctx)


@app.get("/graph/build", response_class=HTMLResponse)
def graph_build(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    window: str | None = None,
    case: list[str] = Query(default=[]),
) -> HTMLResponse:
    """htmx partial: just the graph + ranked tables, for live re-filtering."""
    ctx = _entity_graph_context(q, source, threat, window, case_ids=_case_id_list(case))
    return TEMPLATES.TemplateResponse(request, "_entity_graph.html", ctx)


@app.post("/graph/alias", response_class=HTMLResponse)
def graph_add_alias(
    request: Request,
    alias: str = Form(...),
    canonical: str = Form(...),
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    window: str | None = Form(default=None),
    case: list[str] = Form(default=[]),
) -> HTMLResponse:
    """Add an entity alias (e.g. 'Messi' → 'Lionel Messi') and redraw the graph."""
    with get_connection() as conn:
        add_entity_alias(conn, alias.strip(), canonical.strip())
    ctx = _entity_graph_context(q, source, threat, window, case_ids=_case_id_list(case))
    return TEMPLATES.TemplateResponse(request, "_entity_graph.html", ctx)


@app.post("/graph/alias/{alias_id}/delete", response_class=HTMLResponse)
def graph_delete_alias(
    request: Request,
    alias_id: int,
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    window: str | None = Form(default=None),
    case: list[str] = Form(default=[]),
) -> HTMLResponse:
    """Remove an entity alias and redraw the graph."""
    with get_connection() as conn:
        delete_entity_alias(conn, alias_id)
    ctx = _entity_graph_context(q, source, threat, window, case_ids=_case_id_list(case))
    return TEMPLATES.TemplateResponse(request, "_entity_graph.html", ctx)


# --- "Needs your eyes": the few items that most warrant attention ------------


@app.get("/attention", response_class=HTMLResponse)
def attention(request: Request) -> HTMLResponse:
    """A short, ranked list of the unread items that most warrant attention,
    each with a plain reason it surfaced. Cuts triage from hundreds to a few."""
    settings = get_settings()
    with get_connection() as conn:
        items = attention_items(conn)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "attention.html",
        {
            "items": items,
            "count": len(items),
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
            "status": settings.availability_report(),
        },
    )


@app.get("/attention/count")
def attention_count_route() -> JSONResponse:
    """Unseen-attention count for the nav badge. Never raises."""
    total = 0
    try:
        with get_connection() as conn:
            total = attention_count(conn)
    except Exception:
        logger.debug("attention_count failed", exc_info=True)
    return JSONResponse({"total": total})


# --- Entity dossier: everything about one entity in one place ---------------


@app.get("/entity", response_class=HTMLResponse)
def entity_page(request: Request, name: str = "") -> HTMLResponse:
    """One entity's dossier: stats, the items mentioning it, who it co-occurs
    with, and the cases it appears in. Reachable by clicking any entity name."""
    settings = get_settings()
    name = (name or "").strip()
    if not name:
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="/graph", status_code=302)
    with get_connection() as conn:
        profile = entity_profile(conn, name)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    # Contextual pivots: if this entity looks like an identifier (email / phone /
    # domain / url / handle), offer one-click runs of the matching PASSIVE OSINT
    # tools. Unavailable tools are shown with their install hint, never hidden.
    pivot_type, pivot_tools = None, []
    if profile and profile.get("item_count"):
        from nexus.toolguide import TOOL_CATALOG, pivot_tools_for

        pivot_type, keys = pivot_tools_for(profile["name"], profile.get("kind"))
        if keys:
            cat = {t["key"]: t for t in TOOL_CATALOG}
            for k in keys:
                adapter = get_adapter(k, settings)
                meta = cat.get(k, {})
                pivot_tools.append({
                    "key": k,
                    "title": meta.get("title", k),
                    "available": bool(adapter and adapter.is_available()),
                    "install": meta.get("install", ""),
                })
    return TEMPLATES.TemplateResponse(
        request,
        "entity.html",
        {
            "profile": profile,
            "name": name,
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
            "pivot_type": pivot_type,
            "pivot_tools": pivot_tools,
            "status": settings.availability_report(),
        },
    )


@app.get("/entity/items", response_class=HTMLResponse)
def entity_items(request: Request, name: str = "", offset: int = 0) -> HTMLResponse:
    """htmx 'load more' for the entity dossier's item list."""
    offset = max(int(offset or 0), 0)
    with get_connection() as conn:
        profile = entity_profile(conn, name, offset=offset)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_entity_items.html",
        {
            "profile": profile,
            "name": name,
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
        },
    )


# --- Analyst workspace: bookmarks, cases, notes -----------------------------


@app.post("/items/{item_id}/bookmark", response_class=HTMLResponse)
def bookmark_item(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    """Bookmark an item. Returns the refreshed card for feed outerHTML swaps;
    callers with hx-swap=none (drawer, intel feed) ignore the response."""
    with get_connection() as conn:
        add_bookmark(conn, item_id, case_id)
        return _render_card(request, conn, item_id, case_id)


@app.post("/items/{item_id}/unbookmark", response_class=HTMLResponse)
def unbookmark_item(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    """Remove a bookmark. Returns the refreshed card for feed outerHTML swaps;
    drawer ignores the response (hx-swap=none)."""
    with get_connection() as conn:
        remove_bookmark(conn, item_id, None)
        return _render_card(request, conn, item_id, case_id)


@app.post("/items/{item_id}/evidence", response_class=HTMLResponse)
def capture_item_evidence(item_id: int) -> HTMLResponse:
    """Take a timestamped, hashed screenshot of the item's source (Vault)."""
    with get_connection() as conn:
        row = conn.execute("SELECT url FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return HTMLResponse('<span class="text-rose-400">item not found</span>', status_code=404)
    result = capture_evidence(item_id, row["url"])
    if result.get("ok"):
        return HTMLResponse(
            f'<span class="text-sky-400" title="{result["sha256"]}">&#128247; captured</span>'
        )
    # Escape the error: it can embed the item's (untrusted) host, so it must not
    # be able to break out of the title="" attribute.
    from markupsafe import escape

    return HTMLResponse(
        f'<span class="text-rose-400" title="{escape(result.get("error", ""))}">capture failed</span>'
    )


def _render_card(
    request: Request, conn, item_id: int, case_id: int | None = None
) -> HTMLResponse:
    """Re-render one feed card (after a read/list mutation) for an htmx swap."""
    item = feed_item(conn, item_id)
    if item is None:
        return HTMLResponse('<span class="text-rose-400">item not found</span>', status_code=404)
    current_case = get_case(conn, case_id) if case_id else None
    return TEMPLATES.TemplateResponse(
        request,
        "_feed_item.html",
        {
            "it": item,
            "all_lists": list_lists(conn, case_id=case_id),
            "all_cases": list_cases(conn, status="open"),
            "active_case": get_active_case(conn),
            "current_case": current_case,
        },
    )


def _feed_partial_response(
    request: Request,
    *,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    window: str | None = None,
    unread_only: bool = False,
    scope: str = "topics",
    scan_stats=None,
) -> HTMLResponse:
    """Render the feed list (``_feed.html``) for the given filters.

    Shared by the bulk-triage endpoints so that after a bulk action the feed
    redraws in exactly the slice the analyst was looking at.
    """
    since_ts = _window_since_ts(window)
    with get_connection() as conn:
        page = _paged_feed(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, scope=scope,
        )
        total = count_items(conn)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_feed.html",
        {
            "items": page["items"], "count": total,
            "matched": page["matched"], "has_more": page["has_more"],
            "next_offset": page["next_offset"], "remaining": page["remaining"],
            "all_lists": all_lists,
            "all_cases": all_cases, "active_case": active_case,
            "filters": _filters(q, source, threat, since, until, window,
                                unread_only, scope),
            "topic_scoped": page["topic_scoped"],
            "scan_stats": scan_stats,
        },
    )


@app.post("/feed/read-all", response_class=HTMLResponse)
def feed_read_all(
    request: Request,
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    window: str | None = Form(default=None),
    unread: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> HTMLResponse:
    """Mark every item matching the current filter as read, then redraw the feed."""
    since_ts = _window_since_ts(window)
    _scope = (scope or "topics").strip()
    with get_connection() as conn:
        terms = (_topic_terms(conn) or None) if (not q and _scope == "topics") else None
        rows = search_items(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=bool(unread),
            terms=terms, limit=50000,
        )
        for r in rows:
            mark_read(conn, r["id"])
    return _feed_partial_response(
        request, q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread_only=bool(unread), scope=_scope,
    )


@app.post("/bulk/read", response_class=HTMLResponse)
def bulk_mark_read(
    request: Request,
    item_ids: list[int] = Form(default=[]),
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    window: str | None = Form(default=None),
    unread: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> HTMLResponse:
    """Mark every selected item read in one action, then redraw the feed."""
    with get_connection() as conn:
        for iid in item_ids:
            mark_read(conn, iid)
    return _feed_partial_response(
        request, q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread_only=bool(unread), scope=(scope or "topics"),
    )


@app.post("/bulk/dismiss", response_class=HTMLResponse)
def bulk_dismiss(
    request: Request,
    item_ids: list[int] = Form(default=[]),
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    window: str | None = Form(default=None),
    unread: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> HTMLResponse:
    """Dismiss every selected item, then redraw the feed."""
    with get_connection() as conn:
        for iid in item_ids:
            dismiss_item(conn, iid)
    return _feed_partial_response(
        request, q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread_only=bool(unread), scope=(scope or "topics"),
    )


@app.post("/bulk/lists/{list_id}/add", response_class=HTMLResponse)
def bulk_add_to_list(
    request: Request,
    list_id: int,
    item_ids: list[int] = Form(default=[]),
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    window: str | None = Form(default=None),
    unread: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> HTMLResponse:
    """Add every selected item to a triage list, then redraw the feed."""
    with get_connection() as conn:
        if get_list(conn, list_id) is not None:
            for iid in item_ids:
                add_to_list(conn, list_id, iid)
    return _feed_partial_response(
        request, q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread_only=bool(unread), scope=(scope or "topics"),
    )


@app.post("/bulk/cases/{case_id}/add", response_class=HTMLResponse)
def bulk_add_to_case(
    request: Request,
    case_id: int,
    item_ids: list[int] = Form(default=[]),
    q: str | None = Form(default=None),
    source: str | None = Form(default=None),
    threat: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    window: str | None = Form(default=None),
    unread: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> HTMLResponse:
    """Pin every selected item into an investigation case, then redraw the feed."""
    with get_connection() as conn:
        if get_case(conn, case_id) is not None:
            for iid in item_ids:
                add_bookmark(conn, iid, case_id)
    return _feed_partial_response(
        request, q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread_only=bool(unread), scope=(scope or "topics"),
    )


@app.post("/items/{item_id}/read", response_class=HTMLResponse)
def item_mark_read(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    """Mark an item read and return the refreshed (dimmed) card."""
    with get_connection() as conn:
        mark_read(conn, item_id)
        return _render_card(request, conn, item_id, case_id)


@app.post("/items/{item_id}/unread", response_class=HTMLResponse)
def item_mark_unread(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    with get_connection() as conn:
        mark_unread(conn, item_id)
        return _render_card(request, conn, item_id, case_id)


@app.post("/items/{item_id}/dismiss", response_class=HTMLResponse)
def item_dismiss_route(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    with get_connection() as conn:
        dismiss_item(conn, item_id)
        return _render_card(request, conn, item_id, case_id)


@app.post("/items/{item_id}/undismiss", response_class=HTMLResponse)
def item_undismiss_route(
    request: Request, item_id: int, case_id: int | None = Form(default=None)
) -> HTMLResponse:
    with get_connection() as conn:
        undismiss_item(conn, item_id)
        return _render_card(request, conn, item_id, case_id)


@app.get("/items/{item_id}/detail", response_class=HTMLResponse)
def item_detail(request: Request, item_id: int) -> HTMLResponse:
    """Return a rich detail fragment for the slide-in drawer."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT i.id, i.source, i.url, i.author, i.title, i.content, i.language,
                   i.published_at, i.fetched_at,
                   a.summary, a.translation, a.threat_level, a.confidence,
                   a.party, a.contradiction, a.entities,
                   COALESCE(i.dismissed, 0) AS dismissed,
                   CASE WHEN r.item_id IS NOT NULL THEN 1 ELSE 0 END AS is_read,
                   CASE WHEN bk.item_id IS NOT NULL THEN 1 ELSE 0 END AS is_bookmarked
            FROM items i
            LEFT JOIN analyses a ON a.item_id = i.id
            LEFT JOIN item_reads r ON r.item_id = i.id
            LEFT JOIN bookmarks bk ON bk.item_id = i.id
            WHERE i.id = ?
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            return HTMLResponse("<p class='text-rose-400 p-4'>Item not found.</p>", status_code=404)
        item = dict(row)
        item["entities_typed"] = decode_entities(item.get("entities"))
        # `reliability` is a heuristic derived from the source/URL (not a stored
        # column), so compute it the same way the feed cards do.
        from nexus.storage import _classify_reliability

        item["reliability"] = _classify_reliability(item.get("source"), item.get("url"))
        wl_hits = conn.execute(
            """SELECT w.label, w.kind FROM watchlist_hits h
               JOIN watchlists w ON w.id = h.watchlist_id WHERE h.item_id = ?""",
            (item_id,),
        ).fetchall()
        cases = conn.execute(
            """SELECT DISTINCT c.id, c.name, COALESCE(c.priority, 'medium') AS priority
               FROM bookmarks b
               JOIN cases c ON c.id = b.case_id
               WHERE b.item_id = ? AND b.case_id IS NOT NULL""",
            (item_id,),
        ).fetchall()
        lists_ = conn.execute(
            """SELECT l.id, l.name, l.color FROM list_memberships m
               JOIN lists l ON l.id = m.list_id WHERE m.item_id = ?""",
            (item_id,),
        ).fetchall()
        pinned_case_ids = {r["id"] for r in cases}
        open_cases = conn.execute(
            "SELECT id, name FROM cases WHERE status = 'open' ORDER BY priority DESC, name",
        ).fetchall()
    return TEMPLATES.TemplateResponse(
        request,
        "_item_detail.html",
        {
            "item": item,
            "wl_hits": [dict(r) for r in wl_hits],
            "cases": [dict(r) for r in cases],
            "lists": [dict(r) for r in lists_],
            "open_cases": [dict(r) for r in open_cases],
            "pinned_case_ids": pinned_case_ids,
        },
    )


@app.post("/items/{item_id}/verify", response_class=HTMLResponse)
def item_verify(request: Request, item_id: int) -> HTMLResponse:
    """Cross-check an item's claim against other collected items (AI-assisted).
    Returns a verdict + the corroborating / contradicting sources."""
    from nexus.verify import verify_item

    with get_connection() as conn:
        result = verify_item(conn, item_id, get_settings())
    return TEMPLATES.TemplateResponse(
        request, "_verify_result.html", {"v": result, "item_id": item_id}
    )


# --- Custom triage lists / lanes --------------------------------------------


@app.get("/lists", response_class=HTMLResponse)
def lists_page(request: Request) -> HTMLResponse:
    """Overview of all triage lists."""
    settings = get_settings()
    with get_connection() as conn:
        rows = list_lists(conn)
    return TEMPLATES.TemplateResponse(
        request, "lists.html", {"lists": rows, "status": settings.availability_report()}
    )


@app.post("/lists", response_class=HTMLResponse)
def lists_create(
    request: Request, name: str = Form(...), color: str = Form(default="slate")
) -> HTMLResponse:
    with get_connection() as conn:
        create_list(conn, name, color)
        rows = list_lists(conn)
    return TEMPLATES.TemplateResponse(request, "_list_overview.html", {"lists": rows})


@app.post("/lists/{list_id}/update", response_class=HTMLResponse)
def lists_update(request: Request, list_id: int, name: str = Form(...)) -> HTMLResponse:
    with get_connection() as conn:
        update_list(conn, list_id, name)
        rows = list_lists(conn)
    return TEMPLATES.TemplateResponse(request, "_list_overview.html", {"lists": rows})


@app.post("/lists/{list_id}/delete", response_class=HTMLResponse)
def lists_delete(request: Request, list_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_list(conn, list_id)
        rows = list_lists(conn)
    return TEMPLATES.TemplateResponse(request, "_list_overview.html", {"lists": rows})


@app.get("/lists/{list_id}", response_class=HTMLResponse)
def list_detail(request: Request, list_id: int) -> HTMLResponse:
    """View the items filed into one list (reuses the feed card)."""
    settings = get_settings()
    with get_connection() as conn:
        meta = get_list(conn, list_id)
        if meta is None:
            return HTMLResponse("List not found", status_code=404)
        items = list_member_items(conn, list_id)
        enrich_feed_rows(conn, items)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "list_detail.html",
        {
            "list": meta,
            "items": items,
            "count": len(items),
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
            "status": settings.availability_report(),
        },
    )


@app.post("/lists/{list_id}/items/{item_id}/add", response_class=HTMLResponse)
def list_add_item(
    request: Request, list_id: int, item_id: int,
    case_id: int | None = Form(default=None),
) -> HTMLResponse:
    with get_connection() as conn:
        add_to_list(conn, list_id, item_id)
        return _render_card(request, conn, item_id, case_id=case_id)


@app.post("/lists/{list_id}/items/{item_id}/remove", response_class=HTMLResponse)
def list_remove_item(
    request: Request, list_id: int, item_id: int,
    case_id: int | None = Form(default=None),
) -> HTMLResponse:
    with get_connection() as conn:
        remove_from_list(conn, list_id, item_id)
        return _render_card(request, conn, item_id, case_id=case_id)


@app.post("/lists/quick-add/{item_id}", response_class=HTMLResponse)
def list_quick_add(
    request: Request, item_id: int, name: str = Form(...),
    case_id: int | None = Form(default=None),
) -> HTMLResponse:
    """Create a new list from the card's inline form, scoped to a case if given."""
    with get_connection() as conn:
        new_id = create_list(conn, name, case_id=case_id)
        add_to_list(conn, new_id, item_id)
        return _render_card(request, conn, item_id, case_id=case_id)


@app.get("/cases", response_class=HTMLResponse)
def cases(request: Request) -> HTMLResponse:
    settings = get_settings()
    with get_connection() as conn:
        rows = list_cases(conn, parent_id=None)  # top-level only; sub-cases nest inside
        # "New since last visit" badge + the tracking words that drive each case.
        # Fetch required terms only (alert words are passive, don't inflate badge).
        terms_by_case = case_terms_map(conn, [c["id"] for c in rows], kind="required")
        for c in rows:
            words = terms_by_case.get(c["id"], [])
            c["terms_preview"] = words
            c["new_count"] = case_new_count(conn, c["id"], terms=words)
        active = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "cases.html",
        {
            "cases": rows,
            "active_case": active,
            "status": settings.availability_report(),
        },
    )


@app.get("/brief/count")
def brief_count() -> JSONResponse:
    """Total new tracked items across open cases since each was last opened.
    Polled by the nav to show a live 'unread' badge. Never raises."""
    total = 0
    try:
        with get_connection() as conn:
            for c in list_cases(conn, status="open", parent_id=None):
                total += case_new_count(conn, c["id"])
    except Exception:
        logger.debug("brief_count failed", exc_info=True)
    return JSONResponse({"total": total})


@app.post("/brief/mark-read", response_class=HTMLResponse)
def brief_mark_read(item_ids: list[int] = Form(default=[])) -> HTMLResponse:
    """Mark a set of items read; used by the Brief 'Mark read' per-block button."""
    with get_connection() as conn:
        for iid in item_ids:
            mark_read(conn, iid)
    return HTMLResponse(
        '<span class="text-emerald-400 text-xs mono">&#10003; Marked read</span>'
    )


@app.get("/brief", response_class=HTMLResponse)
def daily_brief(request: Request) -> HTMLResponse:
    """The analyst's morning read: what's new across your open cases since you
    last opened each one, top items first — the whole day's news in one screen."""
    settings = get_settings()
    with get_connection() as conn:
        cases = list_cases(conn, status="open", parent_id=None)
        blocks: list[dict] = []
        quiet_cases: list[dict] = []
        total_new = 0
        for c in cases:
            n = case_new_count(conn, c["id"])
            if n <= 0:
                quiet_cases.append(c)
                continue
            items = case_new_items(conn, c["id"], limit=6)
            enrich_feed_rows(conn, items)
            blocks.append({"case": c, "new_count": n, "new_items": items})
            total_new += n
        active = get_active_case(conn)
        # Readiness: surface the few setup gaps that matter, with a fix link.
        has_sources = bool(
            list_subscriptions(conn) or all_case_terms(conn) or settings.rss_feed_list
        )
        readiness: list[dict] = []
        if not settings.analysis_enabled:
            readiness.append({
                "label": "AI analysis is off",
                "hint": "Add an API key (Gemini / Anthropic / OpenAI) or run a local Ollama "
                        "model so items get summaries, scores and translations.",
                "link": "/settings", "cta": "Open Settings",
            })
        if not has_sources:
            readiness.append({
                "label": "No sources to collect from yet",
                "hint": "Add an RSS preset or feed, or give a case some tracking words.",
                "link": "/topics", "cta": "Add sources",
            })
        if not cases:
            readiness.append({
                "label": "No cases yet",
                "hint": "Open a case for a subject — the AI can set up its tracking words "
                        "and questions from a one-line brief.",
                "link": "/cases", "cta": "Create a case",
            })
    return TEMPLATES.TemplateResponse(
        request,
        "brief.html",
        {
            "blocks": blocks,
            "quiet_cases": quiet_cases,
            "total_new": total_new,
            "open_cases": len(cases),
            "readiness": readiness,
            "active_case": active,
            "analysis_enabled": settings.analysis_enabled,
            "status": settings.availability_report(),
        },
    )


@app.post("/cases", response_class=HTMLResponse)
def cases_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    priority: str = Form(default="medium"),
    brief: str = Form(default=""),
) -> HTMLResponse:
    """Create a case AND set up its tracking words automatically, so opening a
    case and getting a working live feed are one step. Words are always seeded
    from the name; with an AI model they're expanded with related terms + starter
    questions. The optional ``brief`` just adds detail. Degrades gracefully."""
    from nexus.casesetup import auto_setup_case

    with get_connection() as conn:
        cid = create_case(conn, name.strip(), (description or "").strip() or None, priority)
        auto_setup_case(
            conn, cid, name.strip(), (description or "").strip(),
            settings=get_settings(), brief=brief,
        )
        rows = list_cases(conn, parent_id=None)
        active = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request, "_case_list.html", {"cases": rows, "active_case": active}
    )


@app.post("/cases/active", response_class=HTMLResponse)
def cases_set_active(request: Request, case_id: int = Form(...)) -> HTMLResponse:
    """Set (or clear, with case_id=0) the analyst's current working case."""
    with get_connection() as conn:
        set_active_case(conn, case_id if case_id else None)
        rows = list_cases(conn, parent_id=None)
        active = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request, "_case_list.html", {"cases": rows, "active_case": active}
    )


@app.post("/cases/{case_id}/update", response_class=HTMLResponse)
def cases_update(
    request: Request,
    case_id: int,
    status: str = Form(default=None),
    priority: str = Form(default=None),
    name: str = Form(default=None),
    description: str = Form(default=None),
) -> HTMLResponse:
    """Patch case lifecycle/priority/metadata; returns the refreshed board."""
    with get_connection() as conn:
        update_case(
            conn, case_id, name=name, description=description,
            status=status, priority=priority,
        )
        rows = list_cases(conn, parent_id=None)
        active = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request, "_case_list.html", {"cases": rows, "active_case": active}
    )


@app.post("/cases/{case_id}/close")
def cases_close(case_id: int):
    """Archive (close) a case and return to its detail page."""
    from fastapi.responses import RedirectResponse
    with get_connection() as conn:
        update_case(conn, case_id, status="closed")
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@app.post("/cases/{case_id}/reopen")
def cases_reopen(case_id: int):
    """Reopen a closed case and return to its detail page."""
    from fastapi.responses import RedirectResponse
    with get_connection() as conn:
        update_case(conn, case_id, status="open")
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@app.post("/cases/{case_id}/delete", response_class=HTMLResponse)
def cases_delete(request: Request, case_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_case(conn, case_id)
        rows = list_cases(conn, parent_id=None)
        active = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request, "_case_list.html", {"cases": rows, "active_case": active}
    )


_CASE_TABS = {"feed", "pinned", "questions", "subcases", "timeline", "graph"}


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail(
    request: Request, case_id: int, q: str = "", tab: str = "pinned"
) -> HTMLResponse:
    """The unified Case hub: one subject's live feed, pinned dossier, questions
    and sub-cases, as tabs. Everything for a case lives here."""
    settings = get_settings()
    tab = tab if tab in _CASE_TABS else "pinned"
    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        # Pinned dossier (existing behaviour).
        items = case_items(conn, case_id, query=q or None)
        enrich_feed_rows(conn, items)
        notes = case_notes(conn, case_id)
        connections = related_cases(conn, case_id)
        # Sub-cases (only meaningful for a top-level case).
        is_subcase = case.get("parent_id") is not None
        subcases = [] if is_subcase else list_cases(conn, parent_id=case_id)
        parent = get_case(conn, case["parent_id"]) if is_subcase else None
        # Live feed: items matching the case's required tracking words.
        all_terms = case_terms(conn, case_id)
        terms = [t for t in all_terms if t.get("kind") != "alert"]
        alert_terms_list = [t for t in all_terms if t.get("kind") == "alert"]
        # Inherited terms for sub-cases (parent's required terms, shown read-only).
        inherited_terms: list[str] = []
        if is_subcase and parent:
            inherited_terms = [
                r["term"]
                for r in conn.execute(
                    "SELECT term FROM case_terms WHERE case_id = ? AND kind = 'required' ORDER BY id",
                    (case["parent_id"],),
                ).fetchall()
            ]
        live_items = case_live_items(conn, case_id)  # unread_only=True by default
        enrich_feed_rows(conn, live_items)
        unread_count = case_unread_count(conn, case_id)
        # Questions: the case's own PIRs + the items that answer them.
        questions = list_requirements(conn, case_id=case_id)
        question_items = case_question_items(conn, case_id)
        enrich_feed_rows(conn, question_items)
        # Card menus need these; fetch inside the connection block.
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
        # Opening the case counts as "seen" — reset its new-since-visit badge.
        touch_case_visit(conn, case_id)
    return TEMPLATES.TemplateResponse(
        request,
        "case_detail.html",
        {
            "case": case,
            "tab": tab,
            "items": items,
            "notes": notes,
            "connections": connections,
            "q": q,
            "terms": terms,
            "alert_terms": alert_terms_list,
            "inherited_terms": inherited_terms,
            "live_items": live_items,
            "unread_count": unread_count,
            "feed_source": "",
            "feed_since": "",
            "feed_show": "unread",
            "feed_q": "",
            "feed_sort": "newest",
            "questions": questions,
            "question_items": question_items,
            "subcases": subcases,
            "is_subcase": is_subcase,
            "parent": parent,
            "analysis_enabled": settings.analysis_enabled,
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
            "status": settings.availability_report(),
        },
    )


def _render_case_feed_tab(
    request: Request,
    conn,
    case_id: int,
    source: str | None = None,
    since: str | None = None,
    since_preset: str = "",
    show: str = "unread",  # unread | all | reviewed
    q: str | None = None,
    sort: str = "newest",
) -> HTMLResponse:
    """Re-render the Live-feed tab (tracking words + matching items)."""
    settings = get_settings()
    case_row = get_case(conn, case_id)
    all_terms = case_terms(conn, case_id)
    required_terms = [t for t in all_terms if t.get("kind") != "alert"]
    alert_terms_list = [t for t in all_terms if t.get("kind") == "alert"]
    # Inherited terms: parent's required terms shown read-only on sub-cases.
    parent = None
    inherited_terms: list[str] = []
    if case_row and case_row.get("parent_id"):
        parent = get_case(conn, case_row["parent_id"])
        if parent:
            inherited_terms = [
                r["term"]
                for r in conn.execute(
                    "SELECT term FROM case_terms WHERE case_id = ? AND kind = 'required' ORDER BY id",
                    (case_row["parent_id"],),
                ).fetchall()
            ]
    # Fetch items based on show mode.
    if show == "reviewed":
        live_items = case_reviewed_items(
            conn, case_id, source=source, since=since, q=q, sort=sort
        )
    else:
        live_items = case_live_items(
            conn, case_id,
            unread_only=(show != "all"),
            source=source, since=since, q=q, sort=sort,
        )
    enrich_feed_rows(conn, live_items)
    unread_count = case_unread_count(conn, case_id)
    return TEMPLATES.TemplateResponse(
        request,
        "_case_feed_tab.html",
        {
            "case": case_row,
            "terms": required_terms,
            "alert_terms": alert_terms_list,
            "inherited_terms": inherited_terms,
            "parent": parent,
            "live_items": live_items,
            "unread_count": unread_count,
            "feed_source": source or "",
            "feed_since": since_preset,
            "feed_show": show,
            "feed_q": q or "",
            "feed_sort": sort,
            "analysis_enabled": settings.analysis_enabled,
            "all_lists": list_lists(conn),
            "all_cases": list_cases(conn, status="open"),
            "active_case": get_active_case(conn),
        },
    )


def _render_case_questions(request: Request, conn, case_id: int) -> HTMLResponse:
    questions = list_requirements(conn, case_id=case_id)
    question_items = case_question_items(conn, case_id)
    enrich_feed_rows(conn, question_items)
    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request,
        "_case_questions.html",
        {
            "case": get_case(conn, case_id),
            "questions": questions,
            "question_items": question_items,
            "analysis_enabled": settings.analysis_enabled,
            "all_lists": list_lists(conn),
            "all_cases": list_cases(conn, status="open"),
            "active_case": get_active_case(conn),
        },
    )


def _render_case_subcases(request: Request, conn, case_id: int) -> HTMLResponse:
    subcases = list_cases(conn, parent_id=case_id)
    for sc in subcases:
        all_terms = case_terms(conn, sc["id"])
        sc["required_terms"] = [t for t in all_terms if t.get("kind") != "alert"]
        sc["alert_terms"] = [t for t in all_terms if t.get("kind") == "alert"]
        req_words = [t["term"] for t in sc["required_terms"]]
        sc["new_count"] = case_new_count(conn, sc["id"], terms=req_words)
    return TEMPLATES.TemplateResponse(
        request,
        "_case_subcases.html",
        {"case": get_case(conn, case_id), "subcases": subcases},
    )


@app.post("/cases/{case_id}/activate")
def case_activate(case_id: int) -> Response:
    """Make this the active working case (so 'this'/'here' in Sherlock resolve to
    it and pins default here), then return to the case page."""
    from fastapi.responses import RedirectResponse

    with get_connection() as conn:
        set_active_case(conn, case_id if get_case(conn, case_id) else None)
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@app.post("/cases/{case_id}/deactivate")
def case_deactivate(case_id: int) -> Response:
    """Clear the active working case, then return to the case page."""
    from fastapi.responses import RedirectResponse

    with get_connection() as conn:
        set_active_case(conn, None)
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


def _generate_case_briefing(case: dict, items: list[dict], settings) -> str:
    """Ask the AI to synthesise a case's recent items into a short briefing.
    Returns plain text, or '' on any failure (caller shows a graceful message)."""
    if not items:
        return ""
    lines = []
    for i, it in enumerate(items[:25], 1):
        bit = it.get("title") or "(untitled)"
        if it.get("summary"):
            bit += f" — {it['summary'][:200]}"
        lines.append(f"Item {i}. [{it.get('source', '?')}] {bit}")
    corpus = "\n".join(lines)
    sys = (
        "You are an OSINT intelligence analyst. From the collected items below "
        "(numbered Item 1 … Item 25), write a concise briefing for a busy reader, "
        "in plain text with short bullet points under these headings: "
        "Key developments; Notable people/organisations; "
        "Contradictions or uncertainty (say 'none clear' if so); "
        "Suggested next step. When citing a source item write (Item N). "
        "Use ONLY the items; don't invent facts."
    )
    usr = f"Subject / case: {case.get('name')}\n\nCollected items:\n{corpus}"
    try:
        from nexus.analysis.providers import get_provider

        provider = get_provider(settings)
        if provider is None:
            return ""
        chat = getattr(provider, "chat", None) or provider.complete
        return (chat(sys, usr, max_tokens=900) or "").strip()
    except Exception:
        logger.exception("Case AI briefing failed")
        return ""


@app.post("/cases/{case_id}/brief", response_class=HTMLResponse)
def case_ai_brief(request: Request, case_id: int) -> HTMLResponse:
    """Generate an AI synthesis of the case's tracked items (read-only)."""
    settings = get_settings()
    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        items = case_live_items(conn, case_id, limit=25, unread_only=False)
        enrich_feed_rows(conn, items)
    error = None
    briefing = ""
    items_data: list[dict] = []
    if not settings.analysis_enabled:
        error = "An AI model is needed for briefings — add one in Settings."
    elif not items:
        error = "No tracked items yet — add tracking words and scan first."
    else:
        briefing = _generate_case_briefing(case, items, settings)
        if not briefing:
            error = "The AI couldn't produce a briefing just now. Please try again."
        else:
            import json as _json
            items_data = [
                {
                    "id": it.get("id"),
                    "title": (it.get("title") or "")[:120],
                    "url": it.get("url") or "",
                }
                for it in items[:25]
            ]
    return TEMPLATES.TemplateResponse(
        request, "_case_briefing.html",
        {
            "briefing": briefing, "error": error, "case": case,
            "count": len(items), "items_json": __import__("json").dumps(items_data),
        },
    )


@app.post("/cases/{case_id}/scan", response_class=HTMLResponse)
def case_scan(request: Request, case_id: int) -> HTMLResponse:
    """Collect fresh items now, then redraw this case's live feed."""
    collector: Collector = request.app.state.collector
    try:
        collector.scan()
    except Exception:
        logger.exception("Case scan failed")
    with get_connection() as conn:
        return _render_case_feed_tab(request, conn, case_id)


def _resolve_since(preset: str) -> str | None:
    """Convert a date preset ('today', 'week', 'month') to a YYYY-MM-DD cutoff."""
    from datetime import date, timedelta
    preset = (preset or "").strip().lower()
    today = date.today()
    if preset == "today":
        return today.isoformat()
    if preset == "week":
        return (today - timedelta(days=7)).isoformat()
    if preset == "month":
        return (today - timedelta(days=30)).isoformat()
    if len(preset) == 10 and preset[4] == "-":
        return preset  # already a YYYY-MM-DD literal
    return None


@app.get("/cases/{case_id}/feed", response_class=HTMLResponse)
def case_feed_filtered(
    request: Request,
    case_id: int,
    source: str = "",
    since: str = "",
    show: str = "unread",
    q: str = "",
    sort: str = "newest",
) -> HTMLResponse:
    """Re-render the feed tab with optional filters."""
    show = show if show in ("unread", "all", "reviewed") else "unread"
    sort = sort if sort in ("newest", "oldest") else "newest"
    with get_connection() as conn:
        return _render_case_feed_tab(
            request, conn, case_id,
            source=source or None,
            since=_resolve_since(since),
            since_preset=since or "",
            show=show,
            q=q or None,
            sort=sort,
        )


@app.get("/cases/{case_id}/reviewed", response_class=HTMLResponse)
def case_reviewed_tab(request: Request, case_id: int) -> HTMLResponse:
    """Lazy-load the reviewed/read items for this case."""
    with get_connection() as conn:
        case_row = get_case(conn, case_id)
        if case_row is None:
            return HTMLResponse("", status_code=404)
        items = case_reviewed_items(conn, case_id, limit=100)
        enrich_feed_rows(conn, items)
        all_lists = list_lists(conn)
        all_cases = list_cases(conn, status="open")
        active_case = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_case_reviewed.html",
        {
            "case": case_row,
            "items": items,
            "all_lists": all_lists,
            "all_cases": all_cases,
            "active_case": active_case,
        },
    )


@app.post("/cases/{case_id}/read-all", response_class=HTMLResponse)
def case_read_all(request: Request, case_id: int) -> HTMLResponse:
    """Mark all of this case's tracked items as read, then redraw the feed tab."""
    with get_connection() as conn:
        mark_case_read(conn, case_id)
        return _render_case_feed_tab(request, conn, case_id)


@app.post("/cases/{case_id}/terms", response_class=HTMLResponse)
def case_add_terms(
    request: Request,
    case_id: int,
    terms: str = Form(...),
    kind: str = Form(default="required"),
) -> HTMLResponse:
    """Add one or more tracking words/phrases (comma- or newline-separated).

    ``kind`` is 'required' (active search, default) or 'alert' (passive flag).
    """
    kind = kind if kind in ("required", "alert") else "required"
    with get_connection() as conn:
        for term in _split_terms(terms):
            add_case_term(conn, case_id, term, kind=kind)
        return _render_case_feed_tab(request, conn, case_id)


@app.post("/cases/{case_id}/terms/{term_id}/delete", response_class=HTMLResponse)
def case_delete_term(request: Request, case_id: int, term_id: int) -> HTMLResponse:
    with get_connection() as conn:
        remove_case_term(conn, case_id, term_id)
        return _render_case_feed_tab(request, conn, case_id)


@app.post("/cases/{case_id}/terms/{term_id}/edit", response_class=HTMLResponse)
def case_edit_term(
    request: Request, case_id: int, term_id: int, term: str = Form(...)
) -> HTMLResponse:
    """Edit a tracking word in place."""
    with get_connection() as conn:
        update_case_term(conn, case_id, term_id, term)
        return _render_case_feed_tab(request, conn, case_id)


@app.post("/cases/{case_id}/terms/translate", response_class=HTMLResponse)
def case_translate_terms(
    request: Request, case_id: int, language: str = Form(...)
) -> HTMLResponse:
    """Add the case's tracking words in another language so the search covers it.

    Uses the AI to translate/transliterate the existing words (or the case name)
    into the requested language and adds them as new tracking words. Degrades
    gracefully: with no AI model, nothing is added and the tab simply redraws."""
    settings = get_settings()
    lang = (language or "").strip()[:40]
    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        seed = [t["term"] for t in case_terms(conn, case_id) if t.get("kind") != "alert"] or [case["name"]]
        if lang and settings.analysis_enabled:
            try:
                import json as _json

                from nexus.analysis.providers import get_provider
                from nexus.assistant import _extract_json

                provider = get_provider(settings)
                if provider is not None:
                    sys = (
                        "You translate OSINT search keywords for a researcher. Reply ONLY "
                        'with a JSON object {"terms": ["...", "..."]} giving the supplied '
                        "terms translated or transliterated into the target language, suitable "
                        "as search queries (keep well-known proper names natural in that "
                        "language). No duplicates, no commentary."
                    )
                    usr = (
                        f"Target language: {lang}\n"
                        f"Terms: {_json.dumps(seed, ensure_ascii=False)}"
                    )
                    parsed = _extract_json(provider.complete(sys, usr, max_tokens=512))
                    for t in (parsed or {}).get("terms", []) or []:
                        add_case_term(conn, case_id, str(t).strip())
            except Exception:
                logger.exception("Case term translation failed")
        return _render_case_feed_tab(request, conn, case_id)


@app.post("/cases/{case_id}/terms/generate", response_class=HTMLResponse)
def case_generate_terms(
    request: Request, case_id: int, brief: str = Form(default="")
) -> HTMLResponse:
    """Let the AI expand the case's subject into tracking words + questions.

    Reuses the same query planner the old capsule builder used. Degrades
    gracefully: with no AI provider nothing is added and the tab simply redraws.
    """
    from nexus.analysis.query_builder import generate_plan

    settings = get_settings()
    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        try:
            plan = generate_plan(case["name"], (brief or "").strip(), settings)
        except Exception:
            logger.exception("Case term generation failed")
            plan = {}
        for term in plan.get("queries", []) or []:
            add_case_term(conn, case_id, term)
        for q in plan.get("requirements", []) or []:
            if (q or "").strip():
                add_requirement(conn, q.strip(), priority=1, case_id=case_id)
        return _render_case_feed_tab(request, conn, case_id)


@app.post("/cases/{case_id}/questions", response_class=HTMLResponse)
def case_add_question(
    request: Request, case_id: int, question: str = Form(...)
) -> HTMLResponse:
    question = (question or "").strip()
    with get_connection() as conn:
        if question:
            add_requirement(conn, question, priority=1, case_id=case_id)
        return _render_case_questions(request, conn, case_id)


@app.post("/cases/{case_id}/questions/{rid}/delete", response_class=HTMLResponse)
def case_delete_question(request: Request, case_id: int, rid: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_requirement(conn, rid)
        return _render_case_questions(request, conn, case_id)


@app.post("/cases/{case_id}/questions/generate", response_class=HTMLResponse)
def case_generate_questions(
    request: Request, case_id: int, brief: str = Form(default="")
) -> HTMLResponse:
    """Use AI to suggest intelligence questions for the case (PIRs only, no term changes)."""
    from nexus.analysis.query_builder import generate_plan

    settings = get_settings()
    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        try:
            plan = generate_plan(case["name"], (brief or case.get("description") or "").strip(), settings)
        except Exception:
            logger.exception("Case question generation failed")
            plan = {}
        for q in plan.get("requirements", []) or []:
            if (q or "").strip():
                add_requirement(conn, q.strip(), priority=1, case_id=case_id)
        return _render_case_questions(request, conn, case_id)


@app.post("/cases/{case_id}/subcases", response_class=HTMLResponse)
def case_add_subcase(
    request: Request, case_id: int, name: str = Form(...)
) -> HTMLResponse:
    name = (name or "").strip()
    with get_connection() as conn:
        if name:
            create_case(conn, name, parent_id=case_id)
        return _render_case_subcases(request, conn, case_id)


@app.post("/cases/{case_id}/subcases/{sub_id}/terms", response_class=HTMLResponse)
def subcase_add_terms(
    request: Request, case_id: int, sub_id: int,
    terms: str = Form(...), kind: str = Form(default="required"),
) -> HTMLResponse:
    """Add tracking or supplement words to a sub-case; re-renders the parent's sub-cases tab."""
    kind = kind if kind in ("required", "alert") else "required"
    with get_connection() as conn:
        for term in _split_terms(terms):
            add_case_term(conn, sub_id, term, kind=kind)
        return _render_case_subcases(request, conn, case_id)


@app.post("/cases/{case_id}/subcases/{sub_id}/terms/{term_id}/delete", response_class=HTMLResponse)
def subcase_delete_term(
    request: Request, case_id: int, sub_id: int, term_id: int,
) -> HTMLResponse:
    """Remove a tracking word from a sub-case; re-renders the parent's sub-cases tab."""
    with get_connection() as conn:
        conn.execute("DELETE FROM case_terms WHERE id = ? AND case_id = ?", (term_id, sub_id))
        return _render_case_subcases(request, conn, case_id)


@app.get("/cases/{case_id}/timeline", response_class=HTMLResponse)
def case_timeline_tab(request: Request, case_id: int) -> HTMLResponse:
    """Chronological view of all pinned items for this case."""
    with get_connection() as conn:
        case = get_case(conn, case_id)
        if not case:
            return HTMLResponse("Case not found", status_code=404)
        rows = case_timeline(conn, case_id)
    return TEMPLATES.TemplateResponse(
        request, "_case_timeline.html", {"case": case, "rows": rows}
    )


@app.post("/cases/{case_id}/notes", response_class=HTMLResponse)
def case_add_note(request: Request, case_id: int, body: str = Form(...)) -> HTMLResponse:
    body = (body or "").strip()
    with get_connection() as conn:
        if body:
            add_note(conn, body, case_id=case_id)
        notes = case_notes(conn, case_id)
    return TEMPLATES.TemplateResponse(
        request, "_notes.html", {"notes": notes, "case": {"id": case_id}}
    )


@app.post("/notes/{note_id}/update", response_class=HTMLResponse)
def note_update(request: Request, note_id: int, body: str = Form(...)) -> HTMLResponse:
    with get_connection() as conn:
        case_id = update_note(conn, note_id, body)
        notes = case_notes(conn, case_id) if case_id else []
    return TEMPLATES.TemplateResponse(
        request, "_notes.html", {"notes": notes, "case": {"id": case_id}}
    )


@app.post("/notes/{note_id}/delete", response_class=HTMLResponse)
def note_delete(request: Request, note_id: int) -> HTMLResponse:
    with get_connection() as conn:
        case_id = delete_note(conn, note_id)
        notes = case_notes(conn, case_id) if case_id else []
    return TEMPLATES.TemplateResponse(
        request, "_notes.html", {"notes": notes, "case": {"id": case_id}}
    )


def _render_case_items(request: Request, conn, case_id: int) -> HTMLResponse:
    items = case_items(conn, case_id)
    enrich_feed_rows(conn, items)
    return TEMPLATES.TemplateResponse(
        request, "_case_items.html", {"items": items, "case": {"id": case_id}}
    )


@app.post("/cases/{case_id}/pin-all", response_class=HTMLResponse)
def case_pin_all(case_id: int, item_ids: list[int] = Form(default=[])) -> HTMLResponse:
    """Pin a batch of items into a case (used by the Brief 'Pin all' button)."""
    with get_connection() as conn:
        for iid in item_ids:
            add_bookmark(conn, iid, case_id)
    return HTMLResponse(
        '<span class="text-emerald-400 text-xs mono">&#10003; All pinned</span>'
    )


@app.post("/cases/{case_id}/items", response_class=HTMLResponse)
def case_add_item(request: Request, case_id: int, item_id: int = Form(...)) -> HTMLResponse:
    with get_connection() as conn:
        add_bookmark(conn, item_id, case_id)
        return _render_case_items(request, conn, case_id)


@app.post("/cases/{case_id}/items/{item_id}/add", response_class=HTMLResponse)
def case_add_item_card(
    request: Request, case_id: int, item_id: int,
    context_case_id: int | None = Form(default=None),
) -> HTMLResponse:
    """Add an item to a case from a feed card's menu; returns the refreshed card."""
    with get_connection() as conn:
        add_bookmark(conn, item_id, case_id)
        return _render_card(request, conn, item_id, context_case_id)


@app.post("/cases/{case_id}/items/{item_id}/remove", response_class=HTMLResponse)
def case_remove_item_card(
    request: Request, case_id: int, item_id: int,
    context_case_id: int | None = Form(default=None),
) -> HTMLResponse:
    """Remove from a feed card's menu; returns the refreshed card."""
    with get_connection() as conn:
        remove_bookmark(conn, item_id, case_id)
        return _render_card(request, conn, item_id, context_case_id)


@app.post("/cases/{case_id}/items/{item_id}/unpin", response_class=HTMLResponse)
def case_unpin_item(request: Request, case_id: int, item_id: int) -> HTMLResponse:
    """Remove from the case-detail page; returns the refreshed item list."""
    with get_connection() as conn:
        remove_bookmark(conn, item_id, case_id)
        return _render_case_items(request, conn, case_id)


@app.post("/cases/quick-add/{item_id}", response_class=HTMLResponse)
def case_quick_add(
    request: Request, item_id: int, name: str = Form(...),
    context_case_id: int | None = Form(default=None),
) -> HTMLResponse:
    """Create a case from the card's inline form and pin the item into it."""
    with get_connection() as conn:
        new_id = create_case(conn, name.strip() or "Untitled case")
        add_bookmark(conn, item_id, new_id)
        return _render_card(request, conn, item_id, context_case_id)


# --- Reporting --------------------------------------------------------------


@app.get("/cases/{case_id}/report")
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
        suffix = "tracked" if use_live else "items"
        if fmt == "csv":
            return Response(
                content=feed_rows_to_csv(rows),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="case_{case_id}_{suffix}.csv"'},
            )
        return Response(
            content=feed_rows_to_json(rows),
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


@app.get("/cases/{case_id}/obsidian")
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


@app.get("/cases/{case_id}/evidence-manifest")
def case_evidence_manifest(case_id: int) -> Response:
    """Download a plain-text chain-of-custody manifest for a case: every captured
    evidence screenshot with its SHA-256 hash and capture timestamp. Court-ready
    provenance the analyst can keep alongside the exported screenshots."""
    from datetime import datetime, timezone

    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        rows = case_evidence(conn, case_id)

    now = datetime.now(timezone.utc)
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
            "",
        ]
    if not rows:
        lines.append("(No evidence captured for this case yet — capture some from a feed card.)")
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


# Hard cap on a single feed export so a huge history can never produce a
# multi-hundred-MB PDF that the renderer chokes on. Newest items win.
_MAX_EXPORT_ITEMS = 2000


def _filters_label(*, q, source, threat, since, until, window, unread, scope) -> str:
    """A short human description of the active filters for the export header."""
    parts: list[str] = []
    if q:
        parts.append(f'search "{q}"')
    parts.append("everything" if scope == "all" and not q else "tracked topics")
    if source:
        parts.append(f"source={source}")
    if threat:
        parts.append(f"threat={threat}")
    if window:
        parts.append(f"last {window}")
    if since:
        parts.append(f"from {since}")
    if until:
        parts.append(f"to {until}")
    if unread:
        parts.append("unread only")
    return ", ".join(parts)


@app.get("/export")
def feed_export(
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    window: str | None = None,
    unread: str | None = None,
    scope: str | None = None,
    format: str = "pdf",
) -> Response:
    """Export the feed (every item matching the current filters) to PDF or HTML.

    Mirrors the home-feed filters exactly so 'what you see is what you export'.
    Falls back to HTML when no PDF backend is installed, so export never fails.
    """
    scope = (scope or "topics").strip()
    since_ts = _window_since_ts(window)
    unread_only = bool(unread)
    with get_connection() as conn:
        terms = None
        if not q and scope == "topics":
            terms = _topic_terms(conn) or None
        items = search_items(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, terms=terms,
            limit=_MAX_EXPORT_ITEMS, offset=0,
        )
        enrich_feed_rows(conn, items)
        total_matched = count_matching_items(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, unread_only=unread_only, terms=terms,
        )

    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    fmt = (format or "pdf").strip().lower()

    # Machine-readable exports for analysts who want the raw data in a spreadsheet
    # or another tool — same filtered rows as the report, no PDF backend needed.
    if fmt == "csv":
        return Response(
            content=feed_rows_to_csv(items),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="feed_export_{stamp}.csv"'},
        )
    if fmt == "json":
        return Response(
            content=feed_rows_to_json(items),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="feed_export_{stamp}.json"'},
        )

    label = _filters_label(
        q=q, source=source, threat=threat, since=since, until=until,
        window=window, unread=unread_only, scope=scope,
    )
    html = render_feed_report_html(
        TEMPLATES, items=items, total_matched=total_matched, filters_label=label,
    )

    if fmt == "html":
        return HTMLResponse(html)

    pdf = render_report_pdf(html)
    if pdf is None:
        # Graceful fallback: serve HTML (the browser can still print to PDF).
        return HTMLResponse(html)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="feed_export_{stamp}.pdf"'
        },
    )


# --- Air-gap transfer (export / import a portable intelligence bundle) -------


@app.get("/transfer", response_class=HTMLResponse)
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


@app.post("/transfer/export")
def transfer_export(
    scope: str = Form(default="all"),
    case_id: str | None = Form(default=None),
    only_new: str | None = Form(default=None),
) -> Response:
    """Build a ``.nexusbundle`` and stream it as a download for the USB stick."""
    from datetime import datetime, timezone

    from nexus.transfer import export_bundle

    cid = int(case_id) if (scope == "case" and case_id and case_id.isdigit()) else None
    with get_connection() as conn:
        blob, summary = export_bundle(
            conn, scope=scope, case_id=cid, only_new=bool(only_new)
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
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


@app.post("/transfer/import", response_class=HTMLResponse)
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


# --- Security center: outbound-traffic monitor + file safety check -----------
@app.get("/security", response_class=HTMLResponse)
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


@app.post("/security/scan-file", response_class=HTMLResponse)
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


@app.get("/security/report")
def security_audit_report() -> Response:
    """Download a plain-text data-handling audit report (egress snapshot)."""
    from datetime import datetime, timezone

    from nexus.security import format_audit_report

    text = format_audit_report()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"nexus_data_handling_report_{stamp}.txt"
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- In-dashboard analyst assistant -----------------------------------------


@app.post("/assistant/ask")
def assistant_ask(
    question: str = Body(default="", embed=True),
    history: list = Body(default=[], embed=True),
    page: str = Body(default="", embed=True),
    deep: bool = Body(default=False, embed=True),
    cite: bool = Body(default=True, embed=True),
) -> JSONResponse:
    """Answer one analyst request, grounded in the local data — and act on it.

    Sherlock (the assistant) can both answer questions and take constructive
    actions (build a case, fill it with matching items, generate a report). A
    sync route on purpose: the model call blocks, so FastAPI runs it in a worker
    thread (keeping the event loop free). Always returns JSON; never raises — the
    assistant degrades to a clear message instead.
    """
    from nexus.assistant import act

    # Keep history small and well-shaped regardless of what the client sends.
    safe_history: list[dict] = []
    if isinstance(history, list):
        for turn in history[-12:]:
            if isinstance(turn, dict) and turn.get("role") in ("user", "assistant"):
                safe_history.append(
                    {"role": turn["role"], "content": str(turn.get("content") or "")}
                )

    # The current page (path + query) lets Sherlock resolve "this", "here" and
    # "the current case" to what the analyst is actually looking at. Bounded so a
    # crafted client can't bloat the prompt.
    page_ctx = str(page or "")[:300]

    try:
        with get_connection() as conn:
            result = act(question, conn=conn, history=safe_history, page=page_ctx,
                         deep=bool(deep), cite=bool(cite))
    except Exception:
        logger.exception("Assistant request failed")
        result = {
            "ok": False,
            "answer": "",
            "provider": "off",
            "actions": [],
            "error": "Something went wrong answering that. Please try again.",
        }
    return JSONResponse(result)


@app.post("/assistant/save-log")
def assistant_save_log(transcript: list = Body(default=[], embed=True)) -> JSONResponse:
    """Save a chat transcript to a LOCAL log file — opt-in, from "End chat".

    Local-only by design: it writes under the app's own data directory and never
    leaves this machine. Only the role/content text is stored (no system prompt,
    no secrets) so the analyst keeps a private record of what they asked.
    """
    import json as _json
    from datetime import datetime, timezone

    turns: list[dict] = []
    if isinstance(transcript, list):
        for t in transcript[-200:]:
            if isinstance(t, dict) and t.get("role") in ("user", "assistant"):
                turns.append(
                    {"role": t["role"], "content": str(t.get("content") or "")[:4000]}
                )
    if not turns:
        return JSONResponse({"ok": False, "saved": 0})

    try:
        settings = get_settings()
        log_dir = settings.data_path / "assistant_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        path = log_dir / f"sherlock_{now.strftime('%Y%m%d')}.jsonl"
        record = {"saved_at": now.isoformat(timespec="seconds"), "turns": turns}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Assistant: failed to save chat log")
        return JSONResponse({"ok": False, "saved": 0})
    return JSONResponse({"ok": True, "saved": len(turns)})


# --- Watchlists + alerts ----------------------------------------------------

WATCHLIST_KINDS = ["keyword", "regex", "wallet", "phone"]


@app.get("/watchlists/count")
def watchlists_count() -> dict:
    """Return unseen watchlist hit count for the nav badge."""
    with get_connection() as conn:
        return {"total": count_new_watchlist_hits(conn)}


@app.get("/watchlists", response_class=HTMLResponse)
def watchlists(request: Request) -> HTMLResponse:
    settings = get_settings()
    with get_connection() as conn:
        wls = list_watchlists(conn)
        hits = list_watchlist_hits(conn)
        active_case = get_active_case(conn)
        mark_watchlist_hits_seen(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "watchlists.html",
        {
            "watchlists": wls,
            "hits": hits,
            "active_case": active_case,
            "kinds": WATCHLIST_KINDS,
            "status": settings.availability_report(),
        },
    )


@app.post("/watchlists", response_class=HTMLResponse)
def watchlists_create(
    request: Request,
    label: str = Form(...),
    pattern: str = Form(...),
    kind: str = Form(default="keyword"),
) -> HTMLResponse:
    if kind not in WATCHLIST_KINDS:
        kind = "keyword"
    with get_connection() as conn:
        create_watchlist(conn, label.strip(), pattern.strip(), kind)
        wls = list_watchlists(conn)
    return TEMPLATES.TemplateResponse(
        request, "_watchlist_list.html", {"watchlists": wls}
    )


@app.post("/watchlists/{watchlist_id}/delete", response_class=HTMLResponse)
def watchlists_delete(request: Request, watchlist_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_watchlist(conn, watchlist_id)
        wls = list_watchlists(conn)
    return TEMPLATES.TemplateResponse(
        request, "_watchlist_list.html", {"watchlists": wls}
    )


@app.post("/watchlists/{watchlist_id}/toggle", response_class=HTMLResponse)
def watchlists_toggle(request: Request, watchlist_id: int) -> HTMLResponse:
    with get_connection() as conn:
        toggle_watchlist(conn, watchlist_id)
        wls = list_watchlists(conn)
    return TEMPLATES.TemplateResponse(
        request, "_watchlist_list.html", {"watchlists": wls}
    )


# --- Topics: choose what to collect -----------------------------------------
# Lets the analyst pick collection targets from the dashboard instead of editing
# .env. Subscriptions are stored in the DB and merged with env targets at scan
# time. Curated presets are one-click bundles of real, key-free RSS feeds.

# Sources whose targets can be managed as topics, with input guidance.
TOPIC_SOURCES = [
    {"id": "rss", "label": "RSS feed", "placeholder": "https://example.com/feed.xml",
     "hint": "Any RSS/Atom URL. No API key needed."},
    {"id": "serpapi", "label": "Google News query (SERPAPI)", "placeholder": "ransomware attack",
     "hint": "Requires SERPAPI_KEY in .env. Richer Google News results."},
    {"id": "reddit", "label": "Subreddit", "placeholder": "worldnews",
     "hint": "Pulls 'new' posts from a subreddit. Requires Reddit credentials in .env."},
    {"id": "twitter", "label": "Twitter/X query", "placeholder": "#osint",
     "hint": "Requires TWITTER_BEARER_TOKEN in .env."},
    {"id": "telegram", "label": "Telegram channel", "placeholder": "durov",
     "hint": "Requires TELEMETRY_API_KEY in .env."},
]
_TOPIC_SOURCE_IDS = {s["id"] for s in TOPIC_SOURCES}

# Curated one-click bundles. All RSS (no key required) so they work out of the box.
TOPIC_PRESETS = {
    "World News": [
        ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
        ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera"),
        ("https://feeds.reuters.com/Reuters/worldNews", "Reuters World"),
    ],
    "Technology": [
        ("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica"),
        ("https://www.theverge.com/rss/index.xml", "The Verge"),
        ("https://feeds.feedburner.com/TechCrunch/", "TechCrunch"),
    ],
    "Cyber Security": [
        ("https://feeds.feedburner.com/TheHackersNews", "The Hacker News"),
        ("https://krebsonsecurity.com/feed/", "Krebs on Security"),
        ("https://www.bleepingcomputer.com/feed/", "BleepingComputer"),
    ],
    "Military & Defense": [
        ("https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml", "Defense News"),
        ("https://www.military.com/rss-feeds/content?keyword=&type=news", "Military.com"),
    ],
    "Finance & Crypto": [
        ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
        ("https://cointelegraph.com/rss", "Cointelegraph"),
    ],
    "Science": [
        ("https://www.sciencedaily.com/rss/all.xml", "ScienceDaily"),
        ("https://feeds.nature.com/nature/rss/current", "Nature"),
    ],
}


def _grouped_subscriptions(conn) -> dict[str, list[dict]]:
    """All subscriptions grouped by source id (for the Topics table)."""
    grouped: dict[str, list[dict]] = {s["id"]: [] for s in TOPIC_SOURCES}
    for sub in list_subscriptions(conn):
        grouped.setdefault(sub["source"], []).append(sub)
    return grouped


# Persona-driven starter templates. Each one pre-fills the capsule form (name +
# example terms) and a matching intelligence requirement, so a journalist, a
# market analyst or an investigator all get a clear, correct starting point.
CAPSULE_TEMPLATES = [
    {
        "id": "person",
        "icon": "&#128100;",  # bust in silhouette
        "title": "Public figure",
        "who": "Journalists & researchers",
        "desc": "Track a person, what they say, and how others react.",
        "name": "Public figure",
        "terms": "Full name, known alias, @handle, organisation",
        "question": "What is being said by or about this person, and why does it matter?",
    },
    {
        "id": "company",
        "icon": "&#127970;",  # office building
        "title": "Company & market",
        "who": "Investment & due-diligence teams",
        "desc": "Monitor a company, its leadership, filings and market chatter.",
        "name": "Company watch",
        "terms": "Company name, stock ticker, CEO name, \"earnings\", \"lawsuit\"",
        "question": "What developments could move this company's value or reputation?",
    },
    {
        "id": "threat",
        "icon": "&#128737;",  # shield
        "title": "Threat actor",
        "who": "Intelligence & security analysts",
        "desc": "Follow a threat group, its malware, CVEs and infrastructure.",
        "name": "Threat actor",
        "terms": "Group name, malware family, CVE-2026-XXXX, domain or wallet",
        "question": "What new activity, capability or targeting does this threat show?",
    },
    {
        "id": "event",
        "icon": "&#127757;",  # globe
        "title": "Event / topic",
        "who": "Anyone covering a story",
        "desc": "Cover an unfolding event, protest, conflict or theme.",
        "name": "Live event",
        "terms": "Event name, location, key people, hashtag",
        "question": "What are the latest credible developments on this event?",
    },
]


@app.get("/topics", response_class=HTMLResponse)
def topics(request: Request) -> HTMLResponse:
    """Choose collection topics: one-click presets + custom targets."""
    settings = get_settings()
    with get_connection() as conn:
        grouped = _grouped_subscriptions(conn)
        capsules = list_query_capsules(conn)
        source_stats = conn.execute(
            """
            SELECT source,
                   COUNT(*)                                                      AS total,
                   MAX(fetched_at)                                               AS last_at,
                   SUM(CASE WHEN date(fetched_at) = date('now') THEN 1 ELSE 0 END) AS today
            FROM items
            GROUP BY source
            ORDER BY last_at DESC
            """
        ).fetchall()
    return TEMPLATES.TemplateResponse(
        request,
        "topics.html",
        {
            "status": settings.availability_report(),
            "sources": TOPIC_SOURCES,
            "presets": TOPIC_PRESETS,
            "grouped": grouped,
            "capsules": capsules,
            "capsule_templates": CAPSULE_TEMPLATES,
            "source_stats": [dict(r) for r in source_stats],
            "today_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        },
    )


def _split_terms(raw: str) -> list[str]:
    """Split a free-text term list on commas / newlines into clean terms."""
    out: list[str] = []
    for chunk in (raw or "").replace("\n", ",").split(","):
        term = chunk.strip()
        if term and term not in out:
            out.append(term)
    return out


def _capsules_response(request: Request) -> HTMLResponse:
    with get_connection() as conn:
        capsules = list_query_capsules(conn)
    return TEMPLATES.TemplateResponse(
        request, "_capsules.html", {"capsules": capsules}
    )


@app.post("/topics/capsule", response_class=HTMLResponse)
def topics_create_capsule(
    request: Request,
    name: str = Form(...),
    terms: str = Form(""),
    rank: str = Form(""),
    question: str = Form(""),
    questions: str = Form(""),
) -> HTMLResponse:
    """Create (or extend) a named investigation capsule from a list of terms.

    Every term is stored as a unified query labelled with the capsule name, so
    it is searched across every keyword-capable source on each scan. When
    ``rank`` is ticked, a matching intelligence requirement is created so the AI
    ranks collected items by how relevant they are to this subject. ``questions``
    (newline-separated) carries any AI-suggested ranking questions from the query
    builder; each becomes its own standing requirement.
    """
    name = (name or "").strip() or "General"
    with get_connection() as conn:
        for term in _split_terms(terms):
            add_subscription(conn, "query", term, label=name)
        # AI-suggested ranking questions (one per line), if any. Each is tagged
        # with the capsule name so it shows up as a sub-question of this
        # investigation.
        added_questions = False
        for q in (line.strip() for line in (questions or "").splitlines()):
            if q:
                add_requirement(conn, q, priority=1, topic=name)
                added_questions = True
        # Fall back to the single-question / default rank only when the builder
        # did not already supply explicit questions.
        if rank and not added_questions:
            q = (question or "").strip() or (
                f"What is the latest significant information about {name}?"
            )
            add_requirement(conn, q, priority=1, topic=name)
    return _capsules_response(request)


@app.post("/topics/capsule/generate", response_class=HTMLResponse)
def topics_capsule_generate(
    request: Request,
    name: str = Form(...),
    brief: str = Form(""),
) -> HTMLResponse:
    """Turn a plain-English intelligence brief into a reviewable capsule plan.

    Calls the active AI provider to draft concrete search queries and ranking
    questions, then returns an editable preview the analyst confirms before
    saving. Degrades gracefully: with no AI key it returns a notice pointing the
    analyst to the manual form.
    """
    from nexus.analysis.query_builder import generate_plan

    settings = get_settings()
    name = (name or "").strip() or "General"
    plan = generate_plan(name, brief, settings)
    return TEMPLATES.TemplateResponse(
        request,
        "_capsule_preview.html",
        {
            "name": name,
            "brief": (brief or "").strip(),
            "plan": plan,
            # Pre-joined values so the template never has to embed newlines.
            "terms_value": ", ".join(plan.get("queries", [])),
            "questions_value": "\n".join(plan.get("requirements", [])),
            "analysis_enabled": settings.analysis_enabled,
        },
    )


@app.post("/topics/capsule/term", response_class=HTMLResponse)
def topics_capsule_add_term(
    request: Request,
    name: str = Form(...),
    value: str = Form(...),
) -> HTMLResponse:
    """Add a single term to an existing capsule."""
    name = (name or "").strip() or "General"
    value = (value or "").strip()
    with get_connection() as conn:
        if value:
            add_subscription(conn, "query", value, label=name)
    return _capsules_response(request)


@app.post("/topics/capsule/term/{sub_id}/delete", response_class=HTMLResponse)
def topics_capsule_delete_term(request: Request, sub_id: int) -> HTMLResponse:
    """Remove one term from a capsule."""
    with get_connection() as conn:
        delete_subscription(conn, sub_id)
    return _capsules_response(request)


@app.post("/topics/capsule/delete", response_class=HTMLResponse)
def topics_capsule_delete(request: Request, name: str = Form(...)) -> HTMLResponse:
    """Delete a whole capsule and all of its terms."""
    with get_connection() as conn:
        delete_capsule(conn, name)
    return _capsules_response(request)


@app.post("/topics/preset", response_class=HTMLResponse)
def topics_add_preset(request: Request, preset: str = Form(...)) -> HTMLResponse:
    """Subscribe to every feed in a curated preset (idempotent)."""
    with get_connection() as conn:
        for value, label in TOPIC_PRESETS.get(preset, []):
            add_subscription(conn, "rss", value, label)
        grouped = _grouped_subscriptions(conn)
    return TEMPLATES.TemplateResponse(
        request, "_subscriptions.html", {"grouped": grouped, "sources": TOPIC_SOURCES}
    )


@app.post("/onboard/bundle", response_class=HTMLResponse)
def onboard_bundle(request: Request, preset: str = Form(...)) -> HTMLResponse:
    """One-click from the home welcome panel: subscribe to a curated bundle and
    re-render the getting-started panel so step 1 flips to done in place — the
    analyst never has to leave the feed to get started."""
    settings = get_settings()
    with get_connection() as conn:
        for value, label in TOPIC_PRESETS.get(preset, []):
            add_subscription(conn, "rss", value, label)
        has_targets = bool(list_subscriptions(conn))
        total = count_items(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "_getting_started.html",
        {
            "setup": _setup_state(settings, total, has_targets=has_targets),
            "bundles": list(TOPIC_PRESETS.keys()),
        },
    )


@app.post("/topics/add", response_class=HTMLResponse)
def topics_add(
    request: Request,
    source: str = Form(...),
    value: str = Form(...),
    label: str = Form(default=""),
) -> HTMLResponse:
    """Add a single custom collection target."""
    value = (value or "").strip()
    label = (label or "").strip() or None
    if source in _TOPIC_SOURCE_IDS and value:
        with get_connection() as conn:
            add_subscription(conn, source, value, label)
    with get_connection() as conn:
        grouped = _grouped_subscriptions(conn)
    return TEMPLATES.TemplateResponse(
        request, "_subscriptions.html", {"grouped": grouped, "sources": TOPIC_SOURCES}
    )


@app.post("/topics/{sub_id}/delete", response_class=HTMLResponse)
def topics_delete(request: Request, sub_id: int) -> HTMLResponse:
    """Remove a collection target."""
    with get_connection() as conn:
        delete_subscription(conn, sub_id)
        grouped = _grouped_subscriptions(conn)
    return TEMPLATES.TemplateResponse(
        request, "_subscriptions.html", {"grouped": grouped, "sources": TOPIC_SOURCES}
    )


# --- Intelligence requirements (PIRs) ---------------------------------------
# Standing questions the analyst wants answered. The AI scores collected items
# for relevance to each on every scan; the Intel view ranks by that score.

REQUIREMENT_PRIORITIES = {1, 2, 3}


def _to_int(value, default: int | None = None) -> int | None:
    """Parse an optional form/query value to int, tolerating '' and None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@app.get("/requirements", response_class=HTMLResponse)
def requirements_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    with get_connection() as conn:
        reqs = list_requirements(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "requirements.html",
        {
            "requirements": reqs,
            "status": settings.availability_report(),
            "analysis_enabled": settings.analysis_enabled,
        },
    )


@app.post("/requirements", response_class=HTMLResponse)
def requirements_create(
    request: Request, question: str = Form(...), priority: int = Form(default=2)
) -> HTMLResponse:
    if priority not in REQUIREMENT_PRIORITIES:
        priority = 2
    question = (question or "").strip()
    with get_connection() as conn:
        if question:
            add_requirement(conn, question, priority)
        reqs = list_requirements(conn)
    return TEMPLATES.TemplateResponse(
        request, "_requirement_list.html", {"requirements": reqs}
    )


@app.post("/requirements/{req_id}/toggle", response_class=HTMLResponse)
def requirements_toggle(request: Request, req_id: int) -> HTMLResponse:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT enabled FROM requirements WHERE id = ?", (req_id,)
        ).fetchone()
        if row is not None:
            set_requirement_enabled(conn, req_id, not bool(row["enabled"]))
        reqs = list_requirements(conn)
    return TEMPLATES.TemplateResponse(
        request, "_requirement_list.html", {"requirements": reqs}
    )


@app.post("/requirements/{req_id}/delete", response_class=HTMLResponse)
def requirements_delete(request: Request, req_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_requirement(conn, req_id)
        reqs = list_requirements(conn)
    return TEMPLATES.TemplateResponse(
        request, "_requirement_list.html", {"requirements": reqs}
    )


# --- Intel: items ranked by requirement relevance ---------------------------


def _intel_filters(requirement_id, min_score, since, until, q=None) -> dict:
    return {
        "requirement_id": str(requirement_id) if requirement_id else "",
        "min_score": str(min_score or 1),
        "since": since or "",
        "until": until or "",
        "q": q or "",
    }


@app.get("/intel", response_class=HTMLResponse)
def intel(
    request: Request,
    requirement_id: str | None = None,
    min_score: str | None = None,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
) -> HTMLResponse:
    """The relevance-ranked intelligence view."""
    settings = get_settings()
    rid = _to_int(requirement_id)
    score = _to_int(min_score, 1) or 1
    with get_connection() as conn:
        reqs = list_requirements(conn)
        items = intel_items(
            conn, requirement_id=rid, min_score=score, since=since, until=until, q=q
        )
        active_case = get_active_case(conn)
        enrich_feed_rows(conn, items)
    return TEMPLATES.TemplateResponse(
        request,
        "intel.html",
        {
            "items": items,
            "requirements": reqs,
            "status": settings.availability_report(),
            "filters": _intel_filters(rid, score, since, until, q),
            "analysis_enabled": settings.analysis_enabled,
            "has_requirements": bool(reqs),
            "active_case": active_case,
        },
    )


@app.get("/intel/export")
def intel_export(
    requirement_id: str | None = None,
    min_score: str | None = None,
    since: str | None = None,
    until: str | None = None,
    format: str = "csv",
) -> Response:
    """Download the relevance-ranked intelligence view as CSV or JSON data."""
    from datetime import datetime, timezone

    rid = _to_int(requirement_id)
    score = _to_int(min_score, 1) or 1
    with get_connection() as conn:
        rows = intel_items(
            conn, requirement_id=rid, min_score=score, since=since, until=until,
            limit=_MAX_EXPORT_ITEMS,
        )
    # Append the relevance score as an extra column — the whole point of this view.
    from nexus.reporting import DATA_EXPORT_FIELDS

    fields = DATA_EXPORT_FIELDS + ["rel_score"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    if (format or "csv").strip().lower() == "json":
        return Response(
            content=feed_rows_to_json(rows, fields=fields),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="intel_export_{stamp}.json"'},
        )
    return Response(
        content=feed_rows_to_csv(rows, fields=fields),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="intel_export_{stamp}.csv"'},
    )


@app.get("/intel/feed", response_class=HTMLResponse)
def intel_feed(
    request: Request,
    requirement_id: str | None = None,
    min_score: str | None = None,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
) -> HTMLResponse:
    """htmx partial: the ranked list only (live filtering)."""
    rid = _to_int(requirement_id)
    score = _to_int(min_score, 1) or 1
    settings = get_settings()
    with get_connection() as conn:
        items = intel_items(
            conn, requirement_id=rid, min_score=score, since=since, until=until, q=q
        )
        has_requirements = bool(list_requirements(conn))
        active_case = get_active_case(conn)
        enrich_feed_rows(conn, items)
    return TEMPLATES.TemplateResponse(
        request,
        "_intel_feed.html",
        {
            "items": items,
            "analysis_enabled": settings.analysis_enabled,
            "has_requirements": has_requirements,
            "active_case": active_case,
        },
    )


@app.post("/intel/scan", response_class=HTMLResponse)
def intel_scan(
    request: Request,
    requirement_id: str | None = Form(default=None),
    min_score: str | None = Form(default=None),
    since: str | None = Form(default=None),
    until: str | None = Form(default=None),
    q: str | None = Form(default=None),
) -> HTMLResponse:
    """Manual scan from the Intel view — runs collection + scoring, then ranks."""
    collector: Collector = request.app.state.collector
    stats = collector.scan()
    rid = _to_int(requirement_id)
    score = _to_int(min_score, 1) or 1
    settings = get_settings()
    with get_connection() as conn:
        items = intel_items(
            conn,
            requirement_id=rid,
            min_score=score,
            since=since or None,
            until=until or None,
            q=q or None,
        )
        has_requirements = bool(list_requirements(conn))
        active_case = get_active_case(conn)
        enrich_feed_rows(conn, items)
    return TEMPLATES.TemplateResponse(
        request,
        "_intel_feed.html",
        {
            "items": items,
            "scan_stats": stats,
            "analysis_enabled": settings.analysis_enabled,
            "has_requirements": has_requirements,
            "active_case": active_case,
        },
    )


# --- Settings / onboarding --------------------------------------------------

# Secret variables the analyst can set from the dashboard. Each maps to one
# allow-listed key in nexus/envstore.py. The actual values are written to .env
# only (never the DB) and are never rendered back — the UI shows Set / Not set.
SECRET_FIELDS = [
    {"key": "ANTHROPIC_API_KEY", "label": "Anthropic (Claude) API key",
     "hint": "Powers AI summaries, translation and threat scoring.",
     "url": "https://console.anthropic.com/settings/keys",
     "steps": [
         "Open console.anthropic.com and sign in (or create an account).",
         "Add a payment method under Billing — usage is pay-as-you-go.",
         "Go to Settings → API keys and click 'Create Key'.",
         "Copy the key (starts with sk-ant-) and paste it here.",
     ]},
    {"key": "GEMINI_API_KEY", "label": "Google (Gemini) API key",
     "hint": "Free alternative AI provider for analysis.",
     "url": "https://aistudio.google.com/app/apikey",
     "steps": [
         "Open aistudio.google.com and sign in with a Google account.",
         "Click 'Get API key' → 'Create API key'.",
         "Copy the generated key and paste it here.",
     ]},
    {"key": "OPENAI_API_KEY", "label": "OpenAI (ChatGPT) API key",
     "hint": "Alternative AI provider for analysis (GPT models).",
     "url": "https://platform.openai.com/api-keys",
     "steps": [
         "Open platform.openai.com and sign in (or create an account).",
         "Add a payment method under Billing — usage is pay-as-you-go.",
         "Go to API keys and click 'Create new secret key'.",
         "Copy the key (starts with sk-) and paste it here.",
     ]},
    {"key": "SERPAPI_KEY", "label": "SERPAPI key (Google News)",
     "hint": "Lets capsule terms search Google News.",
     "url": "https://serpapi.com/manage-api-key",
     "steps": [
         "Sign up at serpapi.com (the free plan allows 100 searches/month).",
         "Open the 'Api Key' page from your dashboard.",
         "Copy your private API key and paste it here.",
         "Then add a capsule on the Topics page to give it something to search.",
     ]},
    {"key": "GOOGLE_CSE_KEY", "label": "Google Custom Search API key",
     "hint": "Searches the wider indexed web for capsule terms (free: 100/day).",
     "url": "https://developers.google.com/custom-search/v1/introduction",
     "steps": [
         "Open the link and click 'Get a Key' to create/select a Google Cloud project.",
         "Copy the generated API key and paste it here.",
         "Then set the Search Engine ID (cx) below to finish enabling it.",
     ]},
    {"key": "GOOGLE_CSE_CX", "label": "Google Custom Search engine ID (cx)",
     "hint": "The Programmable Search Engine the API key queries.",
     "url": "https://programmablesearchengine.google.com/controlpanel/all",
     "steps": [
         "Open the link and click 'Add' to create a search engine.",
         "Choose 'Search the entire web' so it isn't limited to one site.",
         "Open the engine, copy the 'Search engine ID', and paste it here.",
     ]},
    {"key": "REDDIT_CLIENT_ID", "label": "Reddit client ID",
     "hint": "First half of Reddit API credentials.",
     "url": "https://www.reddit.com/prefs/apps",
     "steps": [
         "Open reddit.com/prefs/apps while logged in.",
         "Click 'create another app...' at the bottom.",
         "Choose type 'script', set redirect URI to http://localhost:8000.",
         "The client ID is the short string just under the app name — paste it here.",
     ]},
    {"key": "REDDIT_CLIENT_SECRET", "label": "Reddit client secret",
     "hint": "Second half of Reddit API credentials.",
     "url": "https://www.reddit.com/prefs/apps",
     "steps": [
         "On the same reddit.com/prefs/apps page, open your script app.",
         "Copy the value labelled 'secret' and paste it here.",
     ]},
    {"key": "TWITTER_BEARER_TOKEN", "label": "Twitter / X bearer token",
     "hint": "Lets capsule terms search Twitter/X.",
     "url": "https://developer.twitter.com/en/portal/dashboard",
     "steps": [
         "Apply for access at developer.twitter.com and create a Project + App.",
         "Open your App → 'Keys and tokens'.",
         "Under 'Bearer Token' click Generate, then copy it.",
         "Paste the bearer token here.",
     ]},
    {"key": "TELEMETRY_API_KEY", "label": "Telegram (Telemetry) key",
     "hint": "Reads public Telegram channels and search via telemetryapp.io.",
     "url": "https://www.telemetryapp.io",
     "steps": [
         "Open Telegram and start a chat with @telemetrio_api_bot.",
         "Follow the bot's prompts to generate an access key.",
         "Copy the key and paste it here (it is sent as an 'api_key' header).",
         "Then add channels or a capsule on the Topics page.",
     ]},
    {"key": "INSTAGRAM_SESSIONID", "label": "Instagram session ID (Toutatis)",
     "hint": "Enables the Toutatis Instagram tool. Use a throwaway account.",
     "url": "https://www.instagram.com",
     "steps": [
         "Log in to Instagram in your browser (prefer a burner account).",
         "Open the browser dev tools (F12) → Application/Storage → Cookies.",
         "Find the cookie named 'sessionid' and copy its value.",
         "Paste it here. Note: it expires when you log out of that session.",
     ]},
]


from nexus.analysis.ollama_admin import RECOMMENDED_MODELS as OLLAMA_RECOMMENDED


def _secret_status(settings) -> dict[str, bool]:
    """Per-key 'is a value set?' map — booleans only, never the secret itself."""
    return {f["key"]: bool(getattr(settings, f["key"].lower(), None)) for f in SECRET_FIELDS}


def _settings_context(settings) -> dict:
    return {
        "status": settings.availability_report(),
        "claude_model": settings.claude_model,
        "gemini_model": settings.gemini_model,
        "translation_target_lang": settings.translation_target_lang,
        "rss_feeds": settings.rss_feed_list,
        "max_items": settings.analysis_max_items_per_run,
        "provider_choices": settings.PROVIDER_CHOICES,
        "chosen_provider": settings.chosen_provider(),
        "active_provider": settings.active_provider(),
        "claude_enabled": settings.claude_enabled,
        "gemini_enabled": settings.gemini_enabled,
        "openai_enabled": settings.openai_enabled,
        "openai_model": settings.openai_model,
        "ollama_enabled": settings.ollama_enabled,
        "ollama_model": settings.effective_ollama_model(),
        "ollama_base_url": settings.ollama_base_url,
        "ollama_recommended": OLLAMA_RECOMMENDED,
        "secret_fields": SECRET_FIELDS,
        "secret_status": _secret_status(settings),
        # Keyless English-translation fallback. The URL is NOT a secret, so unlike
        # the API keys it is shown back so the operator can see/edit it.
        "libretranslate_url": settings.libretranslate_url or "",
        "libretranslate_enabled": settings.libretranslate_enabled,
        "saved": False,
        "translation_saved": False,
        "translation_msg": "",
    }


@app.get("/guide", response_class=HTMLResponse)
def guide(request: Request) -> HTMLResponse:
    """Beginner-friendly walkthrough: what each page does and how to use them."""
    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request,
        "guide.html",
        {"status": settings.availability_report()},
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Configuration overview, onboarding guide, and the secure key editor.

    API keys entered here are written to the local, git-ignored .env file only —
    never the database — and are never rendered back (the UI shows Set / Not set).
    The provider *choice* (not a secret) is stored in the DB meta table.
    """
    settings = get_settings()
    return TEMPLATES.TemplateResponse(request, "settings.html", _settings_context(settings))


@app.post("/settings/auto-scan", response_class=JSONResponse)
def settings_set_auto_scan(interval_hours: int = Form(default=0)) -> JSONResponse:
    """Set the automatic scan interval (0 = off, otherwise hours between scans)."""
    interval_hours = max(0, min(interval_hours, 168))  # cap at 1 week
    with get_connection() as conn:
        set_meta(conn, "auto_scan_interval", str(interval_hours))
        if interval_hours == 0:
            set_meta(conn, "last_auto_scan_at", None)
    return JSONResponse({"ok": True, "interval_hours": interval_hours})


@app.get("/settings/auto-scan-status", response_class=JSONResponse)
def settings_auto_scan_status() -> JSONResponse:
    """Return current auto-scan config and next scheduled time."""
    with get_connection() as conn:
        interval_h = int(get_meta(conn, "auto_scan_interval") or 0)
        last_raw = get_meta(conn, "last_auto_scan_at") or ""
    next_at = ""
    if interval_h > 0 and last_raw:
        try:
            last_dt = datetime.fromisoformat(last_raw)
            next_dt = last_dt + timedelta(hours=interval_h)
            next_at = next_dt.strftime("%H:%M")
        except ValueError:
            pass
    return JSONResponse({"interval_hours": interval_h, "next_at": next_at,
                         "last_at": last_raw[:16] if last_raw else ""})


@app.post("/settings/provider", response_class=HTMLResponse)
def settings_set_provider(request: Request, provider: str = Form(...)) -> HTMLResponse:
    """Switch the active AI provider (anthropic / gemini / off). Not a secret."""
    settings = get_settings()
    if provider in settings.PROVIDER_CHOICES:
        with get_connection() as conn:
            set_meta(conn, "ai_provider", provider)
    return TEMPLATES.TemplateResponse(
        request, "_provider_status.html", _settings_context(settings)
    )


@app.post("/settings/ollama/check", response_class=HTMLResponse)
def settings_check_ollama(request: Request) -> HTMLResponse:
    """Ping the local Ollama server and report status in plain language.

    Lets a non-technical user confirm their local model is set up correctly
    without touching a terminal. Talks only to localhost; never leaks anything.
    """
    from nexus.analysis.providers import check_ollama

    settings = get_settings()
    result = check_ollama(settings.ollama_base_url, settings.effective_ollama_model())
    return TEMPLATES.TemplateResponse(
        request, "_ollama_check.html", {"check": result}
    )


@app.post("/settings/ollama/pull", response_class=HTMLResponse)
def settings_ollama_pull(request: Request, model: str = Form("")) -> HTMLResponse:
    """Download a local model from inside the app — no terminal needed.

    Persists the picked model as the active local model (DB meta override, so it
    takes effect with no restart), then kicks off the download on the local
    Ollama server and returns a self-polling progress partial. Talks only to
    localhost; nothing leaves the machine.
    """
    from nexus.analysis.ollama_admin import start_pull

    settings = get_settings()
    model = (model or "").strip() or settings.effective_ollama_model()
    # Remember the choice so analysis uses it immediately (preference, not secret).
    with get_connection() as conn:
        set_meta(conn, "ollama_model", model)
    state = start_pull(settings.ollama_base_url, model)
    return TEMPLATES.TemplateResponse(request, "_ollama_pull.html", {"pull": state})


@app.get("/settings/ollama/pull/status", response_class=HTMLResponse)
def settings_ollama_pull_status(request: Request) -> HTMLResponse:
    """Current download progress — the progress partial polls this while running."""
    from nexus.analysis.ollama_admin import pull_state

    return TEMPLATES.TemplateResponse(
        request, "_ollama_pull.html", {"pull": pull_state()}
    )


@app.post("/settings/secrets", response_class=HTMLResponse)
async def settings_set_secrets(request: Request) -> HTMLResponse:
    """Persist API keys to .env (never the DB) and reload settings live.

    A blank field leaves the existing value untouched, so one key can be updated
    without wiping the rest; ticking 'clear' removes a key. After writing, the
    cached settings are dropped and the collector is rebuilt so new keys take
    effect immediately — no restart required.
    """
    form = await request.form()
    updates: dict[str, str] = {}
    for key in EDITABLE_KEYS:
        if form.get(f"clear_{key}"):
            updates[key] = ""  # explicit removal
            continue
        value = (form.get(key) or "").strip()
        if value:  # blank = leave as-is (don't overwrite an existing secret)
            updates[key] = value

    if updates:
        update_env(updates)
        reload_settings()
        # Rebuild the collector so its sources pick up the new keys without a restart.
        request.app.state.collector = Collector(get_settings())

    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request, "_settings_left.html", {**_settings_context(settings), "saved": bool(updates)}
    )


@app.post("/settings/translation", response_class=HTMLResponse)
async def settings_set_translation(request: Request) -> HTMLResponse:
    """Configure the keyless English-translation endpoint from the dashboard.

    Writes LIBRETRANSLATE_URL to .env (it is a plain URL, not a secret, so it is
    shown back), reloads settings live, and rebuilds the collector so the next
    scan uses it. A blank submission clears it (disabling the HTTP backend). The
    URL is validated against the shared SSRF guard so a private/loopback address
    is rejected with a clear message rather than silently failing at scan time.
    """
    from nexus.netguard import safe_http_url

    form = await request.form()
    url = (form.get("libretranslate_url") or "").strip()

    msg = ""
    if url:
        # Validate the /translate endpoint the same way nexus.translate will call it.
        ok, reason = safe_http_url(url.rstrip("/") + "/translate")
        if not ok:
            # Reject without writing — surface the reason so the operator can fix it.
            settings = get_settings()
            ctx = {
                **_settings_context(settings),
                "translation_saved": False,
                "translation_msg": f"Not saved — that address was refused: {reason}",
            }
            return TEMPLATES.TemplateResponse(request, "_settings_left.html", ctx)

    update_env({"LIBRETRANSLATE_URL": url})
    reload_settings()
    request.app.state.collector = Collector(get_settings())

    settings = get_settings()
    ctx = {
        **_settings_context(settings),
        "translation_saved": True,
        "translation_msg": (
            "Saved — non-English items will now be rendered to English on the next scan."
            if url else "Cleared — the keyless HTTP translation backend is now off."
        ),
    }
    return TEMPLATES.TemplateResponse(request, "_settings_left.html", ctx)


# --------------------------------------------------------------- custom sources
# Self-service "add any JSON API as a source". The AI (when connected) fills in
# the config from docs/sample; otherwise the analyst fills the fields by hand.
# The API key (if any) goes to .env (CUSTOM_SOURCE_<id>_KEY) — never the DB.

_CUSTOM_FORM_FIELDS = (
    "name", "base_url", "endpoint", "http_method", "auth_type", "auth_param",
    "query_param", "extra_params", "items_path",
    "map_title", "map_content", "map_url", "map_author", "map_published", "notes",
)


def _custom_cfg_from_form(form) -> dict:
    return {field: (form.get(field) or "") for field in _CUSTOM_FORM_FIELDS}


def _custom_sources_context() -> dict:
    settings = get_settings()
    with get_connection() as conn:
        rows = list_custom_sources(conn)
    for row in rows:
        row["key_set"] = custom_source_key_configured(row["id"])
    return {
        "custom_sources": rows,
        "ai_enabled": settings.analysis_enabled,
        "active_provider": settings.active_provider(),
        "status": settings.availability_report(),
    }


@app.get("/sources")
def sources_home() -> Response:
    """Canonical entry for the unified 'where data comes from' area. The Feeds &
    presets tab is the default; Custom APIs is the second tab."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/topics", status_code=302)


@app.get("/sources/custom", response_class=HTMLResponse)
def custom_sources_page(request: Request) -> HTMLResponse:
    """List user-defined API sources and the add-a-source form."""
    return TEMPLATES.TemplateResponse(
        request, "custom_sources.html", _custom_sources_context()
    )


@app.post("/sources/custom/plan", response_class=HTMLResponse)
async def custom_source_plan(request: Request) -> HTMLResponse:
    """Ask the connected AI to draft a source config from docs/sample/hint.

    Returns the add-source form pre-filled with the AI's draft for review. If no
    AI is connected (or the reply is unusable), returns the form with an error
    banner so the analyst can fill it in manually.
    """
    from nexus.analysis.source_planner import plan_source

    form = await request.form()
    result = plan_source(
        docs_url=(form.get("docs_url") or "").strip() or None,
        sample=(form.get("sample") or "").strip() or None,
        hint=(form.get("hint") or "").strip() or None,
    )
    ctx = {"ai_enabled": get_settings().analysis_enabled}
    if result.get("ok"):
        ctx["cfg"] = result["config"]
        ctx["plan_ok"] = True
    else:
        ctx["error"] = result.get("error")
        ctx["cfg"] = {}
    return TEMPLATES.TemplateResponse(request, "_custom_source_form.html", ctx)


@app.post("/sources/custom", response_class=HTMLResponse)
async def custom_source_create(request: Request) -> HTMLResponse:
    """Persist a new custom source; write its API key (if given) to .env."""
    form = await request.form()
    cfg = _custom_cfg_from_form(form)
    with get_connection() as conn:
        new_id = create_custom_source(conn, cfg)
    api_key = (form.get("api_key") or "").strip()
    if api_key and cfg.get("auth_type") in ("header", "query", "bearer"):
        update_env({custom_source_env_name(new_id): api_key})
        reload_settings()
    return TEMPLATES.TemplateResponse(
        request, "_custom_source_list.html", _custom_sources_context()
    )


@app.post("/sources/custom/{source_id}/update", response_class=HTMLResponse)
async def custom_source_update(request: Request, source_id: int) -> HTMLResponse:
    """Update a custom source's config (and rotate its key if a new one is given)."""
    form = await request.form()
    cfg = _custom_cfg_from_form(form)
    with get_connection() as conn:
        update_custom_source(conn, source_id, cfg)
    api_key = (form.get("api_key") or "").strip()
    if api_key and cfg.get("auth_type") in ("header", "query", "bearer"):
        update_env({custom_source_env_name(source_id): api_key})
        reload_settings()
    return TEMPLATES.TemplateResponse(
        request, "_custom_source_list.html", _custom_sources_context()
    )


@app.post("/sources/custom/{source_id}/toggle", response_class=HTMLResponse)
def custom_source_toggle(request: Request, source_id: int) -> HTMLResponse:
    with get_connection() as conn:
        toggle_custom_source(conn, source_id)
    return TEMPLATES.TemplateResponse(
        request, "_custom_source_list.html", _custom_sources_context()
    )


@app.post("/sources/custom/{source_id}/delete", response_class=HTMLResponse)
def custom_source_delete(request: Request, source_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_custom_source(conn, source_id)
    # Clear its secret from .env so no orphan key lingers.
    update_env({custom_source_env_name(source_id): ""})
    return TEMPLATES.TemplateResponse(
        request, "_custom_source_list.html", _custom_sources_context()
    )


@app.post("/sources/custom/{source_id}/test", response_class=HTMLResponse)
def custom_source_test(request: Request, source_id: int) -> HTMLResponse:
    """Do a live fetch against one source and report the result in plain words.

    Runs in a worker thread (the source uses blocking httpx). Never raises — a
    failure is reported as a red status, matching graceful degradation.
    """
    from nexus.sources.custom import CustomApiSource

    with get_connection() as conn:
        row = get_custom_source(conn, source_id)
    if not row:
        return HTMLResponse('<span class="text-rose-400">Source not found.</span>')

    result: dict = {"name": row.get("name")}
    try:
        src = CustomApiSource(row, get_settings())
        if not src.is_available():
            result["error"] = (
                "Not ready: check the base URL and (if the API needs a key) that "
                "the key is set."
            )
        else:
            items = src.fetch()
            result["count"] = len(items)
            if items:
                first = items[0]
                result["sample_title"] = first.title or (first.content or "")[:120]
                result["sample_url"] = first.url
    except Exception as exc:  # pragma: no cover - defensive
        result["error"] = str(exc)
    return TEMPLATES.TemplateResponse(
        request, "_custom_source_test.html", {"result": result}
    )
