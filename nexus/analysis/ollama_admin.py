"""In-app management of the local Ollama model — no terminal required.

The whole point of the local backend is privacy for non-technical analysts, so
the one step that used to need a terminal (``ollama pull <model>``) is done here
instead, with a live progress bar. Ollama exposes a local REST endpoint
(``POST /api/pull``) that streams download progress as NDJSON; we run it on a
background thread and expose a small, thread-safe snapshot the Settings page
polls. Everything talks only to the local Ollama server — nothing leaves the
machine, no key, no account.

Graceful by design: a download for an unknown model, or a server that isn't
running, leaves the tracker in an ``error`` state with a plain-English message
and never raises into the request path.
"""

from __future__ import annotations

import json
import logging
import threading

logger = logging.getLogger("nexus.analysis")

# A small, curated menu so analysts pick from a dropdown instead of typing model
# names. Ordered lightest-first; labels are plain-English with a hardware hint.
RECOMMENDED_MODELS: list[dict[str, str]] = [
    {
        "name": "gemma4:e2b",
        "label": "Gemma 4 (E2B) — lightest, for older laptops",
        "size": "~2 GB download",
        "ram": "8 GB RAM",
    },
    {
        "name": "gemma4:e4b",
        "label": "Gemma 4 (E4B) — recommended, best all-round balance",
        "size": "~4 GB download",
        "ram": "16 GB RAM",
    },
    {
        "name": "gemma4:12b",
        "label": "Gemma 4 (12B) — sharper analysis, needs a strong PC",
        "size": "~9 GB download",
        "ram": "32 GB RAM",
    },
    {
        "name": "gemma4:31b",
        "label": "Gemma 4 (31B) — closest to cloud quality, fully offline",
        "size": "~22 GB download",
        "ram": "64 GB RAM or 24+ GB GPU",
    },
]

# Single in-flight pull at a time (this is a local, single-user app). Guarded by
# a lock so the background thread and request handlers never race.
_lock = threading.Lock()
_state: dict = {
    "status": "idle",  # idle | running | done | error
    "model": "",
    "percent": 0,
    "detail": "",
    "error": "",
}


def pull_state() -> dict:
    """A thread-safe snapshot of the current/last download for the UI."""
    with _lock:
        return dict(_state)


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def start_pull(base_url: str, model: str) -> dict:
    """Begin downloading ``model`` from the local Ollama server, if not already
    running. Returns the current state snapshot immediately (the download runs
    on a daemon thread; the UI polls :func:`pull_state`)."""
    model = (model or "").strip()
    with _lock:
        if _state["status"] == "running":
            return dict(_state)  # one at a time; report the in-flight one
        _state.update(
            {
                "status": "running",
                "model": model,
                "percent": 0,
                "detail": "Starting download…",
                "error": "",
            }
        )
    thread = threading.Thread(
        target=_run_pull, args=(base_url, model), daemon=True
    )
    thread.start()
    return pull_state()


def _run_pull(base_url: str, model: str) -> None:
    """Stream ``POST /api/pull`` and translate its NDJSON progress into the
    tracker. Runs on a background thread; never raises."""
    try:
        import httpx

        url = f"{base_url.rstrip('/')}/api/pull"
        # No read timeout: a large model can take many minutes on a slow link.
        with httpx.stream(
            "POST",
            url,
            json={"name": model, "stream": True},
            timeout=httpx.Timeout(10.0, read=None),
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("error"):
                    _set(status="error", error=str(msg["error"]))
                    return
                status = msg.get("status", "") or ""
                total = msg.get("total")
                completed = msg.get("completed")
                update = {"detail": status}
                if total:
                    update["percent"] = max(
                        0, min(100, int((completed or 0) / total * 100))
                    )
                _set(**update)
                if status.lower() == "success":
                    _set(status="done", percent=100, detail="Download complete")
                    return
        # Stream finished cleanly without an explicit "success" line.
        _set(status="done", percent=100, detail="Download complete")
    except Exception:
        logger.exception("Ollama model pull failed for %r", model)
        _set(
            status="error",
            error=(
                "Couldn't download the model. Make sure the Ollama app is "
                "installed and running, then try again."
            ),
        )
