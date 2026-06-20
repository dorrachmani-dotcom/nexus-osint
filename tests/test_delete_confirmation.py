"""Guard test: every destructive delete in the UI asks for confirmation.

A non-technical operator must never lose a case, source, watchlist, topic, note
or requirement to a single mis-click. Every htmx control that posts to a
``/delete`` endpoint must therefore carry an ``hx-confirm`` so the browser shows
a "are you sure?" dialog first. This test scans the templates and fails if any
new delete button ships without one — the protection cannot silently regress.

Reversible curation actions (removing an item from a list/case via ``/remove``)
are intentionally NOT covered: they add no friction-worthy risk and re-adding is
trivial.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "nexus" / "web" / "templates"

# Match a whole <button ...> or <form ...> opening tag, across newlines, that
# posts to a path ending in "/delete".
_TAG_RE = re.compile(
    r"<(?:button|form)\b[^>]*?hx-post=\"[^\"]*/delete\"[^>]*?>",
    re.DOTALL,
)


def _delete_tags() -> list[tuple[str, str]]:
    """Return (template_name, opening_tag) for every delete control found."""
    found: list[tuple[str, str]] = []
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in _TAG_RE.finditer(text):
            found.append((path.name, match.group(0)))
    return found


def test_some_delete_controls_exist():
    # Sanity: the scanner actually finds the delete buttons (so a regex that
    # silently matches nothing can't make the guard below vacuously pass).
    assert _delete_tags(), "expected to find delete controls in the templates"


@pytest.mark.parametrize("name,tag", _delete_tags(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_delete_control_has_confirmation(name, tag):
    assert "hx-confirm=" in tag, (
        f"{name}: a control posting to a /delete endpoint is missing hx-confirm "
        f"(a destructive action must ask for confirmation):\n{tag}"
    )
