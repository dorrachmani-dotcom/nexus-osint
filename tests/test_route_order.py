"""Route-table regression guard for the ``nexus.web`` router split.

``EXPECTED_ROUTES`` is the full (methods, path) list the monolithic
``nexus/web/app.py`` registered, in registration order, captured from
``origin/main`` before routes moved into ``nexus/web/routers/``. Starlette
matches routes first-come-first-served, so beyond "same set of routes" we also
assert that every pair of routes that can match the same URL keeps its
original relative order (e.g. ``/cases/active`` before ``/cases/{case_id}``).
"""

from __future__ import annotations

import re

import pytest

EXPECTED_ROUTES: list[tuple[str, str]] = [
    ('GET,HEAD', '/openapi.json'),
    ('GET,HEAD', '/docs'),
    ('GET,HEAD', '/docs/oauth2-redirect'),
    ('GET,HEAD', '/redoc'),
    ('Mount', '/static'),
    ('Mount', '/data'),
    ('GET', '/health'),
    ('GET', '/favicon.ico'),
    ('GET', '/api/status'),
    ('GET', '/'),
    ('GET', '/feed'),
    ('GET', '/feed/page'),
    ('POST', '/scan'),
    ('POST', '/scan/start'),
    ('GET', '/scan/status'),
    ('POST', '/demo/load'),
    ('GET', '/investigation'),
    ('POST', '/investigation/scan'),
    ('GET', '/tools'),
    ('POST', '/tools/run'),
    ('GET', '/graph'),
    ('GET', '/graph/build'),
    ('POST', '/graph/alias'),
    ('POST', '/graph/alias/{alias_id}/delete'),
    ('GET', '/attention'),
    ('GET', '/attention/count'),
    ('GET', '/entity'),
    ('GET', '/entity/items'),
    ('POST', '/items/{item_id}/bookmark'),
    ('POST', '/items/{item_id}/unbookmark'),
    ('POST', '/items/{item_id}/evidence'),
    ('GET', '/items/{item_id}/archive'),
    ('POST', '/items/{item_id}/archive'),
    ('POST', '/items/{item_id}/archive/lookup'),
    ('GET', '/cases/{case_id}/archive'),
    ('POST', '/cases/{case_id}/archive-all'),
    ('POST', '/cases/{case_id}/auto-archive'),
    ('POST', '/settings/archive'),
    ('POST', '/feed/read-all'),
    ('POST', '/bulk/read'),
    ('POST', '/bulk/dismiss'),
    ('POST', '/bulk/lists/{list_id}/add'),
    ('POST', '/bulk/cases/{case_id}/add'),
    ('POST', '/items/{item_id}/read'),
    ('POST', '/items/{item_id}/unread'),
    ('POST', '/items/{item_id}/dismiss'),
    ('POST', '/items/{item_id}/undismiss'),
    ('GET', '/items/{item_id}/detail'),
    ('POST', '/items/{item_id}/similar'),
    ('POST', '/items/{item_id}/verify'),
    ('GET', '/lists'),
    ('POST', '/lists'),
    ('POST', '/lists/{list_id}/update'),
    ('POST', '/lists/{list_id}/delete'),
    ('GET', '/lists/{list_id}'),
    ('POST', '/lists/{list_id}/items/{item_id}/add'),
    ('POST', '/lists/{list_id}/items/{item_id}/remove'),
    ('POST', '/lists/quick-add/{item_id}'),
    ('GET', '/cases'),
    ('GET', '/brief/count'),
    ('POST', '/brief/mark-read'),
    ('GET', '/brief'),
    ('POST', '/cases'),
    ('POST', '/cases/active'),
    ('POST', '/cases/{case_id}/update'),
    ('POST', '/cases/{case_id}/close'),
    ('POST', '/cases/{case_id}/reopen'),
    ('POST', '/cases/{case_id}/delete'),
    ('GET', '/cases/{case_id}'),
    ('POST', '/cases/{case_id}/activate'),
    ('POST', '/cases/{case_id}/deactivate'),
    ('POST', '/cases/{case_id}/brief'),
    ('POST', '/cases/{case_id}/scan'),
    ('GET', '/cases/{case_id}/feed'),
    ('GET', '/cases/{case_id}/reviewed'),
    ('POST', '/cases/{case_id}/read-all'),
    ('POST', '/cases/{case_id}/terms'),
    ('POST', '/cases/{case_id}/terms/{term_id}/delete'),
    ('POST', '/cases/{case_id}/terms/{term_id}/edit'),
    ('POST', '/cases/{case_id}/terms/translate'),
    ('POST', '/cases/{case_id}/terms/generate'),
    ('POST', '/cases/{case_id}/questions'),
    ('POST', '/cases/{case_id}/questions/{rid}/delete'),
    ('POST', '/cases/{case_id}/questions/generate'),
    ('POST', '/cases/{case_id}/subcases'),
    ('POST', '/cases/{case_id}/subcases/{sub_id}/terms'),
    ('POST', '/cases/{case_id}/subcases/{sub_id}/terms/{term_id}/delete'),
    ('GET', '/cases/{case_id}/timeline'),
    ('POST', '/cases/{case_id}/notes'),
    ('POST', '/notes/{note_id}/update'),
    ('POST', '/notes/{note_id}/delete'),
    ('POST', '/cases/{case_id}/pin-all'),
    ('POST', '/cases/{case_id}/items'),
    ('POST', '/cases/{case_id}/items/{item_id}/add'),
    ('POST', '/cases/{case_id}/items/{item_id}/remove'),
    ('POST', '/cases/{case_id}/items/{item_id}/unpin'),
    ('POST', '/cases/quick-add/{item_id}'),
    ('GET', '/cases/{case_id}/report'),
    ('GET', '/cases/{case_id}/obsidian'),
    ('GET', '/cases/{case_id}/evidence-manifest'),
    ('GET', '/export'),
    ('GET', '/transfer'),
    ('POST', '/transfer/export'),
    ('POST', '/transfer/import'),
    ('GET', '/security'),
    ('POST', '/security/scan-file'),
    ('GET', '/security/report'),
    ('POST', '/assistant/ask'),
    ('POST', '/assistant/save-log'),
    ('GET', '/watchlists/count'),
    ('GET', '/watchlists'),
    ('POST', '/watchlists'),
    ('POST', '/watchlists/{watchlist_id}/delete'),
    ('POST', '/watchlists/{watchlist_id}/toggle'),
    ('GET', '/topics'),
    ('POST', '/topics/capsule'),
    ('POST', '/topics/capsule/generate'),
    ('POST', '/topics/capsule/term'),
    ('POST', '/topics/capsule/term/{sub_id}/delete'),
    ('POST', '/topics/capsule/delete'),
    ('POST', '/topics/preset'),
    ('POST', '/onboard/bundle'),
    ('POST', '/topics/add'),
    ('POST', '/topics/{sub_id}/delete'),
    ('GET', '/requirements'),
    ('POST', '/requirements'),
    ('POST', '/requirements/{req_id}/toggle'),
    ('POST', '/requirements/{req_id}/delete'),
    ('GET', '/intel'),
    ('GET', '/intel/export'),
    ('GET', '/intel/feed'),
    ('POST', '/intel/scan'),
    ('GET', '/guide'),
    ('GET', '/settings'),
    ('POST', '/settings/auto-scan'),
    ('GET', '/settings/auto-scan-status'),
    ('POST', '/settings/provider'),
    ('POST', '/settings/ollama/check'),
    ('POST', '/settings/ollama/pull'),
    ('GET', '/settings/ollama/pull/status'),
    ('POST', '/settings/secrets'),
    ('POST', '/settings/translation'),
    ('POST', '/settings/local-llm'),
    ('POST', '/settings/local-llm/check'),
    ('POST', '/settings/email/provider'),
    ('POST', '/settings/email'),
    ('POST', '/settings/email/test'),
    ('POST', '/settings/digest'),
    ('POST', '/settings/digest/test'),
    ('GET', '/cases/{case_id}/reports'),
    ('POST', '/cases/{case_id}/reports/settings'),
    ('POST', '/cases/{case_id}/reports/generate'),
    ('GET', '/cases/{case_id}/reports/{filename}'),
    ('POST', '/cases/{case_id}/reports/{filename}/delete'),
    ('GET', '/sources'),
    ('GET', '/sources/custom'),
    ('POST', '/sources/custom/plan'),
    ('POST', '/sources/custom'),
    ('POST', '/sources/custom/{source_id}/update'),
    ('POST', '/sources/custom/{source_id}/toggle'),
    ('POST', '/sources/custom/{source_id}/delete'),
    ('POST', '/sources/custom/{source_id}/test'),
]


