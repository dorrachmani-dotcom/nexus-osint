"""Local file safety scanner — "I'm bringing a file in; is it clean?".

Runs entirely on this machine. Given a file's bytes it reports a verdict —
``clean``, ``suspicious`` or ``dangerous`` — with a plain-language list of why.
The goal is to catch the file-borne tricks that matter for an analyst importing
material from a USB stick or the web, without pretending to be a full antivirus:

  * Hidden executables / scripts disguised by their name (a "photo.png" that is
    really a Windows .exe), detected by inspecting the real file signature.
  * Booby-trapped archives: ``zip slip`` paths that would write outside the
    target folder, and ``zip bombs`` that explode to an enormous size.
  * Executables, installers and scripts hiding *inside* an archive.
  * Office documents with macro containers and other risky formats — flagged so
    the operator opens them with care.

Two optional extra engines kick in only when available, in the spirit of the
rest of the platform (graceful degradation — used if present, skipped if not):

  * **ClamAV** — if the ``clamscan`` binary is on PATH, the file is also run
    through it for real signature-based detection.
  * **VirusTotal** — OFF by default. Only if the operator sets a
    ``VIRUSTOTAL_API_KEY`` is the file's SHA-256 *hash* (never the file itself)
    checked against the global database. This is the only part that uses the
    network, and only when the operator opts in by providing a key.

Nothing here ever raises; any internal error degrades to a clear note.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile

logger = logging.getLogger("nexus.security.filescan")

# Sanity ceiling: refuse to even reason about an absurdly large blob.
_MAX_INPUT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
# Archive-expansion guards.
_MAX_ARCHIVE_TOTAL = 2 * 1024 * 1024 * 1024  # 2 GiB uncompressed
_ZIP_BOMB_RATIO = 120  # uncompressed/compressed beyond this (and big) = bomb
_ZIP_BOMB_MIN = 25 * 1024 * 1024  # only flag the ratio once it's this large

# File extensions that are executable / scripting payloads on a typical desktop.
_DANGEROUS_EXTS = {
    "exe", "dll", "scr", "com", "pif", "msi", "msp", "bat", "cmd", "ps1",
    "psm1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta", "jar", "sh",
    "bash", "csh", "ksh", "run", "bin", "apk", "app", "deb", "rpm", "lnk",
    "reg", "cpl", "gadget", "msc",
}

_LEVEL_RANK = {"info": 0, "warn": 1, "danger": 2}


def _finding(level: str, message: str) -> dict:
    return {"level": level, "message": message}


def _ext(filename: str) -> str:
    name = (filename or "").strip().lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _sniff(data: bytes) -> tuple[str, str]:
    """Return ``(type_label, family)`` from the leading bytes.

    ``family`` is one of ``executable``, ``archive``, ``document``, ``image``,
    ``text``, ``data`` — used to drive the checks below.
    """
    if not data:
        return "empty file", "data"
    head = data[:16]

    if head[:2] == b"MZ":
        return "Windows executable (PE/.exe/.dll)", "executable"
    if head[:4] == b"\x7fELF":
        return "Linux executable (ELF)", "executable"
    if head[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
        return "macOS executable (Mach-O)", "executable"
    if head[:4] == b"\xca\xfe\xba\xbe":
        return "Java/Mach-O fat binary", "executable"
    if head[:2] == b"#!":
        return "script with a shebang (#!)", "executable"
    if head[:4] == b"PK\x03\x04" or head[:4] == b"PK\x05\x06":
        return "ZIP archive (or .nexusbundle / Office / jar)", "archive"
    if head[:4] == b"Rar!" or head[:7] == b"Rar!\x1a\x07\x00":
        return "RAR archive", "archive"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7-Zip archive", "archive"
    if head[:2] == b"\x1f\x8b":
        return "gzip archive", "archive"
    if head[:4] == b"%PDF":
        return "PDF document", "document"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "legacy Microsoft Office document (OLE)", "document"
    if head[:4] == b"L\x00\x00\x00":
        return "Windows shortcut (.lnk)", "executable"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG image", "image"
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG image", "image"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF image", "image"
    # crude text test
    try:
        sample = data[:4096]
        sample.decode("utf-8")
        return "text / data", "text"
    except UnicodeDecodeError:
        return "binary data", "data"


def _scan_zip(data: bytes, findings: list[dict]) -> None:
    """Inspect a ZIP for slip paths, bombs and embedded executables."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        findings.append(_finding("warn", "Looks like an archive but could not be opened — treat with care."))
        return

    total_uncompressed = 0
    total_compressed = 0
    names: list[str] = []
    for info in zf.infolist():
        name = info.filename
        names.append(name)
        total_uncompressed += info.file_size
        total_compressed += info.compress_size

        norm = name.replace("\\", "/")
        if norm.startswith("/") or norm.startswith("../") or "/../" in norm or norm == ".." \
                or (len(name) > 1 and name[1] == ":"):
            findings.append(_finding(
                "danger",
                f"Archive entry tries to escape its folder (path traversal / 'zip slip'): {name!r}.",
            ))

        ext = _ext(name)
        if ext in _DANGEROUS_EXTS:
            findings.append(_finding(
                "danger",
                f"Archive contains an executable/script: {name!r} (.{ext}).",
            ))

    if total_uncompressed > _MAX_ARCHIVE_TOTAL:
        findings.append(_finding(
            "danger",
            f"Archive expands to a very large size ({total_uncompressed // (1024*1024)} MB) — "
            "possible 'zip bomb'.",
        ))
    elif total_compressed > 0 and total_uncompressed >= _ZIP_BOMB_MIN:
        ratio = total_uncompressed / max(total_compressed, 1)
        if ratio > _ZIP_BOMB_RATIO:
            findings.append(_finding(
                "danger",
                f"Archive expands {int(ratio)}x when opened — possible 'zip bomb'.",
            ))

    # A genuine Nexus transfer bundle always carries a data.json manifest.
    if "data.json" in names:
        findings.append(_finding("info", "Contains a Nexus data.json manifest (looks like a transfer bundle)."))


