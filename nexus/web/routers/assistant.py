"""Local AI assistant endpoints."""

from __future__ import annotations

import logging
from datetime import UTC

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from nexus.config import get_settings
from nexus.db import get_connection

logger = logging.getLogger("nexus")
router = APIRouter()


# --- In-dashboard analyst assistant -----------------------------------------
@router.post("/assistant/ask")
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


@router.post("/assistant/save-log")
def assistant_save_log(transcript: list = Body(default=[], embed=True)) -> JSONResponse:
    """Save a chat transcript to a LOCAL log file — opt-in, from "End chat".

    Local-only by design: it writes under the app's own data directory and never
    leaves this machine. Only the role/content text is stored (no system prompt,
    no secrets) so the analyst keeps a private record of what they asked.
    """
    import json as _json
    from datetime import datetime

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
        now = datetime.now(UTC)
        path = log_dir / f"sherlock_{now.strftime('%Y%m%d')}.jsonl"
        record = {"saved_at": now.isoformat(timespec="seconds"), "turns": turns}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Assistant: failed to save chat log")
        return JSONResponse({"ok": False, "saved": 0})
    return JSONResponse({"ok": True, "saved": len(turns)})
