"""Case hub: case CRUD, detail tabs, terms, questions, sub-cases, notes and
pinned items.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from nexus import wayback
from nexus.collector import Collector
from nexus.config import get_settings
from nexus.db import get_connection
from nexus.storage import (
    add_bookmark,
    add_case_term,
    add_note,
    add_requirement,
    case_items,
    case_live_items,
    case_new_count,
    case_notes,
    case_question_items,
    case_reviewed_items,
    case_terms,
    case_terms_map,
    case_timeline,
    case_unread_count,
    create_case,
    delete_case,
    delete_note,
    delete_requirement,
    enrich_feed_rows,
    get_active_case,
    get_case,
    list_cases,
    list_lists,
    list_requirements,
    mark_case_read,
    related_cases,
    remove_bookmark,
    remove_case_term,
    set_active_case,
    touch_case_visit,
    update_case,
    update_case_term,
    update_note,
)
from nexus.web.common import TEMPLATES, _case_archive_ctx, _render_card, _split_terms

logger = logging.getLogger("nexus")
router = APIRouter()


@router.get("/cases", response_class=HTMLResponse)
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


@router.post("/cases", response_class=HTMLResponse)
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


@router.post("/cases/active", response_class=HTMLResponse)
def cases_set_active(request: Request, case_id: int = Form(...)) -> HTMLResponse:
    """Set (or clear, with case_id=0) the analyst's current working case."""
    with get_connection() as conn:
        set_active_case(conn, case_id if case_id else None)
        rows = list_cases(conn, parent_id=None)
        active = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request, "_case_list.html", {"cases": rows, "active_case": active}
    )


@router.post("/cases/{case_id}/update", response_class=HTMLResponse)
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


@router.post("/cases/{case_id}/close")
def cases_close(case_id: int):
    """Archive (close) a case and return to its detail page."""
    from fastapi.responses import RedirectResponse
    with get_connection() as conn:
        update_case(conn, case_id, status="closed")
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id}/reopen")
def cases_reopen(case_id: int):
    """Reopen a closed case and return to its detail page."""
    from fastapi.responses import RedirectResponse
    with get_connection() as conn:
        update_case(conn, case_id, status="open")
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id}/delete", response_class=HTMLResponse)
def cases_delete(request: Request, case_id: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_case(conn, case_id)
        rows = list_cases(conn, parent_id=None)
        active = get_active_case(conn)
    return TEMPLATES.TemplateResponse(
        request, "_case_list.html", {"cases": rows, "active_case": active}
    )


_CASE_TABS = {"feed", "pinned", "questions", "subcases", "timeline", "graph", "reports"}


@router.get("/cases/{case_id}", response_class=HTMLResponse)
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
        case_archive_ctx = _case_archive_ctx(conn, case_id)
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
            "ca": case_archive_ctx,
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


@router.post("/cases/{case_id}/activate")
def case_activate(case_id: int) -> Response:
    """Make this the active working case (so 'this'/'here' in Sherlock resolve to
    it and pins default here), then return to the case page."""
    from fastapi.responses import RedirectResponse

    with get_connection() as conn:
        set_active_case(conn, case_id if get_case(conn, case_id) else None)
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id}/deactivate")
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


@router.post("/cases/{case_id}/brief", response_class=HTMLResponse)
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
            # Pass the list (not a pre-dumped string) so the template can use
            # |tojson, which escapes </script> etc. for safe <script> embedding.
            "count": len(items), "items_json": items_data,
        },
    )


@router.post("/cases/{case_id}/scan", response_class=HTMLResponse)
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
    today = date.today()  # noqa: DTZ011 (the analyst's local calendar day is intended)
    if preset == "today":
        return today.isoformat()
    if preset == "week":
        return (today - timedelta(days=7)).isoformat()
    if preset == "month":
        return (today - timedelta(days=30)).isoformat()
    if len(preset) == 10 and preset[4] == "-":
        return preset  # already a YYYY-MM-DD literal
    return None


@router.get("/cases/{case_id}/feed", response_class=HTMLResponse)
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


@router.get("/cases/{case_id}/reviewed", response_class=HTMLResponse)
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


@router.post("/cases/{case_id}/read-all", response_class=HTMLResponse)
def case_read_all(request: Request, case_id: int) -> HTMLResponse:
    """Mark all of this case's tracked items as read, then redraw the feed tab."""
    with get_connection() as conn:
        mark_case_read(conn, case_id)
        return _render_case_feed_tab(request, conn, case_id)


@router.post("/cases/{case_id}/terms", response_class=HTMLResponse)
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


@router.post("/cases/{case_id}/terms/{term_id}/delete", response_class=HTMLResponse)
def case_delete_term(request: Request, case_id: int, term_id: int) -> HTMLResponse:
    with get_connection() as conn:
        remove_case_term(conn, case_id, term_id)
        return _render_case_feed_tab(request, conn, case_id)


@router.post("/cases/{case_id}/terms/{term_id}/edit", response_class=HTMLResponse)
def case_edit_term(
    request: Request, case_id: int, term_id: int, term: str = Form(...)
) -> HTMLResponse:
    """Edit a tracking word in place."""
    with get_connection() as conn:
        update_case_term(conn, case_id, term_id, term)
        return _render_case_feed_tab(request, conn, case_id)


@router.post("/cases/{case_id}/terms/translate", response_class=HTMLResponse)
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


@router.post("/cases/{case_id}/terms/generate", response_class=HTMLResponse)
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


