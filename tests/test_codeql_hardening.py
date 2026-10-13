"""Regressions for the issues found by the first CodeQL scan."""

import logging
import time

import httpx
import pytest

from nexus import netguard


@pytest.fixture
def public_dns(monkeypatch):
    """Treat *.example as public and everything else via the real guard."""
    real = netguard.safe_http_url

    def fake(url: str):
        if ".example" in url:
            return True, ""
        return real(url)

    monkeypatch.setattr(netguard, "safe_http_url", fake)


def _mock(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(netguard.httpx, "Client", client)


def test_safe_get_refuses_redirect_to_loopback(public_dns, monkeypatch):
    """SSRF: a public URL must not bounce the request to 127.0.0.1."""
    _mock(monkeypatch, lambda r: httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/settings"}))
    with pytest.raises(ValueError, match="non-public"):
        netguard.safe_get("https://docs.example/api")


def test_safe_get_follows_public_redirects(public_dns, monkeypatch):
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new"})
        return httpx.Response(200, text="ok")

    _mock(monkeypatch, handler)
    assert netguard.safe_get("https://docs.example/old").text == "ok"


def test_safe_get_caps_redirect_loops(public_dns, monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(302, headers={"Location": "/again"}))
    with pytest.raises(ValueError, match="too many redirects"):
        netguard.safe_get("https://docs.example/loop")


def test_html_to_text_drops_script_and_style():
    from nexus.analysis.source_planner import _html_to_text

    html = ("<p>Use <b>GET /items</b></p><script>steal()</script >"
            "<STYLE>p{}</STYLE><script type='x'>a</script\n>tail")
    assert _html_to_text(html) == "Use GET /items tail"


def test_log_lines_cannot_be_forged():
    from nexus.logging_safe import SecretRedactingFilter

    record = logging.LogRecord("t", logging.INFO, __file__, 1,
                               "item title: %s", ("fake\nERROR admin logged in",), None)
    SecretRedactingFilter().filter(record)
    assert "\n" not in record.getMessage()
    assert "fake\\nERROR admin logged in" in record.getMessage()


class _Route:
    def __init__(self, url: str) -> None:
        self.request = type("Req", (), {"url": url})()
        self.result = ""

    def continue_(self) -> None:
        self.result = "continue"

    def abort(self) -> None:
        self.result = "abort"


@pytest.mark.parametrize(("url", "expected"), [
    ("http://127.0.0.1:8000/settings", "abort"),
    ("http://192.168.1.1/admin", "abort"),
    ("file:///etc/passwd", "abort"),
    ("data:image/png;base64,AAAA", "continue"),
])
def test_evidence_browser_blocks_internal_requests(url, expected):
    from nexus.evidence import _guard_request

    route = _Route(url)
    _guard_request(route)
    assert route.result == expected


def test_identifier_classification_is_linear_on_hostile_input():
    from nexus.toolguide import pivot_tools_for

    start = time.perf_counter()
    pivot_tools_for("a" + "." * 50_000 + "@")
    assert time.perf_counter() - start < 1.0
