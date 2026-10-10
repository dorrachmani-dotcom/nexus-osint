"""Entity co-occurrence graph and entity aliases."""

from __future__ import annotations

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse

from nexus.db import get_connection
from nexus.graph import render_entity_graph_html
from nexus.storage import (
    add_entity_alias,
    case_terms,
    delete_entity_alias,
    distinct_sources,
    get_case,
    get_entity_aliases,
    list_cases,
    topic_entity_graph,
)
from nexus.web.common import TEMPLATES, THREAT_LEVELS, _filters, _window_since_ts

router = APIRouter()


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


@router.get("/graph", response_class=HTMLResponse)
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


@router.get("/graph/build", response_class=HTMLResponse)
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


@router.post("/graph/alias", response_class=HTMLResponse)
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


@router.post("/graph/alias/{alias_id}/delete", response_class=HTMLResponse)
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