def _flatten(routes) -> list:
    """Routes in effective match order.

    Older FastAPI copies an included router's routes into the parent;
    newer FastAPI keeps an ``_IncludedRouter`` entry and matches its children
    in place (first FULL match wins, first PARTIAL is remembered), which is
    the same as matching the flattened sequence. None of our routers use a
    prefix, so child paths are already absolute.
    """
    out = []
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            out.extend(_flatten(inner.routes))
        else:
            out.append(r)
    return out


def _current_routes() -> list[tuple[str, str]]:
    from nexus.web.app import app

    out = []
    for r in _flatten(app.router.routes):
        methods = sorted(getattr(r, "methods", None) or [])
        out.append((",".join(methods) or type(r).__name__, r.path))
    return out


_PARAM = re.compile(r"^\{[^}]+\}$")


def _overlap(a: str, b: str) -> bool:
    """True if some concrete URL could match both path templates."""
    sa, sb = a.strip("/").split("/"), b.strip("/").split("/")
    if len(sa) != len(sb):
        return False
    return all(x == y or _PARAM.match(x) or _PARAM.match(y) for x, y in zip(sa, sb, strict=True))


def test_route_set_unchanged():
    current = _current_routes()
    assert sorted(current) == sorted(EXPECTED_ROUTES)
    assert len(current) == len(set(current)), "duplicate route registered"


@pytest.mark.parametrize("mount", ["/static", "/data"])
def test_static_mounts_precede_app_routes(mount):
    current = _current_routes()
    assert current.index(("Mount", mount)) < current.index(("GET", "/health"))


def test_overlapping_routes_keep_original_order():
    current = _current_routes()
    pos = {r: i for i, r in enumerate(current)}
    violations = []
    for i, a in enumerate(EXPECTED_ROUTES):
        for b in EXPECTED_ROUTES[i + 1:]:
            if a[0] == "Mount" or b[0] == "Mount":
                continue
            if _overlap(a[1], b[1]) and pos[a] > pos[b]:
                violations.append((a, b))
    assert not violations, f"route order changed for overlapping patterns: {violations}"
