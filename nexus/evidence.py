"""Evidence Vault — court-friendly capture of a source at collection time.

Takes a headless full-page screenshot of an item's source URL with Playwright,
hashes the image (sha256) and timestamps it, then records all three in the
`evidence` table. The image is stored locally under data/evidence so it
survives even if the original source is later deleted.

Playwright is imported lazily and guarded: without it (or its browser binaries)
the capture returns a clear "unavailable" result instead of crashing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from nexus.config import Settings, get_settings
from nexus.db import get_connection
from nexus.netguard import safe_http_url as _is_safe_capture_url

logger = logging.getLogger("nexus.evidence")

_NAV_TIMEOUT_MS = 30000


class EvidenceError(Exception):
    """Raised for an expected, reportable capture failure."""


def _evidence_dir(settings: Settings) -> Path:
    path = settings.data_path / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _browsers_root() -> Path | None:
    """Where Playwright keeps its downloaded browsers on this machine.

    Honours ``PLAYWRIGHT_BROWSERS_PATH`` (set by the packaged build to the
    bundled copy) and otherwise falls back to the per-OS default cache. Returns
    ``None`` if no plausible directory exists.
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env not in ("0", "1") and Path(env).is_dir():
        return Path(env)
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        cand = Path(base) / "ms-playwright" if base else None
    elif sys.platform == "darwin":
        cand = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        cand = Path.home() / ".cache" / "ms-playwright"
    return cand if cand and cand.is_dir() else None


def _discover_chromium_executables() -> list[tuple[str, str]]:
    """Find Chromium binaries on disk, newest first, headless-shell preferred.

    Passing an explicit ``executable_path`` to ``launch`` sidesteps Playwright's
    browser *registry* check — the fragile step that prints "please run
    playwright install" and fails a capture even when the binary is right there
    on disk (a stale long-running server hits this after a Playwright upgrade).
    Returns a list of ``(path, label)``.
    """
    root = _browsers_root()
    if root is None:
        return []
    found: list[tuple[float, str, str]] = []
    # headless-shell first (smaller, what the installer bundles), then full.
    patterns = (
        ("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell.exe", "headless-shell"),
        ("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell", "headless-shell"),
        ("chromium-*/chrome-win*/chrome.exe", "full Chromium"),
        ("chromium-*/chrome-*/chrome", "full Chromium"),
    )
    for pattern, label in patterns:
        for p in root.glob(pattern):
            try:
                if p.is_file():
                    found.append((p.stat().st_mtime, str(p), label))
            except OSError:
                continue
    # Newest build of each kind first; keep headless-shell ahead of full.
    found.sort(key=lambda t: t[0], reverse=True)
    order = {"headless-shell": 0, "full Chromium": 1}
    found.sort(key=lambda t: order.get(t[2], 9))
    return [(path, label) for _, path, label in found]


def _launch_chromium(pw):
    """Launch a headless Chromium for screenshots, as robustly as possible.

    Strategy, broadening on each failure:
      1. Explicit ``executable_path`` to each Chromium binary we can find on
         disk — bypasses the registry/"please install" check that breaks
         captures on otherwise-healthy machines.
      2. ``channel="chromium-headless-shell"`` (the packaged build's browser).
      3. The full Chromium via the registry (dev machines from source).
      4. Bare defaults.
    The real underlying error is preserved in the raised message so a failure is
    diagnosable instead of a generic dead-end.
    """
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    attempts: list[tuple[dict, str]] = []
    for path, label in _discover_chromium_executables():
        attempts.append(
            ({"executable_path": path, "headless": True, "args": launch_args},
             f"{label} @ {path}")
        )
    attempts += [
        ({"channel": "chromium-headless-shell", "args": launch_args}, "headless-shell channel"),
        ({"headless": True, "args": launch_args}, "full Chromium (registry)"),
        ({"args": launch_args}, "Chromium (defaults)"),
    ]
    last_exc: Exception | None = None
    for kwargs, label in attempts:
        try:
            return pw.chromium.launch(**kwargs)
        except Exception as exc:
            last_exc = exc
            logger.info("Chromium launch via %s failed (%s); trying next.", label, exc)
    # Nothing launched — surface a clear, actionable message *with the real cause*.
    detail = f" (last error: {last_exc})" if last_exc else ""
    raise EvidenceError(
        "No Chromium browser is available for screenshots. If you installed from "
        "source, run:  playwright install chromium-headless-shell" + detail
    ) from last_exc


