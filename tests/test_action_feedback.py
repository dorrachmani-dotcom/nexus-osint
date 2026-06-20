"""Guard test: failed actions are never silent.

On success htmx swaps the result into the page, so success is self-evident. The
dangerous case for a non-technical operator is a FAILURE — a refused request, a
server error, or a stopped server leaves the page unchanged with no sign the
click did nothing. base.html wires a toast to htmx's own error events so those
are surfaced. This locks that feedback in so it can't be dropped unnoticed.
"""

from __future__ import annotations

from pathlib import Path

BASE = (
    Path(__file__).resolve().parent.parent
    / "nexus" / "web" / "templates" / "base.html"
)


def test_base_template_has_failure_feedback():
    html = BASE.read_text(encoding="utf-8")
    assert 'id="toast-host"' in html, "the toast container is missing"
    # Both htmx error channels must be handled: an error response and no answer.
    assert "htmx:responseError" in html, "no handler for server/refused errors"
    assert "htmx:sendError" in html, "no handler for unreachable-server errors"
