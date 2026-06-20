"""Tests for the SSRF URL guard and the safe_url link-scheme filter.

These lock in the hardening added during the pre-release security pass: every
server-side fetch of an untrusted URL must refuse internal/loopback/non-http
targets, and every link rendered from collected content must refuse active-script
schemes. Literal IPs are used so the tests need no DNS / network.
"""

from __future__ import annotations

from nexus.netguard import safe_http_url


def test_safe_http_url_allows_public_literal_ip():
    ok, _ = safe_http_url("http://8.8.8.8/path")
    assert ok is True


def test_safe_http_url_rejects_non_http_schemes():
    for url in (
        "file:///etc/passwd",
        "ftp://example.com/x",
        "data:text/html,<script>1</script>",
        "javascript:alert(1)",
    ):
        ok, reason = safe_http_url(url)
        assert ok is False, url
        assert reason


def test_safe_http_url_rejects_loopback_and_internal():
    for url in (
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
    ):
        ok, _ = safe_http_url(url)
        assert ok is False, url


def test_safe_http_url_rejects_empty_and_hostless():
    assert safe_http_url("")[0] is False
    assert safe_http_url("http://")[0] is False


def test_safe_url_filter_blocks_script_schemes():
    from nexus.web.app import _safe_url

    assert _safe_url("javascript:alert(1)") == "#"
    assert _safe_url("data:text/html;base64,AAAA") == "#"
    assert _safe_url("") == "#"
    assert _safe_url(None) == "#"


def test_safe_url_filter_allows_normal_links():
    from nexus.web.app import _safe_url

    assert _safe_url("https://example.com/a") == "https://example.com/a"
    assert _safe_url("http://example.com") == "http://example.com"
    assert _safe_url("mailto:a@b.com") == "mailto:a@b.com"
    # Leading/trailing whitespace is tolerated but the scheme still governs.
    assert _safe_url("  https://x.io  ") == "https://x.io"
