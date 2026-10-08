"""Capture the README screenshots from a running Nexus-OSINT instance.

Run it against a THROWAWAY database loaded with the fictional demo dataset,
never against a real one, because the images are published with the repo:

    DATA_DIR=/tmp/nexus-demo DATABASE_PATH=/tmp/nexus-demo/nexus.db AI_PROVIDER=off \
        python -m uvicorn nexus.web.app:app --host 127.0.0.1 --port 8765
    curl -X POST http://127.0.0.1:8765/demo/load
    python scripts/capture_screenshots.py --base http://127.0.0.1:8765

PNGs are written to docs/media/ at 2x device scale.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "media"
VIEWPORT = {"width": 1440, "height": 900}
# The floating assistant launcher overlaps content in still images.
HIDE_LAUNCHER = "#assistant-launcher{display:none!important}"


def _open(page: Page, url: str, wait_ms: int = 800, *, launcher: bool = False) -> None:
    page.goto(url)
    page.wait_for_load_state("networkidle")
    if not launcher:
        page.add_style_tag(content=HIDE_LAUNCHER)
    page.wait_for_timeout(wait_ms)


def _shot(page: Page, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("saved", name)


def capture(base: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)

        # Feed: scroll past the onboarding card to the analysed items.
        _open(page, f"{base}/")
        top = page.evaluate(
            "document.querySelector('#feed').getBoundingClientRect().top + window.scrollY"
        )
        page.evaluate(f"window.scrollTo(0, {top} - 150)")
        page.wait_for_timeout(300)
        _shot(page, "feed")

        # Item drawer: open the first analysed item.
        page.locator("[hx-get*='/detail']").first.click()
        page.wait_for_timeout(1200)
        _shot(page, "item-drawer")

        for name, path, wait in (
            ("brief", "/brief", 800),
            ("attention", "/attention", 800),
            ("case", "/cases/1", 1200),
            ("timeline", "/cases/1?tab=timeline", 1200),
            ("entity", "/entity?name=Nightjar%20Group", 1200),
            ("settings", "/settings", 800),
        ):
            _open(page, f"{base}{path}", wait)
            _shot(page, name)

        # Relationship graph: frame just the rendered network once physics settles.
        _open(page, f"{base}/graph?case=1&case=2", 7000)
        frame = page.locator("iframe").first
        frame.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        frame.screenshot(path=str(OUT / "graph.png"))
        print("saved graph")

        # Assistant panel open (its welcome text and suggested actions).
        _open(page, f"{base}/", launcher=True)
        page.evaluate("nexusAssistantToggle()")
        page.wait_for_timeout(800)
        _shot(page, "assistant")

        # Light theme variant of the feed.
        _open(page, f"{base}/")
        page.evaluate("document.documentElement.classList.add('light')")
        top = page.evaluate(
            "document.querySelector('#feed').getBoundingClientRect().top + window.scrollY"
        )
        page.evaluate(f"window.scrollTo(0, {top} - 150)")
        page.wait_for_timeout(400)
        _shot(page, "feed-light")

        browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    capture(ap.parse_args().base)
