"""Tests for the shared SSRF guard (nexus.netguard.safe_http_url).

The guard is the single chokepoint for every server-side fetch of an
externally-influenced URL (evidence capture, custom API sources, RSS, AI source
planner). These tests pin the policy without touching the network: DNS is
monkeypatched so we control exactly which IPs a host "resolves" to.
"""

from __future__ import annotations

import socket

import pytest

from nexus import netguard


def _fake_resolve(monkeypatch, addr: str) -> None:
    """Make getaddrinfo resolve any host to a single chosen address."""
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    sockaddr = (addr, 0, 0, 0) if family == socket.AF_INET6 else (addr, 0)

    def fake_getaddrinfo(host, *_a, **_k):
        return [(family, socket.SOCK_STREAM, 0, "", sockaddr)]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake_getaddrinfo)


def test_rejects_non_http_scheme():
    ok, reason = netguard.safe_http_url("file:///etc/passwd")
    assert ok is False
    assert "scheme" in reason


def test_rejects_missing_host():
    ok, _ = netguard.safe_http_url("http://")
    assert ok is False


def test_allows_public_host(monkeypatch):
    _fake_resolve(monkeypatch, "93.184.216.34")  # example.com, public
    ok, reason = netguard.safe_http_url("https://example.com/feed")
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",          # loopback
        "10.0.0.5",           # private
        "192.168.1.10",       # private
        "169.254.169.254",    # cloud metadata / link-local
        "0.0.0.0",            # unspecified
        "::1",                # IPv6 loopback
    ],
)
def test_rejects_internal_addresses(monkeypatch, addr):
    _fake_resolve(monkeypatch, addr)
    ok, reason = netguard.safe_http_url("http://internal.example/x")
    assert ok is False
    assert "non-public" in reason


@pytest.mark.parametrize(
    "addr",
    [
        "::ffff:127.0.0.1",       # IPv4-mapped loopback
        "::ffff:10.0.0.1",        # IPv4-mapped private
        "::ffff:169.254.169.254", # IPv4-mapped metadata
    ],
)
def test_rejects_ipv4_mapped_ipv6(monkeypatch, addr):
    # Older Python (the Docker 3.11 runtime) does not delegate is_private/
    # is_loopback through the mapping, so the guard must check the embedded
    # IPv4 explicitly. This test fails on an unhardened guard under 3.11.
    _fake_resolve(monkeypatch, addr)
    ok, reason = netguard.safe_http_url("http://mapped.example/x")
    assert ok is False
    assert "non-public" in reason


def test_fails_closed_on_unresolvable(monkeypatch):
    def boom(host, *_a, **_k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(netguard.socket, "getaddrinfo", boom)
    ok, reason = netguard.safe_http_url("https://does-not-resolve.invalid/")
    assert ok is False
    assert "resolve" in reason