def capture_evidence(item_id: int, url: str, settings: Settings | None = None) -> dict:
    """Screenshot `url`, store + hash it, and link it to `item_id`.

    Returns a small dict describing the result. Must be called from a worker
    thread (FastAPI sync route), not the event loop, because it uses Playwright's
    synchronous API.
    """
    settings = settings or get_settings()
    if not url:
        return {"ok": False, "error": "item has no source URL to capture"}

    safe, reason = _is_safe_capture_url(url)
    if not safe:
        logger.warning("Refusing to capture unsafe URL for item %s: %s", item_id, reason)
        return {"ok": False, "error": reason}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright not installed; evidence capture disabled.")
        return {"ok": False, "error": "playwright not installed"}

    captured_at = datetime.now(timezone.utc)
    fname = f"item{item_id}_{captured_at.strftime('%Y%m%d%H%M%S')}.png"
    out_path = _evidence_dir(settings) / fname

    try:
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                # A real desktop UA + viewport so sites serve their normal page
                # (some refuse or degrade for headless defaults).
                page = browser.new_page(
                    viewport={"width": 1366, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                # "networkidle" is unreliable: ad/analytics traffic on many news
                # sites never goes quiet, so it would time out and fail an
                # otherwise-good capture. Wait for DOM content instead, then give
                # the page a short grace period to paint above-the-fold content.
                # A navigation timeout is NOT fatal: many sites keep loading
                # trackers forever, but the article is already on screen — so we
                # press on and screenshot whatever rendered.
                try:
                    page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                except Exception as nav_exc:
                    logger.info(
                        "Navigation to %s didn't fully settle (%s); capturing what "
                        "loaded.", url, nav_exc,
                    )
                try:
                    page.wait_for_load_state("load", timeout=8000)
                except Exception:
                    pass  # partial load is fine; we screenshot what rendered
                page.wait_for_timeout(1200)
                # full_page can fail on pages with extreme/transformed layouts;
                # fall back to the visible viewport so we still get evidence.
                try:
                    page.screenshot(path=str(out_path), full_page=True)
                except Exception as shot_exc:
                    logger.info(
                        "Full-page screenshot failed (%s); capturing viewport only.",
                        shot_exc,
                    )
                    page.screenshot(path=str(out_path), full_page=False)
            finally:
                browser.close()
    except EvidenceError as exc:
        logger.warning("Evidence capture unavailable for item %s: %s", item_id, exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("Evidence capture failed for item %s", item_id)
        return {"ok": False, "error": str(exc)}

    if not out_path.exists():
        return {"ok": False, "error": "the page could not be rendered to an image"}

    sha256 = _sha256_file(out_path)
    rel = f"evidence/{fname}"

    # Best-effort OCR so the screenshot's visible text becomes searchable too.
    # Never let a missing engine / unreadable image break a successful capture.
    try:
        from nexus.ocr import extract_text

        ocr_text = extract_text(out_path)
    except Exception:
        logger.exception("OCR step failed for item %s; storing evidence without it", item_id)
        ocr_text = None

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO evidence (item_id, screenshot, sha256, captured_at, ocr_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_id, rel, sha256, captured_at.isoformat(), ocr_text),
        )
    return {
        "ok": True,
        "screenshot": rel,
        "sha256": sha256,
        "captured_at": captured_at.isoformat(),
        "ocr_chars": len(ocr_text) if ocr_text else 0,
    }


def list_evidence(item_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, screenshot, sha256, captured_at, ocr_text FROM evidence "
            "WHERE item_id = ? ORDER BY id DESC",
            (item_id,),
        ).fetchall()
    return [dict(r) for r in rows]
