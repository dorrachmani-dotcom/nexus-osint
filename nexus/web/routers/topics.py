"""Topics, subscriptions, presets, onboarding bundles and search capsules."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.storage import (
    add_requirement,
    add_subscription,
    count_items,
    delete_capsule,
    delete_subscription,
    list_query_capsules,
    list_subscriptions,
)
from nexus.web.common import TEMPLATES, TOPIC_PRESETS, _setup_state, _split_terms

router = APIRouter()


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


@router.get("/topics", response_class=HTMLResponse)
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
            "today_date": datetime.now(UTC).strftime("%Y-%m-%d"),
        },
    )


def _capsules_response(request: Request) -> HTMLResponse:
    with get_connection() as conn:
        capsules = list_query_capsules(conn)
    return TEMPLATES.TemplateResponse(
        request, "_capsules.html", {"capsules": capsules}
    )


@router.post("/topics/capsule", response_class=HTMLResponse)
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


@router.post("/topics/capsule/generate", response_class=HTMLResponse)
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


@router.post("/topics/capsule/term", response_class=HTMLResponse)
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


@router.post("/topics/capsule/term/{sub_id}/delete", response_class=HTMLResponse)
def topics_capsule_delete_term(request: Request, sub_id: int) -> HTMLResponse:
    """Remove one term from a capsule."""
    with get_connection() as conn:
        delete_subscription(conn, sub_id)
    return _capsules_response(request)


@router.post("/topics/capsule/delete", response_class=HTMLResponse)
def topics_capsule_delete(request: Request, name: str = Form(...)) -> HTMLResponse:
    """Delete a whole capsule and all of its terms."""
    with get_connection() as conn:
        delete_capsule(conn, name)
    return _capsules_response(request)


@router.post("/topics/preset", response_class=HTMLResponse)
def topics_add_preset(request: Request, preset: str = Form(...)) -> HTMLResponse:
    """Subscribe to every feed in a curated preset (idempotent)."""
    with get_connection() as conn:
        for value, label in TOPIC_PRESETS.get(preset, []):
            add_subscription(conn, "rss", value, label)
        grouped = _grouped_subscriptions(conn)
    return TEMPLATES.TemplateResponse(
        request, "_subscriptions.html", {"grouped": grouped, "sources": TOPIC_SOURCES}
    )


@router.post("/onboard/bundle", response_class=HTMLResponse)
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


@router.post("/topics/add", response_class=HTMLResponse)
def topics_add(
    request: Request,
    source: str = Form(...),
    value: str = Form(...),
    label: str = Form(default=""),
) -> HTMLResponse:
    """Add a single custom collection target."""
    value = (value or "").strip()
    clean_label = (label or "").strip() or None
    if source in _TOPIC_SOURCE_IDS and value:
        with get_connection() as conn:
            add_subscription(conn, source, value, clean_label)
    with get_connection() as conn:
        grouped = _grouped_subscriptions(conn)
    return TEMPLATES.TemplateResponse(
        request, "_subscriptions.html", {"grouped": grouped, "sources": TOPIC_SOURCES}
    )


@router.post("/topics/{sub_id}/delete", response_class=HTMLResponse)
def topics_delete(request: Request, sub_id: int) -> HTMLResponse:
    """Remove a collection target."""
    with get_connection() as conn:
        delete_subscription(conn, sub_id)
        grouped = _grouped_subscriptions(conn)
    return TEMPLATES.TemplateResponse(
        request, "_subscriptions.html", {"grouped": grouped, "sources": TOPIC_SOURCES}
    )
