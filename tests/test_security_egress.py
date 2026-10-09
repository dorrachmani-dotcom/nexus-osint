"""Tests for the outbound-traffic monitor (nexus.security.egress).

These verify the classification logic (local / AI-provider / source / DNS vs
UNEXPECTED), that recording a connection aggregates correctly and surfaces an
unexpected destination, and that the report shape is what the dashboard expects.
No real sockets are opened — record_connection is called directly with address
tuples, which is exactly what the installed wrapper passes it.
"""

from __future__ import annotations

import socket

from nexus.security import egress


def _reset_state():
    with egress._lock:
        egress._destinations.clear()
        egress._ip_to_host.clear()


def test_loopback_is_local_and_expected():
    cat, expected = egress.classify("127.0.0.1", 8000)
    assert expected is True
    assert "Local" in cat


def test_private_ip_is_local_and_expected():
    cat, expected = egress.classify("192.168.1.50", 11434)
    assert expected is True
    assert "Local" in cat


def test_dns_port_is_expected():
    cat, expected = egress.classify("8.8.8.8", 53)
    assert expected is True
    assert "DNS" in cat


def test_static_ai_provider_is_expected():
    cat, expected = egress.classify("api.anthropic.com", 443)
    assert expected is True
    assert "Anthropic" in cat


def test_static_source_suffix_match_is_expected():
    # Subdomain must match by suffix.
    cat, expected = egress.classify("oauth.reddit.com", 443)
    assert expected is True
    assert "Reddit" in cat


def test_unknown_host_is_unexpected():
    cat, expected = egress.classify("evil.example.net", 443)
    assert expected is False
    assert cat == "Unexpected"


def test_record_connection_aggregates_and_reports():
    _reset_state()
    addr = ("93.184.216.34", 443)  # public, not in any allow-list
    egress.record_connection(addr, socket.AF_INET)
    egress.record_connection(addr, socket.AF_INET)

    report = egress.get_egress_report()
    assert report["total"] == 1
    assert report["unexpected_count"] == 1
    dest = report["destinations"][0]
    assert dest["count"] == 2
    assert dest["expected"] is False
    assert report["ok"] is False  # unexpected destination present


def test_record_connection_local_is_expected_in_report():
    _reset_state()
    egress.record_connection(("127.0.0.1", 8000), socket.AF_INET)
    report = egress.get_egress_report()
    assert report["unexpected_count"] == 0
    assert report["destinations"][0]["expected"] is True


def test_record_connection_ignores_non_inet_family():
    _reset_state()
    # AF_UNIX (or anything non-INET) must be silently ignored.
    fam = getattr(socket, "AF_UNIX", 1)
    egress.record_connection("/tmp/some.sock", fam)
    assert egress.get_egress_report()["total"] == 0


def test_record_connection_never_raises_on_bad_input():
    _reset_state()
    # Malformed address must not raise.
    egress.record_connection(None, socket.AF_INET)
    egress.record_connection((), socket.AF_INET)
    assert egress.get_egress_report()["total"] == 0


def test_dns_cache_maps_ip_to_hostname():
    _reset_state()
    # Simulate what the getaddrinfo wrapper records, then a connect by IP.
    fake_infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.9", 443))]
    egress._remember_dns("api.anthropic.com", fake_infos)
    egress.record_connection(("203.0.113.9", 443), socket.AF_INET)
    dest = egress.get_egress_report()["destinations"][0]
    # Recorded by hostname (resolved from the IP) and classified as expected.
    assert dest["host"] == "api.anthropic.com"
    assert dest["expected"] is True


# --- dynamic allow-list built from the live configuration ------------------

def _reset_allow_cache():
    egress._allow_cache = None
    egress._allow_cache_at = 0.0


class _FakeSettings:
    def __init__(self, *, rss=None, ollama="", telemetry=""):
        self.rss_feed_list = rss or []
        self.ollama_base_url = ollama
        self.telemetry_base_url = telemetry


def test_configured_rss_feed_host_is_expected(monkeypatch):
    # A host the operator added as an RSS feed must classify as an expected
    # source, even though it is in no static allow-list.
    _reset_allow_cache()
    monkeypatch.setattr(
        egress, "_configured_allow",
        lambda: {"feeds.example-news.org": "Source (your RSS feed)"},
    )
    cat, expected = egress.classify("feeds.example-news.org", 443)
    assert expected is True
    assert "RSS" in cat
    # Subdomains of the configured host match by suffix too.
    _cat2, expected2 = egress.classify("cdn.feeds.example-news.org", 443)
    assert expected2 is True


def test_configured_allow_reads_settings_and_caches(monkeypatch):
    # Drive the real _configured_allow(): a fake settings object yields a
    # configured Ollama host that then classifies as expected.
    _reset_allow_cache()
    fake = _FakeSettings(ollama="http://ai.internal.example:11434")
    monkeypatch.setattr("nexus.config.get_settings", lambda: fake)

    allow = egress._configured_allow()
    assert allow.get("ai.internal.example") == "AI provider (Ollama)"

    cat, expected = egress.classify("ai.internal.example", 11434)
    assert expected is True
    assert "Ollama" in cat


def test_configured_allow_is_defensive_on_settings_failure(monkeypatch):
    # If settings can't be read, the allow-list degrades to empty (static-only)
    # rather than raising — an unknown host is still simply "Unexpected".
    _reset_allow_cache()

    def boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr("nexus.config.get_settings", boom)
    allow = egress._configured_allow()
    assert isinstance(allow, dict)
    cat, expected = egress.classify("evil.example.net", 443)
    assert expected is False
    assert cat == "Unexpected"
