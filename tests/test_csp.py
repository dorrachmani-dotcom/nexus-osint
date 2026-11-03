"""Strict Content-Security-Policy: no inline script, no eval (S4)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

WEB = Path(__file__).resolve().parent.parent / "nexus" / "web"
TEMPLATES = sorted((WEB / "templates").glob("*.html"))
JS_DIR = WEB / "static" / "js"


def _client():
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def _directives(csp: str) -> dict[str, list[str]]:
    out = {}
    for part in csp.split(";"):
        bits = part.split()
        if bits:
            out[bits[0]] = bits[1:]
    return out


def test_csp_header_forbids_inline_script_and_eval(temp_db):
    for path in ("/", "/settings", "/security", "/unlock", "/static/css/app.css"):
        r = _client().get(path, follow_redirects=False)
        d = _directives(r.headers["content-security-policy"])
        assert d["script-src"] == ["'self'"], path
        assert "'unsafe-inline'" not in d.get("default-src", [])
        assert d["object-src"] == ["'none'"] and d["frame-ancestors"] == ["'none'"]


def test_templates_have_no_inline_code():
    bad = []
    for f in TEMPLATES:
        text = f.read_text(encoding="utf-8")
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)  # Jinja comments
        for m in re.finditer(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", text, re.S | re.I):
            attrs, body = m.group("attrs"), m.group("body").strip()
            if 'type="application/json"' in attrs:
                continue
            if "src=" not in attrs or body:
                bad.append(f"{f.name}: inline <script>")
        for pat, why in ((r"\son[a-z]+\s*=", "on* handler"), (r"hx-on", "hx-on"),
                         (r"javascript:", "javascript: URL"), (r"\bsrcdoc=", "srcdoc iframe")):
            if re.search(pat, text):
                bad.append(f"{f.name}: {why}")
    assert not bad, bad


def test_referenced_scripts_exist_and_actions_are_defined():
    actions_js = (JS_DIR / "actions.js").read_text(encoding="utf-8")
    defined = set(re.findall(r"^\s{4}'([a-z-]+)':\s*(?:function|briefRefresh)", actions_js, re.M))
    callable_ = set(re.findall(r"(\w+): 1", actions_js))
    missing = []
    for f in TEMPLATES:
        text = f.read_text(encoding="utf-8")
        for src in re.findall(r'<script src="/static/js/([^"]+)"', text):
            if not (JS_DIR / src).is_file():
                missing.append(f"{f.name}: {src}")
        for attr in re.findall(r'data-(?:action|change|toggled|done)="([^"]+)"', text):
            for spec in attr.split(";"):
                name, _, arg = spec.strip().partition(":")
                if name not in defined:
                    missing.append(f"{f.name}: action {name}")
                if name == "call" and arg not in callable_:
                    missing.append(f"{f.name}: call {arg}")
    assert not missing, missing


def test_htmx_eval_disabled():
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    assert '"allowEval": false' in base
    assert base.index("htmx-config") < base.index("htmx.min.js")


def test_graph_frame_has_own_narrow_policy(temp_db):
    from nexus.web import graph_frames

    url = graph_frames.store("<html><script>var x = 1;</script></html>")
    client = _client()
    r = client.get(url)
    assert r.status_code == 200 and "var x = 1" in r.text
    d = _directives(r.headers["content-security-policy"])
    assert d["default-src"] == ["'none'"] and d["frame-ancestors"] == ["'self'"]
    assert d["sandbox"] == ["allow-scripts"]  # opaque origin even when opened directly
    assert "connect-src" not in d  # falls back to default-src 'none': no network
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    gone = client.get("/graph/frame/not-a-token")
    assert gone.status_code == 404 and gone.headers["x-frame-options"] == "SAMEORIGIN"
    assert graph_frames.store(None) == ""


def test_graph_templates_use_frame_url():
    from nexus.web.common import TEMPLATES as T

    for name in ("_entity_graph.html", "_tool_results.html"):
        assert "graph_html | graph_frame_url" in (WEB / "templates" / name).read_text(encoding="utf-8")
    url = T.env.from_string("{{ g | graph_frame_url }}").render(g="<p>graph</p>")
    assert url.startswith("/graph/frame/")
