"""Opt-in internal-network allow-list in the SSRF guard (air-gapped intranets).

No real DNS or network: ``socket.getaddrinfo`` is faked and HTTP goes through
``httpx.MockTransport``.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from nexus import netguard

# Fictional internal names -> addresses.
_DNS = {
    "wiki.corp": "10.20.1.5",
    "docs.corp": "10.20.1.6",
    "other.corp": "192.168.7.7",
    "public.example": "93.184.216.34",
    "sneaky.corp": "127.0.0.1",
    "meta.corp": "169.254.169.254",
}


@pytest.fixture
def fake_dns(monkeypatch):
    def resolve(host, *args, **kwargs):
        addr = _DNS.get(host, host)
        fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (addr, 0))]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", resolve)


def _policy(monkeypatch, hosts: str = "", cidrs: str = ""):
    monkeypatch.setenv("INTERNAL_ALLOWED_HOSTS", hosts)
    monkeypatch.setenv("INTERNAL_ALLOWED_CIDRS", cidrs)
    from nexus.config import get_settings

    get_settings.cache_clear()


def test_private_blocked_by_default(fake_dns, monkeypatch):
    _policy(monkeypatch)
    ok, reason = netguard.safe_http_url("http://wiki.corp/page")
    assert not ok and "non-public" in reason and "INTERNAL_ALLOWED_HOSTS" in reason
    assert netguard.safe_http_url("https://public.example/")[0]


def test_allow_listed_host_passes(fake_dns, monkeypatch):
    _policy(monkeypatch, hosts="wiki.corp")
    ok, reason, internal = netguard.check_url("http://wiki.corp/page")
    assert ok and internal, reason
    assert not netguard.safe_http_url("http://docs.corp/")[0]  # not listed


def test_wildcard_and_cidr(fake_dns, monkeypatch):
    _policy(monkeypatch, hosts="*.corp")
    assert netguard.safe_http_url("http://docs.corp/")[0]
    _policy(monkeypatch, cidrs="10.20.0.0/16")
    assert netguard.safe_http_url("http://docs.corp/")[0]
    assert not netguard.safe_http_url("http://other.corp/")[0]  # 192.168.x not in CIDR


def test_catch_all_entries_are_ignored():
    p = netguard.parse_policy("*, *.*, wiki.corp", "0.0.0.0/0, ::/0, bogus, 10.0.0.0/8")
    assert p.hosts == ("wiki.corp",)
    assert [str(n) for n in p.networks] == ["10.0.0.0/8"]


@pytest.mark.parametrize("host", ["sneaky.corp", "meta.corp"])
def test_loopback_and_metadata_blocked_even_when_listed(fake_dns, monkeypatch, host):
    _policy(monkeypatch, hosts="*.corp", cidrs="127.0.0.0/8, 169.254.0.0/16")
    ok, reason = netguard.safe_http_url(f"http://{host}/")
    assert not ok and "never allowed" in reason


def test_literal_loopback_and_metadata_blocked_even_when_listed(monkeypatch):
    _policy(monkeypatch, hosts="127.0.0.1,169.254.169.254", cidrs="127.0.0.0/8")
    assert not netguard.safe_http_url("http://127.0.0.1:9000/")[0]
    assert not netguard.safe_http_url("http://169.254.169.254/latest/meta-data")[0]


def test_app_port_blocked_on_internal_host(fake_dns, monkeypatch):
    _policy(monkeypatch, hosts="wiki.corp")
    ok, reason = netguard.safe_http_url("http://wiki.corp:8000/settings")
    assert not ok and "own port" in reason
    assert netguard.safe_http_url("http://wiki.corp:8080/")[0]


def _mock(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(netguard.httpx, "Client", client)


def test_redirect_from_allowed_internal_to_unlisted_private_blocked(fake_dns, monkeypatch):
    _policy(monkeypatch, hosts="wiki.corp")
    _mock(monkeypatch, lambda r: httpx.Response(302, headers={"Location": "http://other.corp/x"}))
    with pytest.raises(ValueError, match="non-public"):
        netguard.safe_get("http://wiki.corp/start")


def test_redirect_to_metadata_blocked(fake_dns, monkeypatch):
    _policy(monkeypatch, hosts="wiki.corp")
    _mock(monkeypatch, lambda r: httpx.Response(
        302, headers={"Location": "http://169.254.169.254/latest/"}))
    with pytest.raises(ValueError):
        netguard.safe_get("http://wiki.corp/start")


def test_credentials_dropped_on_cross_host_redirect(fake_dns, monkeypatch):
    _policy(monkeypatch, hosts="*.corp")
    seen: list[tuple[str, str | None]] = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "wiki.corp":
            return httpx.Response(302, headers={"Location": "http://docs.corp/landing"})
        return httpx.Response(200, text="ok")

    _mock(monkeypatch, handler)
    resp = netguard.safe_get("http://wiki.corp/", headers={"Authorization": "Basic abc"})
    assert resp.text == "ok"
    assert seen == [("wiki.corp", "Basic abc"), ("docs.corp", None)]


def test_post_303_becomes_get_and_307_keeps_post(fake_dns, monkeypatch):
    _policy(monkeypatch, hosts="wiki.corp")
    methods: list[str] = []

    def handler(request):
        methods.append(request.method)
        if request.url.path == "/a":
            return httpx.Response(303, headers={"Location": "/b"})
        if request.url.path == "/b":
            return httpx.Response(200, text="b")
        if request.url.path == "/c":
            return httpx.Response(307, headers={"Location": "/d"})
        return httpx.Response(200, text=request.content.decode())

    _mock(monkeypatch, handler)
    assert netguard.safe_request("POST", "http://wiki.corp/a", json={"q": 1}).text == "b"
    assert methods == ["POST", "GET"]
    assert netguard.safe_request("POST", "http://wiki.corp/c", json={"q": 1}).text == '{"q":1}'


def test_max_bytes_enforced(fake_dns, monkeypatch):
    _policy(monkeypatch, hosts="wiki.corp")
    _mock(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(netguard.ResponseTooLarge):
        netguard.safe_get("http://wiki.corp/big", max_bytes=1000)


def test_tls_modes(tmp_path, monkeypatch):
    assert netguard.tls_verify_for("verify") is True
    assert netguard.tls_verify_for("insecure") is False
    pem = tmp_path / "ca.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("INTERNAL_CA_BUNDLE", str(pem))
    from nexus.config import get_settings

    get_settings.cache_clear()
    assert netguard.tls_verify_for("ca_bundle") == str(pem)


def test_egress_labels_internal_allowed(monkeypatch):
    _policy(monkeypatch, hosts="wiki.corp", cidrs="10.20.0.0/16")
    from nexus.security.egress import classify

    assert classify("wiki.corp", 443) == ("Internal network (allowed)", True)
    assert classify("10.20.3.4", 80) == ("Internal network (allowed)", True)
    assert classify("127.0.0.1", 8000)[0] == "Local machine"


def test_internal_network_settings_roundtrip(temp_db):
    from fastapi.testclient import TestClient

    from nexus.envstore import read_env_value
    from nexus.web.app import app

    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/settings/internal-network",
               data={"hosts": "wiki.corp *.docs.internal, *", "cidrs": "10.20.0.0/16, nope",
                     "ca_bundle": ""})
    assert r.status_code == 200 and "Saved" in r.text
    assert read_env_value("INTERNAL_ALLOWED_HOSTS") == "wiki.corp,*.docs.internal"
    assert read_env_value("INTERNAL_ALLOWED_CIDRS") == "10.20.0.0/16"
    assert "wiki.corp" in c.get("/settings/internal-network").text
