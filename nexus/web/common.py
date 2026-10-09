"""Shared web-layer helpers: the Jinja2 environment and its filters, feed
filtering/paging, card and feed-partial renderers, and small context builders
used by more than one router.
"""

from __future__ import annotations

import re
from datetime import UTC
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from nexus import wayback
from nexus.db import get_connection
from nexus.storage import (
    all_case_terms,
    case_archive_summary,
    count_items,
    count_matching_items,
    enrich_feed_rows,
    feed_item,
    get_active_case,
    get_case,
    get_item_archive,
    list_cases,
    list_lists,
    list_query_capsules,
    search_items,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
TEMPLATES = Jinja2Templates(directory=str(TEMPLATES_DIR))


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


def _linkify(value: str) -> Markup:
    """Escape text, then turn bare http/https URLs into clickable links."""
    safe = str(escape(value))
    safe = re.sub(
        r"(https?://[^\s&quot;&lt;&gt;\"']+)",
        r'<a href="\1" target="_blank" rel="noopener" '
        r'class="text-sky-400 hover:text-sky-300 break-all">\1</a>',
        safe,
    )
    # Safe: the input was HTML-escaped above; only the anchor markup is added.
    return Markup(safe)  # noqa: S704


TEMPLATES.env.filters["linkify"] = _linkify


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
    from datetime import datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
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


# --- Internet Archive (Wayback Machine) -------------------------------------
# Captures run on nexus.wayback's background queue (SPN can take 10-60 s); the
# drawer / case page poll these fragments like the scan-status chip. The first
# use shows a one-time OpSec warning (archiving is public and tells the Internet
# Archive which URL you care about); acknowledging it is stored in DB meta.
def _fmt_archive_time(value: str | None) -> str:
    v = (value or "").replace("T", " ").rstrip("Z")
    return v[:16]


def _item_archive_ctx(conn, item_id: int, url: str, **extra) -> dict:
    ctx = {
        "item_id": item_id,
        "item_url": url or "",
        "arch": get_item_archive(conn, item_id),
        "enabled": wayback.is_enabled(conn),
        "acked": wayback.opsec_acknowledged(conn),
        "message": "",
        "message_ok": False,
        "warn_action": "",
    }
    ctx.update(extra)
    return ctx


def _case_archive_ctx(conn, case_id: int, **extra) -> dict:
    ctx = {
        "case_id": case_id,
        "summary": case_archive_summary(conn, case_id),
        "auto": bool((get_case(conn, case_id) or {}).get("auto_archive")),
        "enabled": wayback.is_enabled(conn),
        "acked": wayback.opsec_acknowledged(conn),
        "message": "",
        "warn_action": "",
    }
    ctx.update(extra)
    return ctx


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


# Hard cap on a single feed export so a huge history can never produce a
# multi-hundred-MB PDF that the renderer chokes on. Newest items win.
_MAX_EXPORT_ITEMS = 2000


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


def _split_terms(raw: str) -> list[str]:
    """Split a free-text term list on commas / newlines into clean terms."""
    out: list[str] = []
    for chunk in (raw or "").replace("\n", ",").split(","):
        term = chunk.strip()
        if term and term not in out:
            out.append(term)
    return out
