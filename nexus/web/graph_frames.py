"""Serve rendered relationship graphs as their own sandboxed documents.

The graph library (pyvis / vis-network) emits a self-contained HTML page with
inline scripts. An ``srcdoc`` iframe inherits the app's Content-Security-
Policy, which allows no inline script, so instead each rendered graph is held
briefly in memory under a random token and served from
``/graph/frame/{token}`` with its own, narrow policy: inline script and style
only, no network access at all, and embeddable only by this app. The iframe
keeps ``sandbox="allow-scripts"`` (no same-origin), so the graph runs in an
opaque origin with no access to the app, its cookies or its storage.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict

_TTL = 30 * 60
_MAX = 64
_lock = threading.Lock()
_frames: OrderedDict[str, tuple[float, str]] = OrderedDict()

FRAME_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'self'"
)


def store(html: str | None) -> str:
    """Keep ``html`` and return the URL that serves it ("" for no graph)."""
    if not html:
        return ""
    token = secrets.token_urlsafe(18)
    now = time.monotonic()
    with _lock:
        for key in [k for k, (ts, _) in _frames.items() if now - ts > _TTL]:
            del _frames[key]
        _frames[token] = (now, html)
        while len(_frames) > _MAX:
            _frames.popitem(last=False)
    return f"/graph/frame/{token}"


def get(token: str) -> str | None:
    with _lock:
        entry = _frames.get(token)
    if entry is None or time.monotonic() - entry[0] > _TTL:
        return None
    return entry[1]
