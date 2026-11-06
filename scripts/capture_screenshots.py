"""Capture the README screenshots from a running Nexus-OSINT instance.

Run it against a THROWAWAY database loaded with the fictional demo dataset,
never against a real one, because the images are published with the repo:

    DATA_DIR=/tmp/nexus-demo DATABASE_PATH=/tmp/nexus-demo/nexus.db AI_PROVIDER=off \
        python -m uvicorn nexus.web.app:app --host 127.0.0.1 --port 8765
    curl -X POST http://127.0.0.1:8765/demo/load
    python scripts/capture_screenshots.py --base http://127.0.0.1:8765

PNGs are written to docs/media/ at 2x device scale. The script also adds a
fictional report and fictional connections to that throwaway instance, for the
documents, connector, publish and activity shots. Nothing is scanned or tested,
Confluence stops at the preview, and the only publish is to a temporary vault.
"""

from __future__ import annotations

import argparse
import tempfile
from io import BytesIO
from pathlib import Path

from docx import Document
from playwright.sync_api import Page, sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "media"
VIEWPORT = {"width": 1440, "height": 900}
# The floating assistant launcher overlaps content in still images.
HIDE_LAUNCHER = "#assistant-launcher{display:none!important}"
FICTIONAL_REPORT = (
    (
        "Executive summary",
        "The Rotterdam terminal outage cost 11 days of container scheduling. "
        "No ransom was paid and operations were restored from offline backups.",
    ),
    (
        "Root cause",
        "The intrusion began through an unpatched VPN appliance (CVE-2026-1442). Shared "
        "administrator accounts let the attackers reach terminal scheduling.",
    ),
    (
        "Supplier exposure",
        "Three suppliers held standing remote access to the terminal network. "
        "Access is now limited to named accounts with a 30-day review.",
    ),
    (
        "Remediation plan",
        "Separate the terminal network, reduce the critical patch window to seven "
        "days and run restore tests every month.",
    ),
)


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


def _report_docx() -> bytes:
    """The fictional report as a Word file: Nexus makes one item per heading."""
    doc = Document()
    doc.add_heading("Harborline Logistics: Q3 Resilience Review", 0)
    for heading, body in FICTIONAL_REPORT:
        doc.add_heading(heading, 1)
        doc.add_paragraph(body)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _seed_extras(page: Page, base: str, vault: Path) -> None:
    """Add the fictional report, connections and vault the new shots need."""
    (vault / "Rotterdam outage.md").write_text("# Rotterdam outage\n", encoding="utf-8")
    headers = {"Origin": base, "Referer": f"{base}/"}
    post = page.request.post
    post(
        f"{base}/documents/upload",
        headers=headers,
        multipart={
            "destination": "pool",
            "files": {
                "name": "Harborline-Q3-Resilience-Review.docx",
                "mimeType": "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document",
                "buffer": _report_docx(),
            },
        },
    )
    post(
        f"{base}/sources/confluence",
        headers=headers,
        form={
            "name": "Harborline Wiki",
            "deployment": "dc",
            "destination": "pool",
            "base_url": "https://wiki.harborline.example",
            "spaces": "SEC,OPS",
            "token": "fictional-token",
            "tls_mode": "system",
        },
    )
    post(f"{base}/sources/confluence/1/publishing", headers=headers)
    post(
        f"{base}/sources/obsidian",
        headers=headers,
        form={
            "name": "Harborline Vault",
            "path": str(vault),
            "destination": "pool",
        },
    )
    post(
        f"{base}/sources/obsidian/1/writing",
        headers=headers,
        form={"allow_write": "1", "write_folder": "Nexus"},
    )
    for kind, name in (("notion", "Team Notion"), ("slack", "Ops Slack")):
        post(
            f"{base}/sources/cloud",
            headers=headers,
            form={
                "kind": kind,
                "name": name,
                "token": "fictional-token",
                "destination": "pool",
            },
        )


def _capture_new_pages(page: Page, base: str) -> None:
    """Documents, a connector screen, the publish preview and the activity log."""
    _open(page, f"{base}/sources/documents")
    chunks = page.locator("button[hx-get*='/chunks']").first
    chunks.click()
    page.wait_for_timeout(700)
    chunks.evaluate("e => window.scrollBy(0, e.getBoundingClientRect().top - 120)")
    page.wait_for_timeout(300)
    _shot(page, "documents")

    _open(page, f"{base}/sources/confluence")
    _shot(page, "connector")

    # Edit an item, then stop at the Confluence preview: nothing is sent.
    _open(page, f"{base}/?q=%22suspected%20operator%22")
    page.locator("[hx-get*='/detail']").first.click()
    page.wait_for_selector("[data-item-detail]")
    drawer = page.locator("[data-item-detail]")
    drawer.locator("button:has-text('Edit')").first.click()
    drawer.locator("textarea[name=note]").fill("Confirmed by our Q3 review.")
    drawer.locator("button:has-text('Save edit')").click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    drawer.locator("summary:has-text('Publish')").click()
    drawer.locator("button[hx-get*='target=confluence']").click()
    page.wait_for_selector("[data-publish-form] input[name=space]")
    close_menu = (
        "document.querySelectorAll('[data-item-detail] details[open]').forEach(d => {"
        " if (d.querySelector(\"button[hx-get*='target=']\")) d.removeAttribute('open'); })"
    )
    page.evaluate(close_menu)
    page.locator("[data-publish-form] input[name=space]").fill("SEC")
    page.locator("[data-publish-form] button:has-text('Preview')").click()
    page.wait_for_selector("[data-publish-preview]")
    page.locator("[data-publish-preview]").evaluate("e => e.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(500)
    _shot(page, "publish-preview")

    # A confirmed publish to the temporary vault gives the activity log a row.
    drawer.locator("summary:has-text('Publish')").evaluate(
        "e => e.scrollIntoView({block: 'center'})"
    )
    drawer.locator("summary:has-text('Publish')").click()
    drawer.locator("button[hx-get*='target=obsidian']").click()
    page.wait_for_selector("[data-publish-form] select[name=vault_id]")
    page.evaluate(close_menu)
    page.locator("[data-publish-form] button:has-text('Preview')").click()
    page.wait_for_selector("[data-publish-preview]")
    page.locator("[data-publish-preview] button:has-text('Confirm')").click()
    page.wait_for_selector("text=Saved to Obsidian")

    _open(page, f"{base}/security/outbound")
    _shot(page, "activity-log")


def capture(base: str) -> None:
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=["--lang=en-US"])
        except Exception:  # Playwright's Chromium not installed: use the system Edge
            browser = p.chromium.launch(channel="msedge", args=["--lang=en-US"])
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2, locale="en-US")

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
            ("setup", "/setup", 800),
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

        with tempfile.TemporaryDirectory() as vault:
            _seed_extras(page, base, Path(vault))
            _capture_new_pages(page, base)

        browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    capture(ap.parse_args().base)
