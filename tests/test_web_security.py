"""Tests for the web-hardening middleware (nexus.web.security).

These lock in the browser-side protections for the localhost app so they can
never silently regress:

  * DNS-rebinding guard — only loopback ``Host`` headers are answered.
  * CSRF/origin guard — a state-changing request carrying a cross-origin
    ``Origin``/``Referer`` is refused, while same-origin (and header-less,
    non-browser) requests pass.
  * Security headers — every response carries the clickjacking / MIME-sniff /
    referrer / CSP defenses.

The middleware is exercised on a tiny isolated app (no DB, no AI) so the test is
fast and independent of the real route surface. TestClient's default Host is
``testserver``; we point it at a loopback base URL so legitimate requests look
like the real launcher's same-origin traffic.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.web.security import install_security

LOOPBACK = "http://127.0.0.1"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_security(app)

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    @app.post("/act")
    def act() -> dict:
        return {"did": True}

    # base_url sets the Host header to a loopback name, mirroring real traffic.
    return TestClient(app, base_url=LOOPBACK)


# --- DNS-rebinding (Host header) guard -------------------------------------

def test_loopback_host_is_accepted(client):
    assert client.get("/ping").status_code == 200


@pytest.mark.parametrize("host", ["evil.com", "attacker.example", "169.254.169.254"])
def test_non_loopback_host_is_rejected(client, host):
    resp = client.get("/ping", headers={"host": host})
    assert resp.status_code == 400


def test_localhost_and_ipv6_hosts_accepted(client):
    for host in ("localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
        assert client.get("/ping", headers={"host": host}).status_code == 200


# --- CSRF / cross-origin guard on state-changing methods -------------------

def test_cross_origin_post_is_refused(client):
    resp = client.post("/act", headers={"origin": "http://evil.com"})
    assert resp.status_code == 403


def test_cross_origin_via_referer_is_refused(client):
    resp = client.post("/act", headers={"referer": "http://evil.com/page"})
    assert resp.status_code == 403


def test_same_origin_post_passes(client):
    resp = client.post("/act", headers={"origin": LOOPBACK})
    assert resp.status_code == 200
    assert resp.json() == {"did": True}


def test_headerless_post_passes(client):
    # No Origin/Referer => not a browser cross-site post; local non-browser
    # clients must still work.
    resp = client.post("/act")
    assert resp.status_code == 200


def test_safe_method_skips_origin_check(client):
    # A GET is read-only, so a cross-origin Origin header must not block it.
    resp = client.get("/ping", headers={"origin": "http://evil.com"})
    assert resp.status_code == 200


# --- Defense-in-depth response headers -------------------------------------

def test_security_headers_present_on_every_response(client):
    headers = client.get("/ping").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" in headers
    csp = headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "form-action 'self'" in csp
