"""Background scan tracker.

Runs a collection scan on a daemon thread so the UI never blocks for the minutes
a scan can take (collection across sources + AI analysis of the backlog). The web
layer kicks one off and then polls a small, thread-safe status snapshot — exactly
the pattern used for the Ollama model pull. Single-flight: one manual scan at a
time. Fail-soft: an error is captured into the snapshot, never raised into a
request.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("nexus.scanstate")

_lock = threading.Lock()
_state: dict = {
    "status": "idle",   # idle | running | done | error
    "started": 0.0,
    "finished": 0.0,
    "new_items": 0,
    "catchup_new": 0,  # added by the start-up gap-fill this scan waited for
    "error": "",
}


def state() -> dict:
    """Thread-safe snapshot of the current/last scan, plus an elapsed seconds."""
    with _lock:
        snap = dict(_state)
    if snap["status"] == "running" and snap["started"]:
        snap["elapsed"] = int(time.time() - snap["started"])
    else:
        snap["elapsed"] = 0
    return snap


def start(collector) -> dict:
    """Begin a background scan if one isn't already running. Returns the snapshot."""
    with _lock:
        if _state["status"] == "running":
            return state()
        _state.update({
            "status": "running", "started": time.time(),
            "finished": 0.0, "new_items": 0, "catchup_new": 0, "error": "",
        })
    threading.Thread(target=_run, args=(collector,), daemon=True, name="manual-scan").start()
    return state()


def _run(collector) -> None:
    new = 0
    requested = time.time()
    try:
        stats = collector.scan()
        try:
            new = int((stats or {}).get("_update", {}).get("new") or 0)
        except (TypeError, ValueError):
            new = 0
        with _lock:
            boot = getattr(collector, "last_boot_sync", None) or {}
            catchup = int(boot.get("new") or 0) if float(boot.get("finished") or 0) >= requested else 0
            _state.update({"status": "done", "finished": time.time(), "new_items": new,
                           "catchup_new": catchup})
    except Exception:
        logger.exception("Background scan failed")
        with _lock:
            _state.update({
                "status": "error", "finished": time.time(),
                "error": "The scan hit a problem. Please try again.",
            })
