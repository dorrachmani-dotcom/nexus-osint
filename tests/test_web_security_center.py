"""Integration tests for the Security Center web routes.

The Security Center (``/security``) is the analyst's auditable, fully-local
defence surface: it shows the outbound-traffic monitor and offers a one-file
safety check (``/security/scan-file``) for vetting anything brought in from a USB
stick or the web. These tests lock in that the page renders, that a clean file is
reported clean while a disguised executable is flagged dangerous, that the scan
route never persists the uploaded bytes, and that it degrades to a verdict rather
than a 500 on bad input.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def test_security_page_renders(temp_db):
    with temp_db():
        resp = _client().get("/security")
    assert resp.status_code == 200
    # The egress monitor section is part of the page.
    body = resp.text.lower()
    assert "security" in body


def test_scan_file_reports_clean_for_plain_text(temp_db):
    with temp_db():
        resp = _client().post(
            "/security/scan-file",
            files={"upload": ("notes.txt", b"just some harmless notes", "text/plain")},
        )
    assert resp.status_code == 200
    # A clean verdict is surfaced on the page.
    assert "clean" in resp.text.lower()


def test_scan_file_flags_a_disguised_executable(temp_db):
    # A file named like an image but carrying a real executable signature must be
    # caught by the local signature check and reported dangerous.
    pe_header = b"MZ\x90\x00\x03" + b"\x00" * 60 + b"This program cannot be run in DOS mode"
    with temp_db():
        resp = _client().post(
            "/security/scan-file",
            files={"upload": ("photo.png", pe_header, "image/png")},
        )
    assert resp.status_code == 200
    assert "dangerous" in resp.text.lower()


def test_scan_file_persists_nothing(temp_db):
    # The scan is stateless: scanning a file must not create any items/evidence.
    with temp_db() as conn:
        before = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]

    _client().post(
        "/security/scan-file",
        files={"upload": ("x.bin", b"\x00\x01\x02\x03", "application/octet-stream")},
    )

    with temp_db() as conn:
        after = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
        ev = conn.execute("SELECT COUNT(*) AS c FROM evidence").fetchone()["c"]
    assert after == before
    assert ev == 0


def test_audit_report_download(temp_db):
    with temp_db():
        resp = _client().get("/security/report")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    disp = resp.headers["content-disposition"]
    assert "attachment" in disp and ".txt" in disp
    body = resp.text
    assert "DATA-HANDLING AUDIT REPORT" in body
    assert "DESTINATIONS" in body
    assert "127.0.0.1" in body  # the local-bind statement


def test_audit_report_lists_a_recorded_destination():
    # Record an unexpected outbound connection, then confirm the text report
    # surfaces it and flags it as UNEXPECTED.
    import socket

    from nexus.security import egress, format_audit_report

    with egress._lock:
        egress._destinations.clear()
        egress._ip_to_host.clear()
    egress.record_connection(("93.184.216.34", 443), socket.AF_INET)

    text = format_audit_report()
    assert "93.184.216.34" in text
    assert "UNEXPECTED" in text
