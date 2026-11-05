"""The flows an analyst relies on, end to end in a real browser. Names are fictional."""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, Route, expect

DRAWER_OPEN = "#item-drawer:not(.translate-x-full)"


def _first_item_id(page: Page) -> int:
    page.goto("/")
    link = page.locator("[hx-get$='/detail']").first
    return int(link.get_attribute("hx-get").split("/")[2])


def _answer(page: Page, items: list[dict], text: str = "See Item 1.") -> None:
    def respond(route: Route) -> None:
        route.fulfill(content_type="application/json",
                      body=json.dumps({"ok": True, "answer": text, "items": items, "actions": []}))

    page.route("**/assistant/ask", respond)


def _ask(page: Page, question: str = "what happened?") -> None:
    page.evaluate("window.nexusAssistantToggle(true)")
    page.fill("#assistant-input", question)
    page.locator("#assistant-send").click()


def test_the_strict_csp_is_served(page: Page):
    response = page.goto("/")
    assert response is not None and response.status == 200
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy.split("style-src")[0]


def test_theme_toggle_survives_a_reload(page: Page):
    page.goto("/")
    before = page.evaluate("document.documentElement.className")
    page.evaluate("document.getElementById('theme-toggle').click()")
    after = page.evaluate("document.documentElement.className")
    assert after != before
    page.reload()
    assert page.evaluate("document.documentElement.className") == after


def test_full_text_search_narrows_the_feed(page: Page):
    page.goto("/")
    items = page.locator("[hx-get$='/detail']")
    expect(items.first).to_be_visible()
    everything = items.count()
    page.fill("#feed-filter input[name=q]", "zzzznothingmatchesthis")
    page.press("#feed-filter input[name=q]", "Enter")
    expect(items).to_have_count(0)
    page.fill("#feed-filter input[name=q]", "Acme")
    page.press("#feed-filter input[name=q]", "Enter")
    expect(items.first).to_be_visible()
    assert 0 < items.count() < everything


def test_item_drawer_opens_and_closes_with_escape(page: Page):
    page.goto("/")
    page.locator("[hx-get$='/detail']").first.click()
    expect(page.locator(DRAWER_OPEN)).to_be_visible()
    expect(page.locator("#item-drawer-content")).not_to_be_empty()
    page.keyboard.press("Escape")
    expect(page.locator("#item-drawer")).to_have_class(re.compile("translate-x-full"))


def test_new_case_appears_on_the_board(page: Page):
    page.goto("/cases")
    page.locator("#new-case-button").click()
    page.fill("#new-case input[name=name]", "Harbor Lantern")
    page.locator("#new-case form button").first.click()
    # Case setup tries the (unreachable) AI model first.
    expect(page.locator("#case-list")).to_contain_text("Harbor Lantern", timeout=60_000)


def test_uploaded_document_is_listed(page: Page):
    page.goto("/sources/documents")
    page.set_input_files("input[name=files]", {
        "name": "field-notes.txt", "mimeType": "text/plain",
        "buffer": b"Harbor inspection notes from the morning shift.",
    })
    page.locator("form[hx-post='/documents/upload'] button").click()
    expect(page.locator("#upload-results")).to_contain_text("field-notes.txt")
    expect(page.locator("#upload-results")).to_contain_text("Searchable now")
    page.reload()
    expect(page.locator("body")).to_contain_text("field-notes.txt")


def test_sherlock_cites_a_source_page_as_a_safe_external_link(page: Page):
    page.goto("/")
    _answer(page, [{"n": 1, "id": 1, "url": "https://wiki.example/fleet", "title": "Fleet",
                    "source": "fleet.txt", "when": ""}])
    _ask(page)
    link = page.locator("#assistant-log a[href='https://wiki.example/fleet']").first
    expect(link).to_be_visible()
    assert link.get_attribute("target") == "_blank" and link.get_attribute("rel") == "noopener"


def test_sherlock_opens_a_document_without_a_page_in_the_drawer(page: Page):
    item = _first_item_id(page)
    _answer(page, [{"n": 1, "id": item, "url": "", "title": "Notes", "source": "notes.txt",
                    "when": ""}])
    _ask(page)
    page.locator("#assistant-log button", has_text="Item 1").first.click()
    expect(page.locator(DRAWER_OPEN)).to_be_visible()
    expect(page.locator("#item-drawer-content")).not_to_be_empty()


def test_citations_still_work_after_moving_to_another_page(page: Page):
    item = _first_item_id(page)
    _answer(page, [{"n": 1, "id": item, "url": "", "title": "Notes", "source": "notes.txt",
                    "when": ""}])
    _ask(page)
    expect(page.locator("#assistant-log button", has_text="Item 1").first).to_be_visible()
    page.goto("/cases")
    page.evaluate("window.nexusAssistantToggle(true)")
    page.locator("#assistant-log button", has_text="Item 1").first.click()
    expect(page.locator(DRAWER_OPEN)).to_be_visible()


def test_a_script_url_from_a_source_never_becomes_a_link(page: Page):
    page.goto("/")
    _answer(page, [{"n": 1, "id": 0, "url": "javascript:window.__pwned=1", "title": "Bad",
                    "source": "x", "when": ""}])
    _ask(page)
    expect(page.locator("#assistant-log")).to_contain_text("See Item 1.")
    assert page.locator("#assistant-log a[href^='javascript']").count() == 0
    assert page.evaluate("window.__pwned") is None


def test_a_plain_form_post_is_accepted(page: Page):
    # The app sends Referrer-Policy: no-referrer, so a browser posts these forms
    # with "Origin: null". Unlike htmx's requests, nothing but a real browser
    # shows that.
    def post(action: str):
        with page.expect_navigation() as moved:
            page.evaluate("a => document.querySelector(`form[action='${a}']`).requestSubmit()",
                          action)
        return moved.value

    page.goto("/cases/1")
    closed = post("/cases/1/close")
    assert closed is not None and closed.status == 200, closed
    assert post("/cases/1/reopen").status == 200


def test_a_sandboxed_page_cannot_post_the_same_way(page: Page):
    # It too sends "Origin: null": this is what the guard must still stop.
    html = ("<form id=f method=post action='/cases/1/close'></form>"
            "<script>document.getElementById('f').submit()</script>")
    page.route("**/sandboxed", lambda route: route.fulfill(
        content_type="text/html", body=html,
        headers={"Content-Security-Policy": "sandbox allow-scripts allow-forms"}))
    with page.expect_response("**/cases/1/close") as seen:
        page.goto("/sandboxed")
    request = seen.value.request
    assert request.all_headers()["origin"] == "null"
    assert seen.value.status == 403