def _scan_clamav(data: bytes, findings: list[dict], scanners: list[str]) -> None:
    exe = shutil.which("clamscan")
    if not exe:
        return
    scanners.append("ClamAV")
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".scan") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        # Fixed argv (resolved clamscan path + our temp file), no shell.
        # check=False: exit code 1 means "infected" and is handled below.
        proc = subprocess.run(  # noqa: S603
            [exe, "--no-summary", "--stdout", tmp_path],
            capture_output=True, text=True, timeout=120, check=False,
        )
        # clamscan exit code: 0 clean, 1 infected, 2 error.
        if proc.returncode == 1:
            detail = (proc.stdout or "").strip().splitlines()
            sig = detail[-1].split(":", 1)[-1].strip() if detail else "a known threat"
            findings.append(_finding("danger", f"ClamAV flagged this file: {sig}."))
        elif proc.returncode == 0:
            findings.append(_finding("info", "ClamAV scanned the file and found nothing."))
        else:
            findings.append(_finding("info", "ClamAV could not complete its scan."))
    except Exception:
        logger.debug("clamscan step failed", exc_info=True)
        findings.append(_finding("info", "ClamAV is installed but its scan did not complete."))
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def _scan_virustotal(sha256: str, findings: list[dict], scanners: list[str]) -> None:
    """Optional, opt-in: look the HASH up on VirusTotal. Never sends the file."""
    api_key = (os.environ.get("VIRUSTOTAL_API_KEY") or "").strip()
    if not api_key:
        return
    scanners.append("VirusTotal (hash)")
    try:
        import json
        import urllib.request

        req = urllib.request.Request(
            f"https://www.virustotal.com/api/v3/files/{sha256}",
            headers={"x-apikey": api_key},
        )
        # Fixed https:// VirusTotal endpoint; only the hash is interpolated.
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        stats = (
            payload.get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
        )
        malicious = int(stats.get("malicious", 0))
        suspicious = int(stats.get("suspicious", 0))
        if malicious:
            findings.append(_finding(
                "danger",
                f"VirusTotal: {malicious} engines flagged this file as malicious.",
            ))
        elif suspicious:
            findings.append(_finding(
                "warn", f"VirusTotal: {suspicious} engines flagged this file as suspicious."))
        else:
            findings.append(_finding("info", "VirusTotal knows this file and reports it clean."))
    except Exception as exc:  # 404 = unknown hash, network down, etc.
        status = getattr(exc, "code", None)
        if status == 404:
            findings.append(_finding("info", "VirusTotal has not seen this file before."))
        else:
            findings.append(_finding("info", "VirusTotal lookup was skipped (offline or unavailable)."))


