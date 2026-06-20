"""Tests for the local file-safety scanner (nexus.security.filescan).

These lock in the behaviour an analyst relies on when bringing a file in: real
file-signature sniffing (so a disguised executable is caught), archive checks
(zip-slip and zip-bomb), and the verdict roll-up. Everything runs offline and
with no external tools — the optional ClamAV / VirusTotal engines are skipped
unless present, so these tests are deterministic.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import zipfile

from nexus.security.filescan import scan_bytes, scan_file


def _verdict(data: bytes, filename: str = "") -> str:
    # use_network=False keeps the scan purely local even if a VT key is set.
    return scan_bytes(data, filename=filename, use_network=False)["verdict"]


def test_plain_text_is_clean():
    result = scan_bytes(b"just some harmless notes\n", filename="notes.txt", use_network=False)
    assert result["ok"] is True
    assert result["verdict"] == "clean"
    assert result["sha256"]


def test_empty_file_is_clean():
    result = scan_bytes(b"", filename="empty.dat", use_network=False)
    assert result["verdict"] == "clean"
    assert result["type"] == "empty file"


def test_windows_executable_is_dangerous():
    # MZ header = Windows PE executable.
    data = b"MZ\x90\x00" + b"\x00" * 64
    result = scan_bytes(data, filename="installer.exe", use_network=False)
    assert result["verdict"] == "dangerous"
    assert any(f["level"] == "danger" for f in result["findings"])


def test_executable_disguised_as_png_is_flagged_as_disguise():
    # Real MZ executable, but named like an image — classic disguise.
    data = b"MZ\x90\x00" + b"\x00" * 64
    result = scan_bytes(data, filename="cute_photo.png", use_network=False)
    assert result["verdict"] == "dangerous"
    assert any("disguise" in f["message"].lower() for f in result["findings"])


def test_elf_executable_is_dangerous():
    data = b"\x7fELF" + b"\x00" * 32
    assert _verdict(data, "tool") == "dangerous"


def test_real_png_is_not_executable():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    result = scan_bytes(png, filename="real.png", use_network=False)
    assert result["verdict"] == "clean"
    assert result["type"] == "PNG image"


def test_clean_zip_bundle_is_clean():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.json", '{"items": []}')
        zf.writestr("evidence/shot.png", b"\x89PNG\r\n\x1a\n")
    result = scan_bytes(buf.getvalue(), filename="bundle.nexusbundle", use_network=False)
    assert result["verdict"] == "clean"
    assert any("data.json" in f["message"] for f in result["findings"])


def test_zip_with_embedded_executable_is_dangerous():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.json", "{}")
        zf.writestr("payload.exe", b"MZ\x90\x00")
    assert _verdict(buf.getvalue(), "bundle.nexusbundle") == "dangerous"


def test_zip_slip_path_traversal_is_dangerous():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../escape.txt", "gotcha")
    result = scan_bytes(buf.getvalue(), filename="evil.zip", use_network=False)
    assert result["verdict"] == "dangerous"
    assert any("escape" in f["message"].lower() or "slip" in f["message"].lower()
               for f in result["findings"])


def test_zip_bomb_high_ratio_is_dangerous():
    # Highly compressible payload above the min-size threshold + huge ratio.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", b"\x00" * (40 * 1024 * 1024))  # 40 MB of zeros
    result = scan_bytes(buf.getvalue(), filename="bomb.zip", use_network=False)
    assert result["verdict"] == "dangerous"
    assert any("bomb" in f["message"].lower() for f in result["findings"])


def test_legacy_office_ole_is_suspicious():
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32
    result = scan_bytes(ole, filename="report.doc", use_network=False)
    assert result["verdict"] == "suspicious"


def test_scan_never_raises_on_garbage():
    # Random binary that isn't a known type must still produce a clean dict.
    result = scan_bytes(b"\x01\x02\x03\xff\xfe", filename="x.bin", use_network=False)
    assert result["ok"] is True
    assert result["verdict"] in {"clean", "suspicious", "dangerous"}
    assert "Local signature & structure check" in result["scanners"]


def test_shebang_script_is_dangerous():
    # A shell script (#! shebang) is executable code, not a document.
    data = b"#!/bin/sh\nrm -rf /\n"
    result = scan_bytes(data, filename="setup", use_network=False)
    assert result["verdict"] == "dangerous"
    assert any(f["level"] == "danger" for f in result["findings"])


# --- scan_file (reads from disk) -------------------------------------------

def test_scan_file_reads_and_scans_real_file(tmp_path):
    p = tmp_path / "installer.exe"
    p.write_bytes(b"MZ\x90\x00" + b"\x00" * 64)
    result = scan_file(p, use_network=False)
    assert result["ok"] is True
    assert result["verdict"] == "dangerous"
    # Filename is reduced to the basename, not the full temp path.
    assert result["filename"] == "installer.exe"
    assert result["sha256"]


def test_scan_file_on_unreadable_path_degrades_gracefully(tmp_path):
    # A path that does not exist must not raise — it degrades to a note.
    missing = tmp_path / "does_not_exist.bin"
    result = scan_file(missing, use_network=False)
    assert result["ok"] is False
    assert result["verdict"] == "suspicious"
    assert any("could not read" in f["message"].lower() for f in result["findings"])


# --- VirusTotal (opt-in, hash-only) ----------------------------------------
#
# These lock in the single most OpSec-critical promise of the whole security
# layer: when a VirusTotal key is configured, ONLY the file's SHA-256 hash ever
# leaves the machine — never the file bytes. We assert that by capturing the
# outbound urllib Request and proving the hash is in the URL and the body is
# empty.

class _FakeVTResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_vt(monkeypatch, captured, *, body=None, error=None):
    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = dict(req.header_items())
        if error is not None:
            raise error
        return _FakeVTResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _vt_body(malicious=0, suspicious=0) -> bytes:
    return json.dumps({
        "data": {"attributes": {"last_analysis_stats": {
            "malicious": malicious, "suspicious": suspicious, "harmless": 70,
        }}}
    }).encode("utf-8")


def test_virustotal_sends_only_the_hash_never_the_file(monkeypatch):
    monkeypatch.setenv("VIRUSTOTAL_API_KEY", "test-key")
    captured = {}
    _install_fake_vt(monkeypatch, captured, body=_vt_body(malicious=0))

    payload = b"some private evidence bytes that must never be uploaded"
    result = scan_bytes(payload, filename="evidence.bin", use_network=True)

    # The hash, and only the hash, is what went out.
    assert result["sha256"] in captured["url"]
    assert captured["url"].startswith("https://www.virustotal.com/api/v3/files/")
    # A GET with no request body — the file bytes never leave the machine.
    assert captured["data"] is None
    # And the raw payload must not appear anywhere in the outbound request.
    assert b"private evidence" not in (captured["data"] or b"")
    assert "VirusTotal (hash)" in result["scanners"]


def test_virustotal_malicious_makes_verdict_dangerous(monkeypatch):
    monkeypatch.setenv("VIRUSTOTAL_API_KEY", "test-key")
    captured = {}
    _install_fake_vt(monkeypatch, captured, body=_vt_body(malicious=42))

    result = scan_bytes(b"harmless-looking text", filename="note.txt", use_network=True)
    assert result["verdict"] == "dangerous"
    assert any("virustotal" in f["message"].lower() and f["level"] == "danger"
               for f in result["findings"])


def test_virustotal_unknown_hash_404_stays_clean(monkeypatch):
    monkeypatch.setenv("VIRUSTOTAL_API_KEY", "test-key")
    captured = {}
    err = urllib.error.HTTPError(
        "https://www.virustotal.com/api/v3/files/x", 404, "Not Found", {}, None)
    _install_fake_vt(monkeypatch, captured, error=err)

    result = scan_bytes(b"just some harmless notes\n", filename="n.txt", use_network=True)
    # A file VirusTotal has never seen is not evidence of harm.
    assert result["verdict"] == "clean"
    assert any("not seen this file" in f["message"].lower() for f in result["findings"])


def test_virustotal_skipped_when_no_key(monkeypatch):
    monkeypatch.delenv("VIRUSTOTAL_API_KEY", raising=False)
    called = {"hit": False}

    def fake_urlopen(req, timeout=None):
        called["hit"] = True
        raise AssertionError("VirusTotal must not be contacted without a key")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = scan_bytes(b"just some harmless notes\n", filename="n.txt", use_network=True)
    assert called["hit"] is False
    assert "VirusTotal (hash)" not in result["scanners"]


def test_virustotal_not_called_when_use_network_false(monkeypatch):
    # Even with a key set, an explicit offline scan must never reach out.
    monkeypatch.setenv("VIRUSTOTAL_API_KEY", "test-key")

    def fake_urlopen(req, timeout=None):
        raise AssertionError("offline scan must not use the network")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = scan_bytes(b"just some harmless notes\n", filename="n.txt", use_network=False)
    assert "VirusTotal (hash)" not in result["scanners"]
