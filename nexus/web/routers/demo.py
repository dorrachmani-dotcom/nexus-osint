"""One-click demo data loader."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from nexus.db import get_connection
from nexus.web.common import _feed_partial_response

router = APIRouter()


@router.post("/demo/load", response_class=HTMLResponse)
def demo_load(request: Request) -> HTMLResponse:
    """Load the one-click demo dataset (fictional sample), then show the populated
    feed so a fresh install demonstrates its features before sources are set up."""
    from nexus.demodata import load_demo_data

    with get_connection() as conn:
        load_demo_data(conn)
    return _feed_partial_response(request, scope="all")