def scan_bytes(data: bytes, filename: str = "", *, use_network: bool = True) -> dict:
    """Scan in-memory bytes and return a verdict dict. Never raises.

    ``use_network`` only matters when a VirusTotal key is configured; set it
    False to force a purely offline scan.
    """
    findings: list[dict] = []
    scanners = ["Local signature & structure check"]
    try:
        size = len(data)
        sha256 = hashlib.sha256(data).hexdigest()

        if size == 0:
            return {
                "ok": True, "verdict": "clean", "findings": [
                    _finding("info", "The file is empty.")],
                "sha256": sha256, "size": 0, "type": "empty file",
                "filename": filename, "scanners": scanners,
            }
        if size > _MAX_INPUT_BYTES:
            return {
                "ok": True, "verdict": "dangerous", "findings": [
                    _finding("danger", "The file is unreasonably large to vet safely.")],
                "sha256": sha256, "size": size, "type": "oversized",
                "filename": filename, "scanners": scanners,
            }

        type_label, family = _sniff(data)
        ext = _ext(filename)

        if family == "executable":
            findings.append(_finding(
                "danger",
                f"This is a program/script, not a document ({type_label}). "
                "Importing or opening it could run code on your machine.",
            ))
            # Disguise: claims to be a harmless type but is an executable.
            if ext in {"png", "jpg", "jpeg", "gif", "pdf", "txt", "csv",
                       "json", "nexusbundle", "doc", "docx", "xls", "xlsx"}:
                findings.append(_finding(
                    "danger",
                    f"It is named like a .{ext} file but is actually an executable — a classic disguise.",
                ))
        elif family == "archive":
            _scan_zip(data, findings)
        elif family == "document":
            if "OLE" in type_label:
                findings.append(_finding(
                    "warn",
                    "Legacy Office document — these can carry macros. Open with macros disabled.",
                ))
            else:
                findings.append(_finding(
                    "info", f"Document detected ({type_label})."))
        else:
            findings.append(_finding("info", f"Detected type: {type_label}."))

        # Extra engines (graceful).
        _scan_clamav(data, findings, scanners)
        if use_network:
            _scan_virustotal(sha256, findings, scanners)

        worst = max((_LEVEL_RANK[f["level"]] for f in findings), default=0)
        verdict = {0: "clean", 1: "suspicious", 2: "dangerous"}[worst]
        return {
            "ok": True,
            "verdict": verdict,
            "findings": findings,
            "sha256": sha256,
            "size": size,
            "type": type_label,
            "filename": filename,
            "scanners": scanners,
        }
    except Exception as exc:
        logger.exception("File scan failed")
        return {
            "ok": False,
            "verdict": "suspicious",
            "findings": [_finding("warn", f"The scan could not complete: {exc}")],
            "sha256": "",
            "size": len(data) if isinstance(data, (bytes, bytearray)) else 0,
            "type": "unknown",
            "filename": filename,
            "scanners": scanners,
        }


def scan_file(path, *, use_network: bool = True) -> dict:
    """Read a file from disk and scan it. Never raises."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except Exception as exc:
        return {
            "ok": False, "verdict": "suspicious",
            "findings": [_finding("warn", f"Could not read the file: {exc}")],
            "sha256": "", "size": 0, "type": "unknown",
            "filename": str(path), "scanners": [],
        }
    return scan_bytes(data, filename=os.path.basename(str(path)), use_network=use_network)