@router.post("/cases/{case_id}/questions", response_class=HTMLResponse)
def case_add_question(
    request: Request, case_id: int, question: str = Form(...)
) -> HTMLResponse:
    question = (question or "").strip()
    with get_connection() as conn:
        if question:
            add_requirement(conn, question, priority=1, case_id=case_id)
        return _render_case_questions(request, conn, case_id)


@router.post("/cases/{case_id}/questions/{rid}/delete", response_class=HTMLResponse)
def case_delete_question(request: Request, case_id: int, rid: int) -> HTMLResponse:
    with get_connection() as conn:
        delete_requirement(conn, rid)
        return _render_case_questions(request, conn, case_id)


@router.post("/cases/{case_id}/questions/generate", response_class=HTMLResponse)
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


@router.post("/cases/{case_id}/subcases", response_class=HTMLResponse)
def case_add_subcase(
    request: Request, case_id: int, name: str = Form(...)
) -> HTMLResponse:
    name = (name or "").strip()
    with get_connection() as conn:
        if name:
            create_case(conn, name, parent_id=case_id)
        return _render_case_subcases(request, conn, case_id)


@router.post("/cases/{case_id}/subcases/{sub_id}/terms", response_class=HTMLResponse)
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


@router.post("/cases/{case_id}/subcases/{sub_id}/terms/{term_id}/delete", response_class=HTMLResponse)
def subcase_delete_term(
    request: Request, case_id: int, sub_id: int, term_id: int,
) -> HTMLResponse:
    """Remove a tracking word from a sub-case; re-renders the parent's sub-cases tab."""
    with get_connection() as conn:
        conn.execute("DELETE FROM case_terms WHERE id = ? AND case_id = ?", (term_id, sub_id))
        return _render_case_subcases(request, conn, case_id)


@router.get("/cases/{case_id}/timeline", response_class=HTMLResponse)
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


@router.post("/cases/{case_id}/notes", response_class=HTMLResponse)
def case_add_note(request: Request, case_id: int, body: str = Form(...)) -> HTMLResponse:
    body = (body or "").strip()
    with get_connection() as conn:
        if body:
            add_note(conn, body, case_id=case_id)
        notes = case_notes(conn, case_id)
    return TEMPLATES.TemplateResponse(
        request, "_notes.html", {"notes": notes, "case": {"id": case_id}}
    )


@router.post("/notes/{note_id}/update", response_class=HTMLResponse)
def note_update(request: Request, note_id: int, body: str = Form(...)) -> HTMLResponse:
    with get_connection() as conn:
        case_id = update_note(conn, note_id, body)
        notes = case_notes(conn, case_id) if case_id else []
    return TEMPLATES.TemplateResponse(
        request, "_notes.html", {"notes": notes, "case": {"id": case_id}}
    )


@router.post("/notes/{note_id}/delete", response_class=HTMLResponse)
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


@router.post("/cases/{case_id}/pin-all", response_class=HTMLResponse)
def case_pin_all(case_id: int, item_ids: list[int] = Form(default=[])) -> HTMLResponse:
    """Pin a batch of items into a case (used by the Brief 'Pin all' button)."""
    with get_connection() as conn:
        for iid in item_ids:
            add_bookmark(conn, iid, case_id)
            wayback.auto_archive_on_pin(conn, iid, case_id)
    return HTMLResponse(
        '<span class="text-emerald-400 text-xs mono">&#10003; All pinned</span>'
    )


@router.post("/cases/{case_id}/items", response_class=HTMLResponse)
def case_add_item(request: Request, case_id: int, item_id: int = Form(...)) -> HTMLResponse:
    with get_connection() as conn:
        add_bookmark(conn, item_id, case_id)
        wayback.auto_archive_on_pin(conn, item_id, case_id)
        return _render_case_items(request, conn, case_id)


@router.post("/cases/{case_id}/items/{item_id}/add", response_class=HTMLResponse)
def case_add_item_card(
    request: Request, case_id: int, item_id: int,
    context_case_id: int | None = Form(default=None),
) -> HTMLResponse:
    """Add an item to a case from a feed card's menu; returns the refreshed card."""
    with get_connection() as conn:
        add_bookmark(conn, item_id, case_id)
        wayback.auto_archive_on_pin(conn, item_id, case_id)
        return _render_card(request, conn, item_id, context_case_id)


@router.post("/cases/{case_id}/items/{item_id}/remove", response_class=HTMLResponse)
def case_remove_item_card(
    request: Request, case_id: int, item_id: int,
    context_case_id: int | None = Form(default=None),
) -> HTMLResponse:
    """Remove from a feed card's menu; returns the refreshed card."""
    with get_connection() as conn:
        remove_bookmark(conn, item_id, case_id)
        return _render_card(request, conn, item_id, context_case_id)


@router.post("/cases/{case_id}/items/{item_id}/unpin", response_class=HTMLResponse)
def case_unpin_item(request: Request, case_id: int, item_id: int) -> HTMLResponse:
    """Remove from the case-detail page; returns the refreshed item list."""
    with get_connection() as conn:
        remove_bookmark(conn, item_id, case_id)
        return _render_case_items(request, conn, case_id)


@router.post("/cases/quick-add/{item_id}", response_class=HTMLResponse)
def case_quick_add(
    request: Request, item_id: int, name: str = Form(...),
    context_case_id: int | None = Form(default=None),
) -> HTMLResponse:
    """Create a case from the card's inline form and pin the item into it."""
    with get_connection() as conn:
        new_id = create_case(conn, name.strip() or "Untitled case")
        add_bookmark(conn, item_id, new_id)
        return _render_card(request, conn, item_id, context_case_id)
