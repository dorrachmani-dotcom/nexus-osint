"""FastAPI application entry point.

Creates the app and wires the cross-cutting pieces: logging (with secret
redaction), the background daemons (auto-scan, daily jobs) and Boot Sync in the
lifespan, web hardening and the egress monitor, the friendly last-resort error
page, the static/data mounts, and the domain routers in ``nexus/web/routers/``.

The routes themselves live in the routers; shared web helpers (templates,
filters, feed paging, card renderers) live in ``nexus/web/common.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from nexus import __version__
from nexus.collector import Collector
from nexus.config import get_settings
from nexus.db import get_connection, init_db
from nexus.storage import fail_stale_archive_jobs, get_meta, set_meta
from nexus.web.common import (
    FEED_PAGE_SIZE,
    TEMPLATES,
    _paged_feed,
    _safe_url,
    _topic_terms,
)
from nexus.web.routers import (
    archive,
    assistant,
    brief,
    case_reports,
    cases,
    demo,
    entity,
    feed,
    graph,
    intel,
    items,
    lists,
    llm,
    notifications,
    scan,
    security,
    settings,
    sources,
    system,
    tools,
    topics,
    transfer,
    watchlists,
)

# Names other modules and tests import from here (kept stable across the split).
__all__ = [
    "FEED_PAGE_SIZE",
    "TEMPLATES",
    "_paged_feed",
    "_safe_url",
    "_topic_terms",
    "app",
    "lifespan",
]

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


def _auto_scan_loop(collector: Collector) -> None:
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
                        if datetime.now(UTC) - last_dt < timedelta(hours=interval_h):
                            continue
                    except ValueError:
                        pass
                set_meta(conn, "last_auto_scan_at", datetime.now(UTC).isoformat())
            logger.info("Auto-scan: starting scheduled scan (interval=%dh)", interval_h)
            collector.scan()
            logger.info("Auto-scan: completed")
        except Exception:
            logger.exception("Auto-scan: failed")


def _daily_jobs_loop(app_ref: FastAPI) -> None:
    """Daemon thread (sibling of auto-scan): once a minute, check whether the
    daily case reports and the daily email brief are due, and run them.

    Reads every setting live from the DB/.env so changes apply without a
    restart. Never raises; a failure is logged and retried on a later tick.
    """
    from nexus.digest import run_daily_jobs

    while True:
        time.sleep(60)
        try:
            run_daily_jobs(get_settings(), TEMPLATES, getattr(app_ref.state, "collector", None))
        except Exception:
            logger.exception("Daily jobs: failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Re-run after uvicorn has installed its own log handlers, so the secret
    # redaction filter covers those too.
    install_log_redaction()
    init_db()
    # Archive jobs queued before a restart were lost with the in-memory queue.
    try:
        with get_connection() as conn:
            fail_stale_archive_jobs(conn)
    except Exception:
        logger.exception("Could not reset interrupted archive jobs")
    app.state.collector = Collector(settings)
    report = settings.availability_report()
    logger.info("Nexus-OSINT v%s ready. Sources: %s", __version__, report)

    # Auto-scan background daemon — checks every 60 s if a scan is due.
    t = threading.Thread(
        target=_auto_scan_loop, args=(app.state.collector,), daemon=True, name="auto-scan"
    )
    t.start()

    # Daily jobs daemon — scheduled case reports + the daily email brief.
    threading.Thread(
        target=_daily_jobs_loop, args=(app,), daemon=True, name="daily-jobs"
    ).start()

    # Boot Sync: silent gap-fill, off the event loop so startup stays fast.
    if app.state.collector.available_sources():
        # Keep a reference so the task cannot be garbage-collected mid-run.
        app.state.boot_sync_task = asyncio.create_task(
            asyncio.to_thread(app.state.collector.boot_sync)
        )
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


# Bundled UI assets (compiled stylesheet, vendored htmx) — no CDN at runtime.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Serve stored evidence screenshots (and any other data assets) read-only.
_DATA_DIR = Path(get_settings().data_dir)
_DATA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/data", StaticFiles(directory=str(_DATA_DIR)), name="data")


# Domain routers. Starlette matches routes first-come-first-served, so this
# order is load-bearing: it keeps every pair of overlapping URL patterns in the
# order the original single-module app registered them (e.g. the archive
# routes under /cases/{case_id}/... before POST /cases/quick-add/{item_id},
# which in turn precedes the case report/export routes).
# tests/test_route_order.py guards the full route table.
ROUTERS = (
    system,
    feed,
    scan,
    demo,
    tools,
    graph,
    entity,
    items,
    archive,
    settings,
    lists,
    cases,
    brief,
    case_reports,
    transfer,
    security,
    assistant,
    watchlists,
    topics,
    intel,
    llm,
    notifications,
    sources,
)
for _module in ROUTERS:
    app.include_router(_module.router)
