"""OSINT tool adapters page and runner."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from nexus.adapters.registry import all_adapters, get_adapter
from nexus.config import get_settings
from nexus.graph import render_graph_html
from nexus.web.common import TEMPLATES

router = APIRouter()


# --- Plug & Play OSINT tools + entity graph ---------------------------------
def _adapter_cards(settings) -> list[dict]:
    return [
        {"name": a.name, "binary": a.binary, "available": a.is_available()}
        for a in all_adapters(settings)
    ]


@router.get("/tools", response_class=HTMLResponse)
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


@router.post("/tools/run", response_class=HTMLResponse)
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
