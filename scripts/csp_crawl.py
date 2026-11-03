"""Crawl every page in a real browser and fail on any CSP violation.

Starts Nexus on a free loopback port against a THROWAWAY data folder (and an
empty .env, so no real keys are used), loads the fictional demo data, then
opens each page with Playwright, waits for its htmx partials, clicks every
in-page action button that is safe to click (data-action / tabs / details
toggles; nothing that posts), and records:

  * every ``securitypolicyviolation`` event (listened for through an init
    script, which the page's own CSP cannot block), and
  * console errors that mention the Content Security Policy.

Usage:  python scripts/csp_crawl.py            (exit code 1 on any violation)
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INIT = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__csp.push(e.violatedDirective + ' blocked ' + (e.blockedURI || 'inline') +
                    ' at ' + (e.sourceFile || '') + ':' + (e.lineNumber || 0));
}, true);
"""

# Read-only GET pages (with demo data loaded).
PAGES = [
    "/", "/feed", "/brief", "/attention", "/cases", "/cases/1", "/cases/1?tab=timeline",
    "/cases/1?tab=feed", "/cases/1?tab=graph", "/cases/1?tab=reports", "/cases/1?tab=documents",
    "/graph", "/graph?case=1&case=2", "/entity?name=Nightjar%20Group", "/intel", "/topics",
    "/watchlists", "/requirements", "/lists", "/tools", "/investigation", "/settings", "/setup",
    "/guide", "/security", "/security/outbound", "/transfer", "/sources",
    "/sources/documents", "/sources/web", "/sources/confluence", "/sources/obsidian",
    "/sources/custom", "/settings/backup", "/settings/encryption", "/settings/internal-network",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(port: int) -> None:
    import uvicorn

    from nexus.web.app import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="nexus-csp-"))
    env_file = work / "empty.env"
    env_file.write_text("AI_PROVIDER=off\n", encoding="utf-8")
    os.environ.update({"DATA_DIR": str(work), "DATABASE_PATH": str(work / "nexus.db"),
                       "NEXUS_ENV_FILE": str(env_file), "AI_PROVIDER": "off"})
    sys.path.insert(0, str(ROOT))
    port = _free_port()
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    import httpx

    for _ in range(100):
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    httpx.post(f"{base}/demo/load", headers={"Origin": base}, timeout=60)

    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:  # Playwright's Chromium not installed: use the system Edge
            browser = p.chromium.launch(channel="msedge")
        context = browser.new_context()
        context.add_init_script(INIT)
        page = context.new_page()
        console: list[str] = []
        page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console.append(f"page error: {e}"))
        for path in PAGES:
            console.clear()
            resp = page.goto(base + path)
            status = resp.status if resp else 0
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)
            # Exercise the declarative actions that do not change data.
            for sel in ("[data-action^='call:nexusToggleTheme']", "[data-action='nav-toggle']",
                        "[data-action^='call:nexusAssistantToggle']", "[data-action^='call:toggleKind']",
                        "[data-action^='toggle-details']", "summary"):
                for el in page.locator(sel).all()[:3]:
                    try:
                        if el.is_visible():
                            el.click(timeout=1000)
                            page.wait_for_timeout(150)
                    except Exception:  # noqa: S110 - a hidden/detached element is fine
                        pass
            # Open the first item's detail drawer where there is one.
            detail = page.locator("[hx-get*='/detail']")
            if detail.count():
                try:
                    detail.first.click(timeout=1500)
                    page.wait_for_timeout(800)
                except Exception:  # noqa: S110
                    pass
            violations = page.evaluate("window.__csp || []")
            csp_console = [c for c in console if "Content Security Policy" in c or "Refused to" in c]
            other = [c for c in console if c not in csp_console]
            mark = "ok" if not (violations or csp_console) else "CSP VIOLATION"
            if status >= 400:
                problems.append(f"{path}: HTTP {status}")
            print(f"{status} {mark:14} {path}" + (f"  (other console errors: {len(other)})" if other else ""))
            for v in violations + csp_console:
                problems.append(f"{path}: {v}")
                print("    ", v)
            for o in other[:3]:
                print("     console:", o[:200])
        browser.close()
    print(f"\n{len(PAGES)} pages crawled, {len(problems)} CSP violation(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
